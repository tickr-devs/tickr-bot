"""Read-only Webull probes for the owner's Mac: `python -m aperture.probes <command> SYMBOL`.

They answer two open facts of docs/webull-facts.md, section C, with chart bars reads only (no
account route, no order, the account id is never read):

- `chart-limit` (C2): is the bars route limit counted per App Key across processes? It starts
  `[probe] chart_limit_processes` processes that each send `chart_limit_requests` bars requests
  inside one route window. Each process stays within the route limit on its own; together they
  pass it. An HTTP 429 in any of them says the limit is shared. This is deliberately more than
  one process may send, so it REFUSES while a bot process is recorded in the lock (it has no
  place in the bot's budget) and during the regular session, when the watchdog may start a bot
  at any moment and a paused bars route would cost it evaluations.
- `chart-client` (C1): can a second Webull client read next to the running bot? It reads bars
  from its own market-data-only client while the bot runs, and after each read notes whether the
  bot still reads the broker and whether the token folder changed. It runs only inside the
  chart's own slice of the bars limit: `[chart] webull_enabled` must be true, and the config
  file must not have changed since the bot started (the bot gave up that slice when it started).
  If the experiment fails the bot may lose its broker reads, so it REFUSES to start while the
  bot's status or its last broker read shows a position or a working order of its own, or
  either cannot be read or is stale.

Every request is sent once and never repeated. Evidence goes to
`<data_dir>/<[probe] evidence_dir>/<NY date>/<probe>-<UTC time>/`: one JSON line per request and
a `summary.json`. Nothing in it names the account or a credential; the token folder is described
by file count, sizes and modification times only.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from collections.abc import Callable, Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from aperture import config as config_module
from aperture.api import contract as api
from aperture.api.chart import CHART_SYMBOL
from aperture.broker.base import BrokerError, RateLimited
from aperture.broker.budget import BudgetExhausted
from aperture.broker.webull import (
    BrokerReadFailed,
    Route,
    budget_from_config,
    chart_market_data_from_config,
    chart_probe_market_data_from_config,
)
from aperture.calendar import CalendarCause, CalendarRefused, SessionCalendar, ny_date_of
from aperture.cli import data_paths
from aperture.config import Config, ConfigError
from aperture.data.base import BarSpan, MarketData, MarketDataError
from aperture.engine.core import Fence
from aperture.lock import recorded_holder

# One bar is enough: the probes count requests and answers, never prices.
BARS_PER_READ = 1

OK = "ok"
NOT_SENT = "not-sent"
HTTP_429 = "http-429"
READ_FAILED = "read-failed"
ERROR = "error"

SHARED = "shared"
NOT_SHARED = "not-shared"
INCONCLUSIVE = "inconclusive"
PASSED = "passed"
FAILED = "failed"

SUMMARY_FILE = "summary.json"
EXIT_ANSWERED = 0
EXIT_CONFIG = 2
EXIT_REFUSED = 3
EXIT_NO_ANSWER = 4

Clock = Callable[[], datetime]
Sleep = Callable[[float], None]
Launch = Callable[[list[list[str]]], list[int]]


class ProbeRefused(Exception):
    """The probe may not run now; nothing was sent. The message is the single cause."""


def read_once(market: MarketData, symbol: str, now: datetime) -> dict[str, Any]:
    """One bars request and what came back, as evidence. Never raises for a broker answer."""
    try:
        bars = market.history_bars(symbol, BarSpan.M1, now, BARS_PER_READ)
    except BudgetExhausted as exc:  # our own budget refused: nothing was sent
        return {"outcome": NOT_SENT, "cause": exc.cause}
    except RateLimited:
        return {"outcome": HTTP_429}
    except BrokerReadFailed as exc:
        return {"outcome": READ_FAILED, "http_status": exc.http_status, "code": exc.code}
    except (BrokerError, MarketDataError) as exc:
        return {"outcome": ERROR, "type": type(exc).__name__}
    return {"outcome": OK, "bars": len(bars)}


def _wait_until(due: datetime, now: Clock, sleep: Sleep) -> None:
    delay = (due - now()).total_seconds()
    if delay > 0:
        sleep(delay)


def _append(path: Path, row: dict[str, Any]) -> None:
    with path.open("a", encoding="utf-8") as out:
        out.write(json.dumps(row, sort_keys=True) + "\n")


def _rows(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text("utf-8").splitlines() if line]


def evidence_folder(cfg: Config, probe: str, at: datetime) -> Path:
    folder = (cfg.root / cfg.run.data_dir / cfg.probe.evidence_dir / ny_date_of(at).isoformat()
              / f"{probe}-{at.astimezone(UTC):%H%M%S}")
    folder.mkdir(parents=True, exist_ok=False)
    return folder


def _require_symbol(symbol: str) -> None:
    if not CHART_SYMBOL.match(symbol):
        raise ProbeRefused(f"{symbol!r} is not a ticker")


# --------------------------------------------------------------------------------------------
# chart-limit (C2)
# --------------------------------------------------------------------------------------------

NO_SESSION_CAUSES = frozenset({CalendarCause.WEEKEND, CalendarCause.HOLIDAY})


def _refuse_in_regular_session(cfg: Config, at: datetime) -> None:
    """Outside the regular session only. A calendar that cannot answer is a refusal too."""
    try:
        day = SessionCalendar.load((cfg.root / cfg.session.calendar_file).parent).day_at(at)
    except CalendarRefused as exc:
        if exc.cause in NO_SESSION_CAUSES:
            return
        raise ProbeRefused(f"the calendar cannot say whether the regular session is on: "
                           f"{exc.cause}: {exc.detail}") from exc
    if day.in_regular_session(at):
        raise ProbeRefused("the regular session is on: the watchdog may start a bot at any "
                           "moment, and this probe can pause the bars route it needs")


def run_limit_worker(market: MarketData, symbol: str, requests: int, window_seconds: float,
                     start_at: datetime, out: Path, *, now: Clock, sleep: Sleep) -> None:
    """One process of `chart-limit`: `requests` reads spread evenly over one route window that
    starts at `start_at`, each written to `out` as soon as it is answered."""
    step = timedelta(seconds=window_seconds / requests)
    for seq in range(requests):
        _wait_until(start_at + seq * step, now, sleep)
        sent_at = now()
        answer = read_once(market, symbol, sent_at)
        _append(out, {"seq": seq, "sent_at": sent_at.isoformat(),
                      "answered_at": now().isoformat(), **answer})


def limit_verdict(workers: Sequence[Sequence[dict[str, Any]]], requests: int) -> str:
    """SHARED: Webull refused a request with HTTP 429 although no process passed the limit on
    its own. NOT_SHARED: every request of every process was answered. Anything else (a process
    that did not finish, a failed read) proves nothing."""
    rows = [row for worker in workers for row in worker]
    if any(row["outcome"] == HTTP_429 for row in rows):
        return SHARED
    if all(len(worker) == requests for worker in workers) and all(
            row["outcome"] == OK for row in rows):
        return NOT_SHARED
    return INCONCLUSIVE


def _counts(rows: Sequence[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in rows:
        counts[row["outcome"]] = counts.get(row["outcome"], 0) + 1
    return counts


def _launch(commands: list[list[str]]) -> list[int]:
    processes = [subprocess.Popen(command) for command in commands]  # noqa: S603 - our own argv
    return [process.wait() for process in processes]


def chart_limit(cfg: Config, config_path: Path, symbol: str, *, now: Clock,
                launch: Launch = _launch) -> tuple[str, Path]:
    _require_symbol(symbol)
    _, lock_path = data_paths(cfg)
    pid = recorded_holder(lock_path)
    if pid is not None:
        raise ProbeRefused(f"a bot process (pid {pid}) is recorded in the lock; this probe "
                           "sends more than one process may and has no place in its budget")
    _refuse_in_regular_session(cfg, now())
    probe = cfg.probe
    limit = budget_from_config(cfg).limit_for(Route.BARS.path)
    if probe.chart_limit_requests > limit.max_requests:
        raise ProbeRefused(f"[probe] chart_limit_requests {probe.chart_limit_requests} is above "
                           f"the bars route limit {limit.max_requests}")
    total = probe.chart_limit_processes * probe.chart_limit_requests
    if total <= limit.max_requests:
        raise ProbeRefused(f"{total} requests in all do not pass the bars route limit "
                           f"{limit.max_requests}: the probe would prove nothing")
    started = now()
    folder = evidence_folder(cfg, "chart-limit", started)
    start_at = started + timedelta(seconds=probe.chart_limit_start_delay_seconds)
    files = [folder / f"worker-{index}.jsonl" for index in range(probe.chart_limit_processes)]
    codes = launch([[sys.executable, "-m", "aperture.probes", "--config", str(config_path),
                     "chart-limit-worker", symbol, "--out", str(out),
                     "--start-at", start_at.isoformat()] for out in files])
    workers = [_rows(out) for out in files]
    verdict = limit_verdict(workers, probe.chart_limit_requests)
    (folder / SUMMARY_FILE).write_text(json.dumps({
        "probe": "chart-limit", "environment": cfg.environment_name, "symbol": symbol,
        "started_at": started.isoformat(), "window_start": start_at.isoformat(),
        "route_limit": limit.max_requests, "window_seconds": limit.window_seconds,
        "requests_per_process": probe.chart_limit_requests,
        "workers": [{"exit_code": code, "outcomes": _counts(rows)}
                    for code, rows in zip(codes, workers, strict=True)],
        "verdict": verdict,
    }, indent=1, sort_keys=True) + "\n", "utf-8")
    return verdict, folder


# --------------------------------------------------------------------------------------------
# chart-client (C1)
# --------------------------------------------------------------------------------------------


def token_fingerprint(folder: Path) -> list[tuple[int, int]]:
    """Size and modification time of every file in the token folder. Never a name or content."""
    if not folder.is_dir():
        return []
    stats = [path.stat() for path in folder.rglob("*") if path.is_file()]
    return sorted((stat.st_size, stat.st_mtime_ns) for stat in stats)


def bot_health(cfg: Config, at: datetime) -> dict[str, Any]:
    """What the bot's own status file says about its broker reads right now."""
    path = cfg.root / cfg.run.data_dir / cfg.api.status_file
    try:
        status = api.read_status(path)
    except (OSError, api.ContractError) as exc:
        return {"readable": False, "type": type(exc).__name__}
    last_read = status.freshness.last_broker_read_at
    return {
        "readable": True,
        "status_age_seconds": round((at - status.written_at).total_seconds()),
        "last_broker_read_at": None if last_read is None else last_read.isoformat(),
        "broker_read_failed": any(fence.kind.split(":", 1)[0] == Fence.BROKER_READ
                                  for fence in status.fences),
    }


