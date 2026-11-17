"""`python -m tickr.probes`: the chart probes C1 and C2 against fakes. No Webull, no SDK."""

from __future__ import annotations

import json
import os
import shutil
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from tickr import config, probes
from tickr.api import contract as api
from tickr.broker.base import RateLimited
from tickr.broker.budget import BudgetExhausted, RequestBudget, RouteLimit
from tickr.broker.webull import BrokerReadFailed, Route
from tickr.cli import data_paths
from tickr.data.base import FakeMarketData

ROOT = Path(__file__).resolve().parents[1]
CFG = config.load(ROOT / "config" / "bot.toml")
ACCOUNT = ROOT / "tests" / "api" / "fixtures" / "account-v1.json"
EXAMPLE = ROOT / "tests" / "api" / "fixtures" / "status-v1.example.json"
T0 = datetime(2026, 11, 16, 13, 0, tzinfo=UTC)  # 08:00 New York


class Clock:
    """A clock that only moves when the probe sleeps."""

    def __init__(self) -> None:
        self.at = T0
        self.slept: list[float] = []

    def now(self) -> datetime:
        return self.at

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.at += timedelta(seconds=seconds)


def cfg_in(tmp_path: Path, *, chart_on: bool = False) -> config.Config:
    calendar = Path(CFG.session.calendar_file).parent
    if not (tmp_path / calendar).exists():
        shutil.copytree(ROOT / calendar, tmp_path / calendar)
    return replace(CFG, root=tmp_path, chart=replace(CFG.chart, webull_enabled=chart_on))


def live_bot(cfg: config.Config) -> Path:
    """A lock that records a live process: this test's own."""
    _, lock = data_paths(cfg)
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.write_text(str(os.getpid()))
    return lock


def config_file(tmp_path: Path, *, after: Path | None = None) -> Path:
    path = tmp_path / "bot.toml"
    path.write_text("")
    if after is not None:
        later = after.stat().st_mtime_ns + 10**9
        os.utime(path, ns=(later, later))
    return path


class Raises(FakeMarketData):
    def __init__(self, error: Exception) -> None:
        super().__init__()
        self.error = error

    def history_bars(self, symbol, span, end, count):  # noqa: ANN001, ANN201
        raise self.error


@pytest.mark.parametrize("error, expected", [
    (BudgetExhausted("bars", RequestBudget.CAUSE_WINDOW_FULL, 1.0),
     {"outcome": probes.NOT_SENT, "cause": RequestBudget.CAUSE_WINDOW_FULL}),
    (RateLimited("BARS: HTTP 429"), {"outcome": probes.HTTP_429}),
    (BrokerReadFailed("BARS", 500, "INTERNAL"),
     {"outcome": probes.READ_FAILED, "http_status": 500, "code": "INTERNAL"}),
])
def test_read_once_names_what_came_back_and_never_raises(error, expected):
    assert probes.read_once(Raises(error), "ABC", T0) == expected


def test_read_once_tells_our_own_budget_from_a_429():
    """Only a 429 from Webull answers C2; a request our budget refused was never sent."""
    assert probes.read_once(FakeMarketData(), "ABC", T0) == {"outcome": probes.OK, "bars": 0}
    assert issubclass(BudgetExhausted, RateLimited)


def test_the_limit_worker_spreads_its_requests_over_one_window(tmp_path):
    clock, market, out = Clock(), FakeMarketData(), tmp_path / "worker.jsonl"
    start = T0 + timedelta(seconds=20)
    probes.run_limit_worker(market, "ABC", 20, 60.0, start, out, now=clock.now,
                            sleep=clock.sleep)
    rows = [json.loads(line) for line in out.read_text().splitlines()]
    assert [row["seq"] for row in rows] == list(range(20))
    assert {row["outcome"] for row in rows} == {probes.OK}
    sent = [datetime.fromisoformat(row["sent_at"]) for row in rows]
    assert sent[0] == start and sent[-1] == start + timedelta(seconds=57)
    assert len(market.calls) == 20  # one request each, never repeated


def test_the_limit_worker_records_a_429_and_goes_on_without_repeating(tmp_path):
    clock, market, out = Clock(), FakeMarketData(), tmp_path / "worker.jsonl"
    market.limit("history_bars", 3)
    probes.run_limit_worker(market, "ABC", 5, 60.0, T0, out, now=clock.now, sleep=clock.sleep)
    rows = [json.loads(line) for line in out.read_text().splitlines()]
    assert [row["outcome"] for row in rows] == [probes.OK] * 3 + [probes.HTTP_429] * 2
    assert len(market.calls) == 3


