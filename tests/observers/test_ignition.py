"""Observer "early thin ignition" (observers/ignition.py): signal from screener rows, paper
trade on quotes, records, resume; and its wiring into discovery and the stream."""

from __future__ import annotations

import json
import tomllib
from dataclasses import replace
from datetime import datetime, timedelta
from decimal import Decimal as D
from pathlib import Path

import pytest

from tickr import config
from tickr.calendar import NEW_YORK, SessionCalendar
from tickr.data.base import ScreenerKind, ScreenerRow
from tickr.observers import ignition as ig
from tickr.types import Quote

ROOT = Path(__file__).resolve().parents[2]
CFG = config.load(ROOT / "config" / "bot.toml")
CALENDAR = SessionCalendar.load((ROOT / CFG.session.calendar_file).parent)
TOML = ROOT / "strategies" / "early-thin-ignition-v1" / "strategy.toml"
VALUES = tomllib.loads(TOML.read_text("utf-8"))
RULES = ig.Rules.from_values(VALUES)
T0 = datetime(2026, 11, 16, 10, 0, tzinfo=NEW_YORK)  # Monday
DAY = CALENDAR.day_at(T0)
SYM = "THIN"


def row(price: str, high: str, volume: str = "100000", low: str = "1.00",
        symbol: str = SYM) -> ScreenerRow:
    return ScreenerRow(ScreenerKind.GAINERS, 1, f"id-{symbol}", symbol, "NSQ", "USD",
                       "COMMON_STOCK", {"price": price, "high": high, "low": low,
                                        "volume": volume})


def quote(bid: str, ask: str, at: datetime, symbol: str = SYM) -> Quote:
    return Quote(symbol, D(bid), D(ask), D(100), D(100), at, at)


class World:
    def __init__(self, tmp_path: Path, rules: ig.Rules = RULES) -> None:
        self.log = ig.IgnitionLog(tmp_path / "decisions")
        self.obs = ig.Ignition(rules, self.log)

    def pass_(self, at: datetime, rows: list[ScreenerRow], quotes: list[Quote] = (),
              universe: set[str] | None = None) -> None:
        self.obs.screener(rows, "fake", at)
        self.obs.quotes(list(quotes), "fake", "snapshot")
        self.obs.after_pass(DAY, {r.symbol for r in rows} if universe is None else universe,
                            at)

    def records(self, kind: str | None = None) -> list[dict]:
        return [r for r in self.log.read(T0) if kind is None or r["record"] == kind]

    def ignite(self, at: datetime = T0, ask: str = "1.26", bid: str = "1.24") -> None:
        """A low of 1.00 ten minutes before `at`, then a new high 25% above it."""
        self.pass_(at - timedelta(minutes=10), [row("1.00", "1.05")])
        self.pass_(at, [row("1.25", "1.25", volume="200000")],
                   [quote(bid, ask, at)])


def test_the_shipped_values_load_and_bad_ones_are_refused():
    assert RULES.trails == (D("0.2"), D("0.1"), D("0.3"))
    assert RULES.max_dollar_volume == D(5000000) and RULES.low_window == timedelta(minutes=30)
    assert RULES.min_rise == D("0.1")
    for broken in ({**VALUES, "extra": {}},
                   {**VALUES, "exit": {**VALUES["exit"], "trail_percent": 100}},
                   {**VALUES, "exit": {**VALUES["exit"], "variant_trail_percents": [20]}},
                   {**VALUES, "signal": {**VALUES["signal"], "low_max_age_minutes": 31}},
                   {**VALUES, "signal": {**VALUES["signal"], "min_rise_percent": True}},
                   {**VALUES, "record": {}}):
        with pytest.raises(ig.IgnitionError):
            ig.Rules.from_values(broken)


def test_from_config_off_and_on():
    assert ig.from_config(replace(CFG, observers=replace(CFG.observers, ignition=""))) is None
    built = ig.from_config(CFG)
    assert built is not None and built.rules == RULES


def test_a_thin_new_high_25_percent_off_a_fresh_low_enters_at_the_ask(tmp_path):
    world = World(tmp_path)
    world.ignite()
    [signal] = world.records(ig.SIGNAL)
    assert signal["outcome"] == ig.ENTERED and signal["entry_price"] == "1.26"
    assert signal["rise_percent"] == "25.00" and signal["low"] == "1.00"
    assert signal["dollar_volume_usd"] == "225000"  # 100k x 1.00 + 100k more x 1.25
    assert signal["spread_percent"] == "1.60" and signal["minutes_since_low"] == 10
    assert signal["lists"] == ["GAINERS"] and signal["ask_size"] == "100"
    assert world.obs.watching() == {SYM}


@pytest.mark.parametrize("case", ["not-at-high", "rise-too-small", "low-too-old",
                                  "too-much-volume", "before-0935", "after-1530",
                                  "not-in-universe"])