def _refuse_while_exposed(cfg: Config, at: datetime) -> None:
    """Only next to a bot whose fresh status shows no position and no working order of its own:
    if the second client costs the bot its broker reads, nothing of ours is then at stake."""
    path = cfg.root / cfg.run.data_dir / cfg.api.status_file
    try:
        status = api.read_status(path)
    except (OSError, api.ContractError) as exc:
        raise ProbeRefused(f"the bot's status file cannot be read ({type(exc).__name__}), so "
                           "it is unknown whether a trade is open") from exc
    age = (at - status.written_at).total_seconds()
    if age > cfg.api.status_stale_seconds:
        raise ProbeRefused(f"the bot's status is {round(age)} s old, so it is unknown whether "
                           "a trade is open")
    block = status.entry_block
    if status.positions or status.position is not None or (
            block is not None and block.cause == api.OWN_TRADE_OPEN):
        raise ProbeRefused("the bot has an open position or a working order of its own; run "
                           "this probe while it is flat")
    # The status hides a working entry behind an earlier entry block (after the last-entry
    # time, say), so the bot's last broker read has to agree.
    try:
        account = api.read_account(cfg.root / cfg.run.data_dir / cfg.api.account_file)
    except (OSError, api.ContractError) as exc:
        raise ProbeRefused(f"the bot's account file cannot be read ({type(exc).__name__}), so "
                           "it is unknown whether a trade is open") from exc
    age = (at - account.read_at).total_seconds()
    if age > cfg.api.account_stale_seconds:
        raise ProbeRefused(f"the bot's last broker read is {round(age)} s old, so it is "
                           "unknown whether a trade is open")
    if account.positions or any(order.ours for order in account.orders):
        raise ProbeRefused("the broker shows a position or a working order of the bot's; run "
                           "this probe while it is flat")