def rows(*outcomes: str) -> list[dict]:
    return [{"seq": seq, "outcome": outcome} for seq, outcome in enumerate(outcomes)]


def test_the_limit_verdict():
    full = rows(*[probes.OK] * 20)
    assert probes.limit_verdict([full, full], 20) == probes.NOT_SHARED
    assert probes.limit_verdict([full, rows(*[probes.OK] * 12, probes.HTTP_429)], 20) \
        == probes.SHARED
    # A process that did not finish, or a read that failed for another reason, proves nothing.
    assert probes.limit_verdict([full, rows(*[probes.OK] * 12)], 20) == probes.INCONCLUSIVE
    assert probes.limit_verdict([full, rows(*[probes.OK] * 19, probes.READ_FAILED)], 20) \
        == probes.INCONCLUSIVE
    assert probes.limit_verdict([full, []], 20) == probes.INCONCLUSIVE


def test_chart_limit_refuses_next_to_a_bot(tmp_path):
    cfg = cfg_in(tmp_path)
    live_bot(cfg)
    launched: list[list[list[str]]] = []
    with pytest.raises(probes.ProbeRefused, match="recorded in the lock"):
        probes.chart_limit(cfg, config_file(tmp_path), "ABC", now=Clock().now,
                           launch=lambda commands: launched.append(commands) or [])
    assert launched == [] and not (tmp_path / CFG.run.data_dir / CFG.probe.evidence_dir).exists()


@pytest.mark.parametrize("at, refused", [
    (datetime(2026, 11, 16, 14, 29, 59, tzinfo=UTC), None),          # 09:29:59 New York
    (datetime(2026, 11, 16, 14, 30, tzinfo=UTC), "regular session is on"),
    (datetime(2026, 11, 16, 20, 59, 59, tzinfo=UTC), "regular session is on"),
    (datetime(2026, 11, 16, 21, 0, tzinfo=UTC), None),                # the close
    (datetime(2026, 11, 15, 17, 0, tzinfo=UTC), None),                # a Sunday at noon
    (datetime(2031, 1, 6, 17, 0, tzinfo=UTC), "calendar cannot say"),  # no calendar file
])
def test_chart_limit_runs_only_outside_the_regular_session(tmp_path, at, refused):
    cfg, launched = cfg_in(tmp_path), []

    def run() -> None:
        probes.chart_limit(cfg, config_file(tmp_path), "ABC", now=lambda: at,
                           launch=lambda commands: launched.append(commands) or [0, 0])

    if refused is None:
        run()
        assert len(launched) == 1
    else:
        with pytest.raises(probes.ProbeRefused, match=refused):
            run()
        assert launched == []


@pytest.mark.parametrize("processes, requests, cause", [
    (2, 31, "above the bars route limit"),       # a process would pass its own limit
    (1, 20, "would prove nothing"),              # together they would not pass the limit
])
def test_chart_limit_refuses_numbers_that_cannot_answer(tmp_path, processes, requests, cause):
    cfg = cfg_in(tmp_path)
    cfg = replace(cfg, probe=replace(cfg.probe, chart_limit_processes=processes,
                                     chart_limit_requests=requests))
    assert probes.budget_from_config(cfg).limit_for(Route.BARS.path) == RouteLimit(
        30, 60.0, float(cfg.environment.rate_limit_cooldown_seconds))
    with pytest.raises(probes.ProbeRefused, match=cause):
        probes.chart_limit(cfg, config_file(tmp_path), "ABC", now=Clock().now,
                           launch=lambda commands: [])