def test_no_signal_unless_every_rule_holds(tmp_path, case):
    world = World(tmp_path)
    at = T0
    low_at = at - timedelta(minutes=10)
    price, high, volume, universe = "1.25", "1.25", "200000", None
    if case == "not-at-high":
        high = "1.30"
    elif case == "rise-too-small":
        price = high = "1.09"
    elif case == "low-too-old":
        low_at = at - timedelta(minutes=31)  # left the 30-minute window
    elif case == "too-much-volume":
        volume = "4100000"  # 100k x 1.00 + 4M x 1.25 = 5.1M
    elif case == "before-0935":
        at = DAY.regular_open + timedelta(minutes=4)
        low_at = at - timedelta(minutes=3)
    elif case == "after-1530":
        at = DAY.before_close(29)
        low_at = at - timedelta(minutes=10)
    elif case == "not-in-universe":
        universe = set()
    world.pass_(low_at, [row("1.00", "1.05")])
    world.pass_(at, [row(price, high, volume=volume)], [quote("1.24", "1.26", at)],
                universe=universe)
    assert world.records(ig.SIGNAL) == []


def test_a_low_age_limit_below_the_window_is_kept(tmp_path):
    strict = replace(RULES, low_max_age=timedelta(minutes=20))
    world = World(tmp_path, strict)
    world.pass_(T0 - timedelta(minutes=21), [row("1.00", "1.05")])
    world.pass_(T0, [row("1.25", "1.25", volume="200000")], [quote("1.24", "1.26", T0)])
    assert world.records(ig.SIGNAL) == []


def test_a_new_day_low_seen_in_a_row_counts_as_the_low(tmp_path):
    world = World(tmp_path)
    world.pass_(T0 - timedelta(minutes=25), [row("1.10", "1.15", low="1.05")])
    # the price never sampled at 1.00, but the day low moved there 15 minutes ago
    world.pass_(T0 - timedelta(minutes=15), [row("1.08", "1.15", low="1.00")])
    world.pass_(T0, [row("1.25", "1.25", volume="150000", low="1.00")],
                [quote("1.24", "1.26", T0)])
    [signal] = world.records(ig.SIGNAL)
    assert signal["low"] == "1.00" and signal["outcome"] == ig.ENTERED


def test_a_signal_without_a_quote_or_while_open_dies_and_is_recorded(tmp_path):
    world = World(tmp_path)
    world.pass_(T0 - timedelta(minutes=10), [row("1.00", "1.05")])
    world.pass_(T0, [row("1.25", "1.25", volume="200000")])  # no quote this pass
    later = T0 + timedelta(minutes=1)
    world.pass_(later, [row("1.30", "1.30", volume="210000")], [quote("1.29", "1.31", later)])
    again = later + timedelta(minutes=1)
    world.pass_(again, [row("1.35", "1.35", volume="220000")], [quote("1.34", "1.36", again)])
    assert [s["outcome"] for s in world.records(ig.SIGNAL)] == [
        ig.DIED_NO_QUOTE, ig.ENTERED, ig.DIED_OPEN]


def test_each_trail_exits_at_the_bid_and_the_flat_ends_the_rest(tmp_path):
    world = World(tmp_path)
    world.ignite()
    t = T0
    for bid in ("1.50", "1.40", "1.34", "1.21", "1.19"):  # high bid 1.50
        t += timedelta(seconds=10)
        world.obs.on_quote(quote(bid, str(D(bid) + D("0.02")), t))
    exits = world.records(ig.EXIT)
    # 10%: 1.50 x 0.9 = 1.35 -> exits at 1.34; 20%: 1.20 -> exits at 1.19
    assert [(e["trail_percent"], e["exit_bid"], e["cause"], e["main"]) for e in exits] == [
        ("10", "1.34", ig.TRAIL, False), ("20", "1.19", ig.TRAIL, True)]
    assert exits[1]["result_percent"] == "-5.56"  # 1.19 / 1.26 - 1
    assert exits[1]["result_usd"] == "-55.56" and exits[1]["high_bid"] == "1.50"  # on $1,000
    assert exits[1]["quotes"] == {"stream": 5, "snapshot": 1}
    assert world.obs.watching() == {SYM}  # the 30% trail is still out
    flat = DAY.before_close(RULES.flat_minutes_before_close)
    world.obs.on_quote(quote("1.10", "1.12", flat))
    last = world.records(ig.EXIT)[-1]
    assert (last["trail_percent"], last["cause"], last["exit_bid"]) == ("30", ig.FLAT, "1.10")
    assert world.obs.watching() == set()