def client_problems(rows: Sequence[dict[str, Any]], token_changed: bool,
                    status_stale_seconds: Decimal) -> list[str]:
    """Every reason the run does not show a second client living next to the bot."""
    problems = []
    refused = [row["seq"] for row in rows if row["outcome"] != OK]
    if refused:
        problems.append(f"probe reads not answered: {refused}")
    if token_changed:
        problems.append("the token folder changed during the run")
    for row in rows:
        bot = row["bot"]
        if not bot["readable"]:
            problems.append(f"read {row['seq']}: the bot's status file was unreadable")
        elif bot["status_age_seconds"] > status_stale_seconds:
            problems.append(f"read {row['seq']}: the bot's status was "
                            f"{bot['status_age_seconds']} s old")
        elif bot["broker_read_failed"]:
            problems.append(f"read {row['seq']}: the bot reports failing broker reads")
    reads = [row["bot"].get("last_broker_read_at") for row in rows]
    if reads and (reads[0] is None or reads[-1] is None or
                  datetime.fromisoformat(reads[-1]) <= datetime.fromisoformat(reads[0])):
        problems.append("the bot's last broker read did not move during the run")
    return problems


def chart_client(cfg: Config, config_path: Path, symbol: str, *, now: Clock, sleep: Sleep,
                 market: Callable[[Config], MarketData] = chart_market_data_from_config,
                 ) -> tuple[str, Path]:
    _require_symbol(symbol)
    _, lock_path = data_paths(cfg)
    if recorded_holder(lock_path) is None:
        raise ProbeRefused("no bot process is recorded in the lock; this probe is about a "
                           "second client next to the running bot")
    if not cfg.chart.webull_enabled:
        raise ProbeRefused("[chart] webull_enabled is false: the bot keeps the whole bars "
                           "limit, so these reads would not be inside any budget")
    if config_path.stat().st_mtime_ns > lock_path.stat().st_mtime_ns:
        raise ProbeRefused(f"{config_path} changed after the bot started; the running bot may "
                           "not have given up the chart's slice of the bars limit")
    _refuse_while_exposed(cfg, now())
    probe = cfg.probe
    if probe.chart_client_reads <= 1:
        raise ProbeRefused("[probe] chart_client_reads must be above 1")
    source = market(cfg)  # its own market-data-only client and the chart's own budget
    token_folder = cfg.root / cfg.run.data_dir / cfg.environment.token_dir
    token_before = token_fingerprint(token_folder)
    started = now()
    folder = evidence_folder(cfg, "chart-client", started)
    out = folder / "reads.jsonl"
    for seq in range(probe.chart_client_reads):
        _wait_until(started + timedelta(seconds=seq * probe.chart_client_interval_seconds),
                    now, sleep)
        sent_at = now()
        answer = read_once(source, symbol, sent_at)
        _append(out, {"seq": seq, "sent_at": sent_at.isoformat(), **answer,
                      "bot": bot_health(cfg, now())})
    rows = _rows(out)
    token_after = token_fingerprint(token_folder)
    problems = client_problems(rows, token_after != token_before,
                               cfg.api.status_stale_seconds)
    verdict = FAILED if problems else PASSED
    (folder / SUMMARY_FILE).write_text(json.dumps({
        "probe": "chart-client", "environment": cfg.environment_name, "symbol": symbol,
        "started_at": started.isoformat(), "finished_at": now().isoformat(),
        "outcomes": _counts(rows),
        "token_files": {"before": len(token_before), "after": len(token_after),
                        "changed": token_after != token_before},
        "problems": problems, "verdict": verdict,
    }, indent=1, sort_keys=True) + "\n", "utf-8")
    return verdict, folder