def test_chart_limit_starts_its_processes_and_writes_the_answer(tmp_path):
    cfg, clock = cfg_in(tmp_path), Clock()
    path = config_file(tmp_path)

    def launch(commands: list[list[str]]) -> list[int]:
        assert len(commands) == cfg.probe.chart_limit_processes
        for index, command in enumerate(commands):
            assert command[1:6] == ["-m", "tickr.probes", "--config", str(path),
                                    "chart-limit-worker"]
            out, start = Path(command[command.index("--out") + 1]), command[-1]
            assert datetime.fromisoformat(start) == T0 + timedelta(
                seconds=cfg.probe.chart_limit_start_delay_seconds)
            market = FakeMarketData()
            if index:
                market.limit("history_bars", 11)  # Webull refuses the 31st request overall
            probes.run_limit_worker(market, "ABC", cfg.probe.chart_limit_requests, 60.0,
                                    datetime.fromisoformat(start), out,
                                    now=clock.now, sleep=lambda seconds: None)
        return [0] * len(commands)

    verdict, folder = probes.chart_limit(cfg, path, "ABC", now=clock.now, launch=launch)
    assert verdict == probes.SHARED
    assert folder == tmp_path / cfg.run.data_dir / "probes" / "2026-11-16" / "chart-limit-130000"
    summary = json.loads((folder / probes.SUMMARY_FILE).read_text())
    assert summary["verdict"] == probes.SHARED and summary["route_limit"] == 30
    assert summary["workers"] == [
        {"exit_code": 0, "outcomes": {probes.OK: 20}},
        {"exit_code": 0, "outcomes": {probes.OK: 11, probes.HTTP_429: 9}}]


def test_a_symbol_that_is_not_a_ticker_is_refused(tmp_path):
    with pytest.raises(probes.ProbeRefused, match="not a ticker"):
        probes.chart_limit(cfg_in(tmp_path), config_file(tmp_path), "abc; rm", now=Clock().now)


# --- chart-client ------------------------------------------------------------------------------


class Bot:
    """Writes the status file the way a running bot does: a fresh one before every probe read."""

    def __init__(self, cfg: config.Config, clock: Clock, *, reads_broker: bool = True,
                 fence: str | None = None, exposed: str | None = None) -> None:
        self.cfg, self.clock, self.reads_broker, self.fence = cfg, clock, reads_broker, fence
        self.exposed = exposed
        self.path = cfg.root / cfg.run.data_dir / cfg.api.status_file
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.example = api.loads(api.StatusV1, EXAMPLE.read_bytes())
        self.account = api.loads(api.AccountV1, ACCOUNT.read_bytes())

    def write(self) -> None:
        at = self.clock.now()
        read_at = at if self.reads_broker else T0 - timedelta(minutes=5)
        fences = () if self.fence is None else (api.Fence(self.fence, at, "x"),)
        flat = replace(self.example, position=None, positions=(), entry_block=None)
        seen = replace(self.account, read_at=at, positions=(), orders=())
        order = self.account.orders[0]
        if self.exposed == "broker-position":
            seen = replace(seen, positions=self.account.positions[:1])
        elif self.exposed == "broker-order":
            seen = replace(seen, orders=(replace(order, ours=True),))
        elif self.exposed == "foreign-order":
            seen = replace(seen, orders=(replace(order, ours=False),))
        api.write_account(self.path.parent / self.cfg.api.account_file, seen)
        shown = {None: flat, "position": replace(flat, positions=self.example.positions),
                 "first-position": replace(flat, position=self.example.position),
                 "working-order": replace(flat, entry_block=self.example.entry_block)}
        api.write_status(self.path, replace(
            shown.get(self.exposed, flat), written_at=at, fences=fences,
            freshness=replace(self.example.freshness, last_broker_read_at=read_at)))


class Watched(FakeMarketData):
    def __init__(self, bot: Bot) -> None:
        super().__init__()
        self.bot = bot

    def history_bars(self, symbol, span, end, count):  # noqa: ANN001, ANN201
        self.bot.write()
        return super().history_bars(symbol, span, end, count)


def client_run(tmp_path: Path, **bot_options) -> tuple[str, dict, list[dict], Watched]:
    cfg, clock = cfg_in(tmp_path, chart_on=True), Clock()
    path = config_file(tmp_path)
    live_bot(cfg)
    market = Watched(Bot(cfg, clock, **bot_options))
    market.bot.write()
    verdict, folder = probes.chart_client(cfg, path, "ABC", now=clock.now, sleep=clock.sleep,
                                          market=lambda _: market)
    summary = json.loads((folder / probes.SUMMARY_FILE).read_text())
    reads = [json.loads(line) for line in (folder / "reads.jsonl").read_text().splitlines()]
    assert clock.slept == [CFG.probe.chart_client_interval_seconds] * (len(reads) - 1)
    return verdict, summary, reads, market


