"""Feed-arrival measurement: recorder with a fake stream and fake market data, report against a
small synthetic flat file. No network, no credentials (canary values only)."""

from __future__ import annotations

import datetime
import gzip
import json
import threading
import time
from pathlib import Path

import model_bars
import pytest

from tickr import cli, config
from tickr.calendar import SessionCalendar
from tickr.data.base import FakeMarketData, ScreenerKind, ScreenerRow, ScreenerSession
from tickr.model import arrival, massive

ROOT = Path(__file__).resolve().parents[2]
CFG = config.load(ROOT / "config" / "bot.toml")
CAL = SessionCalendar.load((ROOT / CFG.session.calendar_file).parent)
DAY = CAL.day(datetime.date(2026, 11, 18))
NS = 10**9
OFFSET = 7 * NS  # monotonic = wall - OFFSET in these tests


@pytest.fixture
def creds(monkeypatch):
    monkeypatch.setenv(CFG.massive.api_key_variable, "api-canary-1")


def ns(t: datetime.datetime) -> int:
    return int(t.timestamp()) * NS


def row(kind, rank, symbol):
    return ScreenerRow(kind, rank, f"id-{symbol}", symbol, "NAS", "USD", None)


class Clock:
    def __init__(self, start: int) -> None:
        self.now = start

    def wall(self) -> int:
        return self.now

    def mono(self) -> int:
        return self.now - OFFSET


class Socket:
    def __init__(self, batches) -> None:
        self.incoming = [json.dumps(b) for b in batches]
        self.drained = threading.Event()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def send(self, text):
        pass

    def recv(self, timeout):
        if self.incoming:
            return self.incoming.pop(0)
        self.drained.set()
        time.sleep(0.001)
        raise TimeoutError