# --------------------------------------------------------------------------------------------
# Command line
# --------------------------------------------------------------------------------------------

# Replaced in tests with fakes.
NOW: Clock = lambda: datetime.now(UTC)  # noqa: E731
SLEEP: Sleep = time.sleep
LIMIT_MARKET: Callable[[Config], MarketData] = chart_probe_market_data_from_config
CLIENT_MARKET: Callable[[Config], MarketData] = chart_market_data_from_config


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m aperture.probes", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--config", type=Path, default=Path("config/bot.toml"))
    sub = parser.add_subparsers(dest="command", required=True)
    for name, text in (("chart-limit", "C2: is the bars limit shared between processes? "
                                       "Only with the bot stopped"),
                       ("chart-client", "C1: a second client next to the running bot")):
        sub.add_parser(name, help=text).add_argument("symbol")
    worker = sub.add_parser("chart-limit-worker", help="one process of chart-limit (internal)")
    worker.add_argument("symbol")
    worker.add_argument("--out", type=Path, required=True)
    worker.add_argument("--start-at", type=datetime.fromisoformat, required=True)
    args = parser.parse_args(argv)
    try:
        cfg = config_module.load(args.config)
        if args.command == "chart-limit-worker":
            limit = budget_from_config(cfg).limit_for(Route.BARS.path)
            run_limit_worker(LIMIT_MARKET(cfg), args.symbol, cfg.probe.chart_limit_requests,
                             limit.window_seconds, args.start_at, args.out,
                             now=NOW, sleep=SLEEP)
            return EXIT_ANSWERED
        if args.command == "chart-limit":
            verdict, folder = chart_limit(cfg, args.config, args.symbol, now=NOW)
        else:
            verdict, folder = chart_client(cfg, args.config, args.symbol, now=NOW, sleep=SLEEP,
                                           market=CLIENT_MARKET)
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return EXIT_CONFIG
    except (ProbeRefused, BrokerError) as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return EXIT_REFUSED
    print(f"{args.command}: {verdict}; evidence in {folder}")
    return EXIT_ANSWERED if verdict in (SHARED, NOT_SHARED, PASSED) else EXIT_NO_ANSWER


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