def test_chart_client_passes_when_the_bot_keeps_reading(tmp_path):
    verdict, summary, reads, market = client_run(tmp_path)
    assert verdict == probes.PASSED and summary["problems"] == []
    assert len(reads) == len(market.calls) == CFG.probe.chart_client_reads
    assert summary["outcomes"] == {probes.OK: CFG.probe.chart_client_reads}
    assert reads[0]["bot"] == {"readable": True, "status_age_seconds": 0,
                               "last_broker_read_at": T0.isoformat(),
                               "broker_read_failed": False}


def summary_file(cfg: config.Config, day: str, run: str, **fields) -> Path:
    folder = cfg.root / cfg.run.data_dir / cfg.probe.evidence_dir / day / run
    folder.mkdir(parents=True)
    path = folder / probes.SUMMARY_FILE
    path.write_text(json.dumps({"environment": cfg.environment_name, **fields}))
    return path


def test_the_chart_gate_opens_only_on_a_newest_run_that_passed(tmp_path):
    cfg = cfg_in(tmp_path, chart_on=True)
    assert "is not recorded for sandbox" in probes.chart_client_gate(cfg)
    summary_file(cfg, "2026-11-16", "chart-client-150000", verdict=probes.PASSED,
                 started_at="2026-11-16T15:00:00+00:00")
    assert probes.chart_client_gate(cfg) is None
    # Another probe's summary and another environment's run say nothing about this one.
    summary_file(cfg, "2026-11-16", "chart-limit-160000", verdict=probes.SHARED,
                 started_at="2026-11-16T16:00:00+00:00")
    other = summary_file(cfg, "2026-11-17", "chart-client-150000", verdict=probes.FAILED,
                         started_at="2026-11-17T15:00:00+00:00")
    other.write_text(other.read_text().replace(cfg.environment_name, "production"))
    assert probes.chart_client_gate(cfg) is None
    assert "last ended 'failed' for production" in probes.chart_client_gate(
        replace(cfg, run=replace(cfg.run, environment="production")))
    # A later run that failed closes it again, wherever its folder sorts.
    summary_file(cfg, "2026-11-16", "chart-client-140000", verdict=probes.FAILED,
                 started_at="2026-11-16T19:00:00+00:00")
    assert "last ended 'failed' for sandbox" in probes.chart_client_gate(cfg)


def test_the_chart_gate_shuts_on_a_newer_run_that_left_no_summary(tmp_path):
    cfg = cfg_in(tmp_path, chart_on=True)
    passed_at = datetime(2026, 11, 16, 15, 0, tzinfo=UTC)
    summary_file(cfg, "2026-11-16", "chart-client-150000", verdict=probes.PASSED,
                 started_at=passed_at.isoformat())
    crashed = summary_file(cfg, "2026-11-16", "chart-client-140000").parent
    (crashed / probes.SUMMARY_FILE).unlink()
    reads = crashed / "reads.jsonl"
    reads.write_text("{}\n")

    def touch(at: datetime) -> None:
        for path in (reads, crashed):
            os.utime(path, (at.timestamp(), at.timestamp()))

    touch(passed_at - timedelta(hours=1))  # it crashed before the run that passed
    assert probes.chart_client_gate(cfg) is None
    touch(passed_at + timedelta(hours=1))  # it crashed after it, or is still running
    assert "last ended 'unfinished' for sandbox" in probes.chart_client_gate(cfg)
    summary_file(cfg, "2026-11-16", "chart-client-170000", verdict=probes.PASSED,
                 started_at=(passed_at + timedelta(hours=2)).isoformat())
    assert probes.chart_client_gate(cfg) is None


@pytest.mark.parametrize("text", ["{", "[]", '{"environment": "sandbox"}',
                                  '{"environment": "sandbox", "verdict": "passed", '
                                  '"started_at": "2026-11-16T15:00:00"}'])
def test_the_chart_gate_stays_shut_on_a_summary_it_cannot_read(tmp_path, text):
    cfg = cfg_in(tmp_path, chart_on=True)
    summary_file(cfg, "2026-11-16", "chart-client-150000", verdict=probes.PASSED,
                 started_at="2026-11-16T15:00:00+00:00")
    summary_file(cfg, "2026-11-17", "chart-client-150000").write_text(text)
    cause = probes.chart_client_gate(cfg)
    assert "summary.json cannot be read" in cause and "remove that run's folder" in cause


def test_a_passed_chart_client_run_opens_the_chart_gate(tmp_path):
    verdict, _, _, _ = client_run(tmp_path)
    assert verdict == probes.PASSED
    assert probes.chart_client_gate(cfg_in(tmp_path, chart_on=True)) is None