def test_record_keeps_every_message_and_the_lists_at_the_bot_cadence(tmp_path, creds):
    cfg = config.load(ROOT / "config" / "bot.toml", root=tmp_path)
    start = DAY.extended_close - datetime.timedelta(seconds=90)
    clock = Clock(ns(start))
    bar = {"ev": "AM", "sym": "AAA", "v": 10, "o": 1.0, "c": 1.1, "h": 1.2, "l": 0.9,
           "s": ns(start) // 10**6 - 60000}
    fix = dict(bar, c=1.15)  # a correction of the same symbol-minute
    sock = Socket([[{"ev": "status", "status": "auth_success"}], [bar], [fix]])
    market = FakeMarketData()
    for kind in ScreenerKind:
        market.set_screener(kind, [row(kind, 1, "AAA"), row(kind, 2, "BBB")])
    market.limit("screener", 4)  # the second round is cut short by the budget

    def wait(seconds):
        sock.drained.wait(5)  # the stream's messages are in before the clock moves
        clock.now += int(seconds * NS)

    said = []
    code = arrival.record(cfg, DAY, market, threading.Event(), said.append,
                          connect=lambda url, max_size: sock, wall_ns=clock.wall,
                          mono_ns=clock.mono, wait=wait)
    assert code == 0
    [path] = arrival.day_captures(cfg, "2026-11-18")
    recs = list(arrival._lines(path))
    kinds = [r["t"] for r in recs]
    assert kinds[0] == "start" and kinds[-1] == "stop" and recs[-1]["cause"] == "end"
    assert [r["msg"] for r in recs if r["t"] == "bar"] == [bar, fix]  # both, as sent
    lists = [r for r in recs if r["t"] == "list"]
    # the post-market's lists: one round at the first pass, the next one a minute later
    assert [(r["kind"], r["session"]) for r in lists[:3]] == [
        (k.value, "POST") for k in ScreenerKind]
    assert lists[0]["rows"] == [[1, "AAA", "id-AAA"], [2, "BBB", "id-BBB"]]
    assert all(r["w0"] <= r["w1"] for r in lists)
    assert market.calls[0] == ("screener", ("GAINERS", "POST"))
    [cut] = [r for r in recs if r["t"] == "list_skipped"]
    assert cut["cause"] == "budget" and cut["kinds"] == ["LOSERS", "TOP_ACTIVES"]
    # the run ends at the extended close plus the tail
    end = DAY.extended_close + datetime.timedelta(seconds=CFG.massive.arrival_tail_seconds)
    assert clock.now == ns(end)


def test_lists_follow_the_session_and_the_minute(creds):
    assert arrival.list_session(DAY, DAY.regular_open) is ScreenerSession.REGULAR
    assert arrival.list_session(DAY, DAY.premarket_open) is ScreenerSession.PRE
    assert arrival.list_session(DAY, DAY.regular_close) is ScreenerSession.POST
    assert arrival.list_session(DAY, DAY.extended_close) is None


def test_a_missing_key_refuses_by_name_before_any_broker_read(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv(CFG.massive.api_key_variable, raising=False)
    monkeypatch.setattr(cli, "NOW", lambda: DAY.regular_open)
    monkeypatch.setattr(cli, "PROBE_ADAPTERS", lambda cfg: pytest.fail("no broker read"))
    assert cli.main(["--config", str(ROOT / "config" / "bot.toml"),
                     "probe-feed-arrival"]) == 3
    assert CFG.massive.api_key_variable in capsys.readouterr().err


def test_a_refused_login_ends_the_run_with_its_cause(tmp_path, creds):
    cfg = config.load(ROOT / "config" / "bot.toml", root=tmp_path)
    clock = Clock(ns(DAY.regular_open))
    sock = Socket([[{"ev": "status", "status": "auth_failed"}]])
    said = []
    code = arrival.record(cfg, DAY, FakeMarketData(), threading.Event(), said.append,
                          connect=lambda url, max_size: sock, wall_ns=clock.wall,
                          mono_ns=clock.mono, wait=lambda s: time.sleep(0.001))
    assert code == 3
    recs = list(arrival._lines(arrival.day_captures(cfg, "2026-11-18")[0]))
    assert recs[-2]["t"] == "refused" and recs[-2]["cause"] == "stream_refused"


# --- the report -------------------------------------------------------------------------------


def am(r) -> dict:
    return {"ev": "AM", "sym": r.ticker, "v": int(r.volume), "o": r.open, "c": r.close,
            "h": r.high, "l": r.low, "s": int(r.window_start) // 10**6}


def write_flat(frame, path: Path) -> Path:
    path.write_bytes(gzip.compress(frame.to_csv(index=False).encode()))
    return path


def scenario(tmp_path):
    """A capture of the synthetic day: every bar 1.5 s after its minute, except late ones
    (+7 s), never-streamed ones, a 5-minute connection gap at 11:00, one correction, one bar
    the file does not hold, one bar of another day, and two list reads."""
    day, frame = model_bars.market(1)[0]
    assert day == "2026-11-18"
    # prices as a day file prints them (pandas' default csv reader does not round-trip 17 digits)
    frame = frame.round({k: 4 for k in ("open", "high", "low", "close")})
    flat = write_flat(frame, tmp_path / "flat.csv.gz")
    clock = Clock(ns(DAY.premarket_open) - 60 * NS)
    cap = arrival.Capture(tmp_path / "arrival-035900.jsonl.gz", 1, clock.wall, clock.mono)
    cap.write("start", {})
    t11 = ns(DAY.regular_open.replace(hour=11, minute=0))
    gap_lo, gap_hi = t11, t11 + 5 * 60 * NS
    rows = frame.sort_values("window_start", kind="stable").reset_index(drop=True)
    sent, expect = [], {"late": 0, "never": 0, "gap": 0, "regular": 0}
    corrected = None
    for i, r in rows.iterrows():
        end = int(r.window_start) + 60 * NS
        regular = ns(DAY.regular_open) <= int(r.window_start) < ns(DAY.regular_close)
        expect["regular"] += regular
        if gap_lo <= end < gap_hi:
            expect["gap"] += regular
            continue
        near_gap = gap_lo - 60 * NS <= end < gap_hi + 60 * NS
        if regular and i % 50 == 1 and not near_gap:
            expect["never"] += 1
            continue
        late = regular and i % 50 == 0
        expect["late"] += late
        sent.append((end + (7 if late else 1.5) * NS, am(r)))
        if regular and corrected is None and not late:
            corrected = r
            sent.append((end + 30 * NS, dict(am(r), c=float(r.close) + 1)))
    stray = int(rows.window_start.iloc[0]) + 600 * 60 * NS
    sent.append((stray + 61 * NS, {"ev": "AM", "sym": "ZZZZ", "v": 1, "o": 1, "c": 1, "h": 1,
                                   "l": 1, "s": stray // 10**6}))
    sent.append((stray, {"ev": "AM", "sym": "AAA", "v": 1, "o": 1, "c": 1, "h": 1, "l": 1,
                         "s": stray // 10**6 + 86400000}))
    sent.sort(key=lambda x: x[0])
    gap_written = False
    for at, msg in sent:
        if not gap_written and at >= gap_hi:
            clock.now = gap_hi - 2 * NS
            cap.write("gap", {"down": gap_lo - OFFSET, "up": gap_hi - 3 * NS - OFFSET,
                              "cause": "ConnectionClosed"})
            gap_written = True
        clock.now = int(at)
        cap.write("bar", {"msg": msg}, mono=clock.mono())
    # list reads at 10:30:03: one member traded at 10:29, one did not, one is not in the file
    t1030 = ns(DAY.regular_open.replace(hour=10, minute=30)) + 3 * NS
    s1029 = (t1030 // NS // 60 - 1) * 60 * NS
    at_1029 = set(rows[rows.window_start == s1029].ticker)
    traded = sorted(at_1029)[0]
    quiet = sorted(set(rows.ticker) - at_1029)[0]
    clock.now = t1030 + NS // 5
    cap.write("list", {"kind": "GAINERS", "session": "REGULAR", "w0": t1030, "m0": 0,
                       "w1": clock.now, "m1": 0,
                       "rows": [[1, traded, "i1"], [2, quiet, "i2"], [3, "NOPE", "i3"]]})
    cap.write("list_skipped", {"kinds": ["LOSERS"], "session": "REGULAR", "cause": "budget",
                               "w0": clock.now, "m0": 0})
    clock.now = ns(DAY.extended_close) + 300 * NS
    cap.write("stop", {"cause": "end"})
    cap.close()
    return cap.path, flat, expect, corrected


def test_the_report_keeps_present_late_gap_and_never_apart(tmp_path):
    path, flat, expect, corrected = scenario(tmp_path)
    frame = massive.read_day_file(flat.read_bytes(), "2026-11-18")
    cap = arrival.read_captures([path])
    result = arrival.report(cap, frame, DAY, CFG.massive.arrival_report_points_seconds)
    reg = result["sessions"]["regular"]
    by = {p["seconds"]: p for p in reg["points"]}
    assert by[5.0]["reference"] == expect["regular"]
    assert by[5.0]["late"] == expect["late"] and by[10.0]["late"] == 0
    assert by[5.0]["gap"] == expect["gap"] > 0 and reg["lost_in_gaps"] == expect["gap"]
    assert by[5.0]["never"] == expect["never"] > 0 and reg["never_streamed"] == expect["never"]
    assert by[1.0]["present"] == 0 and by[2.0]["present"] == (
        expect["regular"] - expect["late"] - expect["gap"] - expect["never"])
    assert reg["delay"]["p50"] == pytest.approx(1.5)
    assert reg["corrections"] == 1 and reg["corrections_changed"] == 1
    assert reg["first_differs_from_file"] == 0 and reg["last_differs_from_file"] == 1
    assert reg["stream_only"] == 1 and result["other_day"] == 1
    assert reg["gaps"] == 1
    # the gap lasts until data flowed again: the first bar after the reconnect
    [(lo, hi, cause)] = result["outages"]
    assert cause == "ConnectionClosed" and (hi - lo) / NS == pytest.approx(301.5)
    li = reg["lists"]
    assert (li["reads"], li["skipped"], li["members"]) == (1, {"budget": 1}, 3)
    assert (li["present"], li["late"], li["never"], li["untraded"], li["not_in_file"]) == (
        1, 0, 0, 1, 1)
    assert li["latency"]["max"] == pytest.approx(0.2)
    for name in ("pre", "post"):
        s = result["sessions"][name]
        assert s["points"][1]["present"] == s["points"][1]["reference"] > 0
    assert any("[regular]" in line for line in arrival.render(result))


def test_a_killed_run_is_read_to_its_last_flush(tmp_path):
    clock = Clock(ns(DAY.regular_open))
    cap = arrival.Capture(tmp_path / "arrival-093000.jsonl.gz", 0, clock.wall, clock.mono)
    cap.write("start", {})
    cap.write("bar", {"msg": {"sym": "AAA", "s": 1, "v": 1, "o": 1, "c": 1, "h": 1,
                              "l": 1}})
    cap._gz.flush()
    cap._raw.flush()
    data = cap.path.read_bytes()  # what is on disk when the process dies here
    cut = tmp_path / "arrival-cut.jsonl.gz"
    cut.write_bytes(data)
    read = arrival.read_captures([cut])
    assert read.truncated == 1 and len(read.received) == 1


def test_the_report_command_prints_per_session(tmp_path, capsys):
    path, flat, _, _ = scenario(tmp_path)
    assert cli.main(["--config", str(ROOT / "config" / "bot.toml"), "report-feed-arrival",
                     "2026-11-18", "--capture", str(path), "--flat-file", str(flat)]) == 0
    out = capsys.readouterr().out
    assert "[pre]" in out and "[regular]" in out and "[post]" in out and "gap " in out


def test_a_day_without_a_capture_is_refused(tmp_path, capsys):
    toml = tmp_path / "config" / "bot.toml"
    toml.parent.mkdir()
    toml.write_text((ROOT / "config" / "bot.toml").read_text())
    (tmp_path / "config" / "calendar").symlink_to(ROOT / "config" / "calendar")
    assert cli.main(["--config", str(toml), "report-feed-arrival", "2026-11-18"]) == 3
    assert "capture_missing" in capsys.readouterr().err