def test_old_or_invalid_quotes_are_ignored_and_a_pause_is_recorded(tmp_path):
    world = World(tmp_path)
    world.ignite()
    world.obs.on_quote(quote("0.50", "0.52", T0 - timedelta(seconds=1)))  # older
    world.obs.on_quote(quote("0.50", "0.40", T0 + timedelta(seconds=1)))  # crossed
    world.obs.on_quote(quote("0", "0.40", T0 + timedelta(seconds=2)))  # no bid
    assert world.records(ig.EXIT) == []
    world.obs.on_quote(quote("1.25", "1.27", T0 + timedelta(seconds=75)))
    [gap] = world.records(ig.GAP)
    assert gap["seconds"] == 75 and gap["source"] == "stream"


def test_a_new_entry_after_the_main_exit_ends_the_old_variants(tmp_path):
    world = World(tmp_path)
    world.ignite()
    world.obs.on_quote(quote("0.95", "0.97", T0 + timedelta(seconds=5)))  # 10% and 20% out
    at = T0 + timedelta(minutes=5)
    world.pass_(at, [row("1.60", "1.60", volume="230000")], [quote("1.59", "1.61", at)])
    signals = world.records(ig.SIGNAL)
    assert [s["outcome"] for s in signals] == [ig.ENTERED, ig.ENTERED]
    replaced = [e for e in world.records(ig.EXIT) if e["cause"] == ig.REPLACED]
    assert [e["trail_percent"] for e in replaced] == ["30"]


def test_after_the_flat_grace_the_last_bid_ends_a_quiet_trade(tmp_path):
    world = World(tmp_path)
    world.ignite()
    late = DAY.before_close(RULES.flat_minutes_before_close) + RULES.flat_grace
    world.pass_(late, [])
    exits = world.records(ig.EXIT)
    assert {e["cause"] for e in exits} == {ig.FLAT_LAST_BID} and len(exits) == 3
    assert {e["exit_bid"] for e in exits} == {"1.24"}
    assert world.obs.watching() == set()


def test_a_restart_takes_back_the_open_paper_trades(tmp_path):
    world = World(tmp_path)
    world.ignite()
    world.obs.on_quote(quote("1.10", "1.12", T0 + timedelta(seconds=5)))  # 10% out
    again = World(tmp_path)
    again.pass_(T0 + timedelta(minutes=1), [])
    assert again.obs.watching() == {SYM}
    again.obs.on_quote(quote("0.95", "0.97", T0 + timedelta(minutes=2)))  # 20% out (1.24 x .8)
    exits = again.records(ig.EXIT)
    assert [e["trail_percent"] for e in exits] == ["10", "20"]
    late = DAY.before_close(RULES.flat_minutes_before_close) + RULES.flat_grace
    fresh = World(tmp_path)
    fresh.pass_(late, [])  # a restart that saw no quote: no result, never a guessed one
    last = fresh.records(ig.EXIT)[-1]
    assert last["cause"] == ig.FLAT_NO_QUOTE and "result_percent" not in last


def test_records_are_one_json_object_per_line(tmp_path):
    world = World(tmp_path)
    world.ignite()
    lines = world.log.path(T0).read_text("utf-8").splitlines()
    assert all(isinstance(json.loads(line), dict) for line in lines)


def test_each_entry_records_how_many_paper_trades_are_open(tmp_path):
    world = World(tmp_path)
    world.ignite()
    other = "RUN"
    world.pass_(T0 - timedelta(minutes=5), [row("2.00", "2.05", symbol=other)])
    at = T0 + timedelta(minutes=1)
    world.pass_(at, [row("2.60", "2.60", volume="200000", symbol=other)],
                [quote("2.59", "2.61", at, symbol=other)])
    first, second = world.records(ig.SIGNAL)
    assert (first["open_paper_trades"], first["open_main_trails"]) == (1, 1)
    assert (second["open_paper_trades"], second["open_main_trails"]) == (2, 2)


def test_a_slow_record_write_never_holds_the_stream_thread(tmp_path):
    import threading

    world = World(tmp_path)
    world.ignite()  # THIN has an open paper trade
    entered, release = threading.Event(), threading.Event()
    write = world.log.write

    def slow(at, record):
        entered.set()
        assert release.wait(5)
        write(at, record)

    world.log.write = slow  # type: ignore[method-assign]
    gap = T0 + RULES.quote_gap + timedelta(seconds=1)  # a gap record: a write
    writer = threading.Thread(target=world.obs.on_quote, args=(quote("1.30", "1.32", gap),))
    writer.start()
    assert entered.wait(5)  # the gap record's write is hanging ...
    done = threading.Event()
    other = threading.Thread(target=lambda: (world.obs.on_quote(
        quote("1.31", "1.33", gap + timedelta(seconds=1))), done.set()))
    other.start()
    assert done.wait(5)  # ... and the next quote was handled all the same
    release.set()
    writer.join(5)
    other.join(5)
    assert [r["record"] for r in world.records(ig.GAP)] == [ig.GAP]