def test_chart_client_fails_when_the_bot_stops_reading_the_broker(tmp_path):
    verdict, summary, _, _ = client_run(tmp_path, reads_broker=False)
    assert verdict == probes.FAILED
    assert summary["problems"] == ["the bot's last broker read did not move during the run"]


def test_chart_client_fails_on_a_broker_read_fence(tmp_path):
    verdict, summary, _, _ = client_run(tmp_path, fence="broker-read-failed:open-orders")
    assert verdict == probes.FAILED
    assert "read 0: the bot reports failing broker reads" in summary["problems"]


def test_chart_client_problems_name_refused_reads_and_a_changed_token():
    bot = {"readable": True, "status_age_seconds": 1, "broker_read_failed": False}
    sample = [{"seq": 0, "outcome": probes.OK,
               "bot": {**bot, "last_broker_read_at": T0.isoformat()}},
              {"seq": 1, "outcome": probes.HTTP_429,
               "bot": {**bot, "status_age_seconds": 40,
                       "last_broker_read_at": (T0 + timedelta(seconds=30)).isoformat()}},
              {"seq": 2, "outcome": probes.OK, "bot": {"readable": False, "type": "OSError"}}]
    assert probes.client_problems(sample, True, CFG.api.status_stale_seconds) == [
        "probe reads not answered: [1]",
        "the token folder changed during the run",
        "read 1: the bot's status was 40 s old",
        "read 2: the bot's status file was unreadable",
        "the bot's last broker read did not move during the run"]


def test_the_token_fingerprint_sees_a_rewrite_and_never_a_name(tmp_path):
    folder = tmp_path / "token"
    assert probes.token_fingerprint(folder) == []
    folder.mkdir()
    (folder / "secret-name.txt").write_text("abc")
    before = probes.token_fingerprint(folder)
    assert before == [(3, (folder / "secret-name.txt").stat().st_mtime_ns)]
    (folder / "secret-name.txt").write_text("abcd")
    assert probes.token_fingerprint(folder) != before


def refused_client(tmp_path: Path, cfg: config.Config, path: Path) -> str:
    built: list[object] = []
    with pytest.raises(probes.ProbeRefused) as refused:
        probes.chart_client(cfg, path, "ABC", now=Clock().now, sleep=lambda seconds: None,
                            market=lambda _: built.append(1) or FakeMarketData())
    assert built == []  # no client was built, so nothing was sent
    return str(refused.value)


def test_chart_client_refuses_without_a_running_bot(tmp_path):
    cfg = cfg_in(tmp_path, chart_on=True)
    assert "no bot process" in refused_client(tmp_path, cfg, config_file(tmp_path))


def test_chart_client_refuses_outside_the_chart_slice(tmp_path):
    cfg = cfg_in(tmp_path, chart_on=False)
    path = config_file(tmp_path)
    live_bot(cfg)
    assert "would not be inside any budget" in refused_client(tmp_path, cfg, path)


def test_chart_client_refuses_a_config_changed_after_the_bot_started(tmp_path):
    cfg = cfg_in(tmp_path, chart_on=True)
    lock = live_bot(cfg)
    path = config_file(tmp_path, after=lock)
    assert "changed after the bot started" in refused_client(tmp_path, cfg, path)


@pytest.mark.parametrize("exposed", ["position", "first-position", "working-order",
                                     "broker-position", "broker-order"])
def test_chart_client_refuses_while_the_bot_has_a_trade_open(tmp_path, exposed):
    cfg = cfg_in(tmp_path, chart_on=True)
    path = config_file(tmp_path)
    live_bot(cfg)
    Bot(cfg, Clock(), exposed=exposed).write()
    assert "run this probe while it is flat" in refused_client(tmp_path, cfg, path)


def test_chart_client_runs_next_to_an_order_that_is_not_the_bots(tmp_path):
    verdict, _, _, _ = client_run(tmp_path, exposed="foreign-order")
    assert verdict == probes.PASSED


def test_chart_client_refuses_when_the_bots_last_broker_read_cannot_say(tmp_path):
    cfg = cfg_in(tmp_path, chart_on=True)
    path = config_file(tmp_path)
    live_bot(cfg)
    bot = Bot(cfg, Clock())
    bot.write()
    account = bot.path.parent / cfg.api.account_file
    api.write_account(account, replace(bot.account, positions=(), orders=(), read_at=T0 - timedelta(
        seconds=float(CFG.api.account_stale_seconds) + 1)))
    assert "last broker read is 31 s old" in refused_client(tmp_path, cfg, path)
    account.unlink()
    assert "account file cannot be read" in refused_client(tmp_path, cfg, path)


def test_chart_client_refuses_when_the_bots_status_cannot_say(tmp_path):
    cfg = cfg_in(tmp_path, chart_on=True)
    path = config_file(tmp_path)
    live_bot(cfg)
    assert "cannot be read" in refused_client(tmp_path, cfg, path)
    earlier = Clock()
    earlier.at = T0 - timedelta(seconds=float(CFG.api.status_stale_seconds) + 1)
    Bot(cfg, earlier).write()
    assert "it is unknown whether a trade is open" in refused_client(tmp_path, cfg, path)


# --- command line ------------------------------------------------------------------------------


def test_main_refuses_with_exit_3_and_sends_nothing(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(probes.config_module, "load", lambda path: cfg_in(tmp_path))
    monkeypatch.setattr(probes, "CLIENT_MARKET", lambda cfg: pytest.fail("built a client"))
    assert probes.main(["--config", str(config_file(tmp_path)), "chart-client", "ABC"]) \
        == probes.EXIT_REFUSED
    assert "refused: no bot process" in capsys.readouterr().err


def test_main_runs_one_worker_process(tmp_path, monkeypatch):
    clock, market, out = Clock(), FakeMarketData(), tmp_path / "w.jsonl"
    monkeypatch.setattr(probes.config_module, "load", lambda path: cfg_in(tmp_path))
    monkeypatch.setattr(probes, "LIMIT_MARKET", lambda cfg: market)
    monkeypatch.setattr(probes, "NOW", clock.now)
    monkeypatch.setattr(probes, "SLEEP", clock.sleep)
    assert probes.main(["chart-limit-worker", "ABC", "--out", str(out),
                        "--start-at", T0.isoformat()]) == probes.EXIT_ANSWERED
    assert len(out.read_text().splitlines()) == CFG.probe.chart_limit_requests
    assert len(market.calls) == CFG.probe.chart_limit_requests


# --- tick (K) ----------------------------------------------------------------------------------


def test_tick_refuses_next_to_a_bot_and_builds_nothing(tmp_path):
    cfg = cfg_in(tmp_path)
    live_bot(cfg)
    with pytest.raises(probes.ProbeRefused, match="entitlement"):
        probes.tick(cfg, ["SOFI"], now=Clock().now, sleep=Clock().sleep,
                    market=lambda c: pytest.fail("built a client"),
                    stream=lambda c, e: pytest.fail("built a stream"))
    assert not (tmp_path / CFG.run.data_dir / CFG.probe.evidence_dir).exists()


def test_tick_takes_the_cap_symbols_from_the_screeners(tmp_path, monkeypatch, capsys):
    from tickr.data.base import ScreenerKind, ScreenerRow, StreamAnswer
    from tickr.engine import tick_probe

    market = FakeMarketData()
    for kind, names in ((ScreenerKind.TOP_ACTIVES, ["SOFI", "NIO"]),
                        (ScreenerKind.GAINERS, ["ABC"]), (ScreenerKind.LOSERS, ["XYZ"])):
        market.set_screener(kind, [ScreenerRow(kind, n, f"id-{s}", s, "NAS", "USD", "CS")
                                   for n, s in enumerate(names, start=1)])
    sent: list[tuple[str, list[str]]] = []

    class Stream:
        def __init__(self, on_event):
            self.on_event = on_event

        def start(self):
            pass

        def wait_connected(self, seconds):
            return True

        def subscribe(self, symbols, sub_type):
            sent.append((sub_type, list(symbols)))
            if sub_type == tick_probe.TICK:
                self.on_event({"kind": "tick", "symbol": symbols[0]})
            return StreamAnswer(True)

        def unsubscribe_all(self):
            return StreamAnswer(True)

        def stop(self):
            pass

    clock = Clock()
    monkeypatch.setattr(probes.config_module, "load", lambda path: cfg_in(tmp_path))
    monkeypatch.setattr(probes, "LIMIT_MARKET", lambda cfg: market)
    monkeypatch.setattr(probes, "TICK_STREAM", lambda cfg, on_event: Stream(on_event))
    monkeypatch.setattr(probes, "NOW", clock.now)
    monkeypatch.setattr(probes, "SLEEP", clock.sleep)
    # Four distinct symbols cannot reach the first cap step: all four are sent, and with no
    # refusal q2 stays unanswered.
    assert probes.main(["tick", "SOFI"]) == probes.EXIT_NO_ANSWER
    assert sent[0] == (tick_probe.TICK, ["SOFI"]) and sent[1] == (tick_probe.QUOTE, ["SOFI"])
    out = capsys.readouterr().out
    assert "q1_tick_streams: streams" in out and "universe-too-small: 4 symbol(s) held of 4" in out


def test_tick_holds_the_lock_for_its_run(tmp_path):
    from tickr.data.base import StreamAnswer
    from tickr.lock import SingleInstanceLock

    cfg = cfg_in(tmp_path)
    _, lock_path = data_paths(cfg)
    seen = []

    class Stream:
        def start(self):
            try:  # a bot job starting during the probe is refused
                SingleInstanceLock(lock_path).acquire()
                seen.append("bot started")
            except Exception as exc:  # noqa: BLE001
                seen.append(type(exc).__name__)

        def wait_connected(self, seconds):
            return False

        def stop(self):
            pass

        def subscribe(self, symbols, sub_type):
            return StreamAnswer(True)

        def unsubscribe_all(self):
            return StreamAnswer(True)

    probes.tick(cfg, ["SOFI"], now=Clock().now, sleep=Clock().sleep,
                market=lambda c: FakeMarketData(), stream=lambda c, e: Stream(),
                out=lambda line: None)
    assert seen == ["AlreadyRunning"]
    with SingleInstanceLock(lock_path):  # released after the run
        pass


# --- history (H2) -------------------------------------------------------------------------


class Pages:
    """A broker whose history pages are scripted shapes."""

    def __init__(self, *pages: dict) -> None:
        self.pages, self.asked = list(pages), []

    def history_page_shapes(self, max_pages: int) -> list[dict]:
        self.asked.append(max_pages)
        return self.pages[:max_pages]


def page(times: list[str], more: bool) -> dict:
    return {"combos": len(times), "rows": len(times), "own_rows": len(times),
            "place_times": times, "next_page": more}


def test_history_probe_records_page_size_and_order(tmp_path):
    cfg, lines = cfg_in(tmp_path), []
    broker = Pages(page(["2026-11-17T15:03:00+00:00", "2026-11-17T15:02:00+00:00"], True),
                   page(["2026-11-17T15:01:00+00:00"], False))
    verdict, folder = probes.history(cfg, now=Clock().now, broker=lambda _: broker,
                                     out=lines.append)
    summary = json.loads((folder / probes.SUMMARY_FILE).read_text())
    assert verdict == probes.PASSED and broker.asked == [cfg.probe.history_max_pages]
    assert (summary["paging"], summary["page_size"], summary["order"]) == (
        probes.PAGED, 2, probes.NEWEST_FIRST)
    assert summary["rows_per_page"] == [2, 1] and not summary["last_page_has_next"]
    assert lines[0].startswith("h2_history: 2 page(s)")
    assert len((folder / "pages.jsonl").read_text().splitlines()) == 2


def test_one_history_page_or_mixed_order_leaves_h2_open(tmp_path):
    cfg = cfg_in(tmp_path)
    one = Pages(page(["2026-11-17T15:01:00+00:00", "2026-11-17T15:02:00+00:00"], False))
    verdict, folder = probes.history(cfg, now=Clock().now, broker=lambda _: one,
                                     out=lambda _: None)
    summary = json.loads((folder / probes.SUMMARY_FILE).read_text())
    assert verdict == probes.UNFINISHED
    assert (summary["paging"], summary["order"]) == (probes.ONE_PAGE, probes.OLDEST_FIRST)
    assert probes.history_order(["2026-11-17T15:01:00+00:00", "2026-11-17T15:03:00+00:00",
                                 "2026-11-17T15:02:00+00:00"]) == probes.MIXED


def test_history_probe_refuses_next_to_a_bot_with_a_trade_open(tmp_path):
    cfg = cfg_in(tmp_path)
    live_bot(cfg)
    Bot(cfg, Clock(), exposed="position").write()
    broker = Pages()
    with pytest.raises(probes.ProbeRefused, match="flat"):
        probes.history(cfg, now=Clock().now, broker=lambda _: broker)
    assert broker.asked == []
