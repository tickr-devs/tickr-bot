"""strategy.composed and ignition-v1: a strategy that is only its strategy.toml."""

from __future__ import annotations

import copy
import tomllib
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from strategy_bars import SYMBOL, TODAY, bar, d, minute, quote

from tickr import config
from tickr.observers.loader import load_strategy, parse_modules
from tickr.strategy import composed
from tickr.strategy.base import PositionView, SymbolContext, Verdict

ROOT = Path(__file__).resolve().parents[2]
VALUES = tomllib.loads((ROOT / "strategies" / "ignition-v1" / "strategy.toml").read_text())


def make(values=None):
    return composed.create(copy.deepcopy(VALUES if values is None else values))


def flat(count, price=1.00, volume=10_000):
    return [bar(TODAY, i, price, price, price, price, volume) for i in range(count)]


def ignition(count=20, low=1.00, close=1.20, volume=10_000):
    """Quiet at `low`, then one bar at the last index closing at `close`, a new day high
    20% over the low."""
    bars = flat(count - 1, low, volume)
    bars.append(bar(TODAY, count - 1, low, close, low, close, volume))
    return bars


OPEN = minute(TODAY, 0)


def ctx(bars, q=None, position=None, session_open=OPEN):
    return SymbolContext(SYMBOL, bars, (), q, position, session_open=session_open)


def rules(decision):
    return [(c.rule, c.passed) for c in decision.checks]


def test_the_shipped_ignition_file_loads_through_the_module_map():
    cfg = config.load(ROOT / "config" / "bot.toml")
    loaded = load_strategy(ROOT, "ignition-v1", parse_modules(cfg.observers.strategy_modules))
    assert loaded.identity == "ignition-v1 v1"
    assert [n for n, _ in loaded.strategy.signals] == [
        "minutes_after_open", "new_session_high", "rise_from_low"]


def test_ignition_enters_at_the_ask_with_the_stop_twenty_percent_under():
    decision = make().evaluate(ctx(ignition(), quote("1.20", "1.21")))
    assert decision.verdict is Verdict.ENTER, rules(decision)
    assert decision.entry_high == d("1.20") * d("1.02")
    assert decision.stop == min(decision.entry_high * d("0.8"), d("1.20") * d("0.8"))
    assert decision.quantity == (Decimal(1000) / decision.entry_high).to_integral_value(
        rounding="ROUND_FLOOR")
    assert decision.setup == "ignition-v1"


def test_without_a_quote_the_signal_is_watched():
    assert make().evaluate(ctx(ignition())).verdict is Verdict.WATCH


@pytest.mark.parametrize("bars, failed", [
    (ignition(count=4), "minutes_after_open"),                  # 09:33 bar: too early
    (ignition(close=1.05), "rise_from_low_percent"),           # 5% over the low
    (ignition(volume=100_000), "dollar_volume_max_usd"),       # $2M traded already
])
def test_each_block_refuses_on_its_own(bars, failed):
    decision = make().evaluate(ctx(bars, quote("1.20", "1.21")))
    assert decision.verdict is Verdict.IGNORE
    assert decision.decisive.rule == failed


def test_not_a_new_day_high_is_ignored():
    bars = ignition()
    bars[5] = bar(TODAY, 5, 1.00, 1.30, 1.00, 1.00, 10_000)  # an earlier high above 1.20
    decision = make().evaluate(ctx(bars, quote("1.20", "1.21")))
    assert decision.decisive.rule == "new_session_high"


def test_the_low_is_taken_only_over_the_window():
    bars = ignition(count=60)
    for i in range(29):  # the last 30 minutes sit at 1.15: only 4% under the close
        bars[30 + i] = bar(TODAY, 30 + i, 1.15, 1.15, 1.15, 1.15, 1_000)
    bars[-1] = bar(TODAY, 59, 1.15, 1.20, 1.15, 1.20, 1_000)
    decision = make().evaluate(ctx(bars, quote("1.20", "1.21")))
    assert decision.decisive.rule == "rise_from_low_percent"


def test_a_session_without_the_0930_bar_is_decided_when_the_read_reached_the_open():
    bars = ignition()[3:]  # no trade in the first three minutes: no bars for them
    decision = make().evaluate(ctx(bars, quote("1.20", "1.21")))
    assert decision.verdict is Verdict.ENTER, rules(decision)


def test_bars_not_proven_to_reach_the_open_are_refused():
    decision = make().evaluate(ctx(ignition(), quote("1.20", "1.21"), session_open=None))
    assert decision.verdict is Verdict.IGNORE
    assert decision.decisive.rule == "session_whole"


def test_minutes_after_open_counts_the_clock_not_the_bars():
    bars = [bar(TODAY, i, 1.00, 1.00, 1.00, 1.00, 10_000) for i in (2, 3)]
    bars.append(bar(TODAY, 4, 1.00, 1.20, 1.00, 1.20, 10_000))  # 09:34: 4 minutes in, 3 bars
    decision = make().evaluate(ctx(bars, quote("1.20", "1.21")))
    assert decision.decisive.rule == "minutes_after_open"
    later = [bar(TODAY, i, 1.00, 1.00, 1.00, 1.00, 10_000) for i in (2, 3)]
    later.append(bar(TODAY, 5, 1.00, 1.20, 1.00, 1.20, 10_000))  # 09:35: 5 minutes in
    assert make().evaluate(ctx(later, quote("1.20", "1.21"))).verdict is Verdict.ENTER


def test_the_trailing_exit_fires_twenty_percent_under_the_high_since_the_fill():
    bars = ignition()
    filled = bars[-1].start + timedelta(minutes=1)
    bars += [bar(TODAY, 20, 1.20, 2.00, 1.20, 1.90, 10_000),   # runs to 2.00
             bar(TODAY, 21, 1.90, 1.90, 1.70, 1.70, 10_000)]   # 15% off: hold
    position = PositionView(d("1.21"), d(800), d("0.96"), filled_at=filled)
    strategy = make()
    assert strategy.evaluate(ctx(bars, position=position)).verdict is Verdict.HOLD
    bars.append(bar(TODAY, 22, 1.70, 1.70, 1.59, 1.59, 10_000))  # 20.5% off the 2.00 high
    decision = strategy.evaluate(ctx(bars, position=position))
    assert decision.verdict is Verdict.EXIT and decision.checks[-1].rule == "trailing_stop"


def test_the_bid_alone_can_fire_the_trail():
    bars = ignition()
    position = PositionView(d("1.20"), d(800), d("0.96"), filled_at=bars[-1].start)
    decision = make().evaluate(ctx(bars, quote("0.95", "0.97"), position))
    assert decision.verdict is Verdict.EXIT


def test_a_broken_book_never_fires_the_trail_the_last_close_decides():
    bars = ignition()  # last close 1.20 at its high
    position = PositionView(d("1.20"), d(800), d("0.96"), filled_at=bars[-1].start)
    decision = make().evaluate(ctx(bars, quote("0.50", "1.21"), position))
    assert decision.verdict is Verdict.HOLD, rules(decision)
    assert decision.checks[-1].note.startswith("last close (bid not used: spread ratio")


def test_an_exit_on_the_close_with_a_wide_book_names_the_trail_as_decisive():
    bars = ignition()
    bars.append(bar(TODAY, 20, 1.20, 1.20, 0.90, 0.90, 10_000))  # 25% under 1.20
    position = PositionView(d("1.20"), d(800), d("0.96"), filled_at=bars[-2].start)
    decision = make().evaluate(ctx(bars, quote("0.50", "1.21"), position))
    assert decision.verdict is Verdict.EXIT
    assert all(c.passed for c in decision.checks if c.rule != "trailing_stop")
    assert decision.checks[-1].rule == "trailing_stop" and decision.checks[-1].passed


def test_without_a_quote_the_last_close_decides():
    bars = ignition()
    position = PositionView(d("1.20"), d(800), d("0.96"), filled_at=bars[-1].start)
    decision = make().evaluate(ctx(bars, None, position))
    assert decision.verdict is Verdict.HOLD
    assert decision.checks[-1].note.startswith("last close (bid not used: no valid fresh quote)")


def test_without_a_fill_time_the_high_counts_from_the_first_bar_seen():
    bars = ignition()
    strategy = make()
    position = PositionView(d("1.20"), d(800), d("0.96"))
    assert strategy.evaluate(ctx(bars, position=position)).verdict is Verdict.HOLD
    bars.append(bar(TODAY, 20, 1.20, 1.50, 1.20, 1.50, 10_000))
    bars.append(bar(TODAY, 21, 1.50, 1.50, 1.19, 1.19, 10_000))  # 20.7% under 1.50
    assert strategy.evaluate(ctx(bars, position=position)).verdict is Verdict.EXIT


def test_a_new_ignition_after_the_exit_enters_again():
    strategy = make()
    first = ignition()
    assert strategy.evaluate(ctx(first, quote("1.20", "1.21"))).verdict is Verdict.ENTER
    later = first + [bar(TODAY, 20 + i, 1.10, 1.10, 1.00, 1.10, 10_000) for i in range(10)]
    later.append(bar(TODAY, 30, 1.10, 1.40, 1.10, 1.40, 10_000))  # a new high 40% up
    assert strategy.evaluate(ctx(later, quote("1.40", "1.41"))).verdict is Verdict.ENTER


@pytest.mark.parametrize("change, message", [
    (lambda v: v["signal"].append({"block": "moon_phase"}), "needs block"),
    (lambda v: v["signal"][0].update(extra=1), "unknown keys"),
    (lambda v: v["exit"][0].pop("percent"), "missing keys"),
    (lambda v: v["stop"].update(percent=100), "below 100"),
    (lambda v: v["liquidity"].update(min_session_dollar_volume_usd=900000), "floor"),
    (lambda v: v.pop("sizing"), "missing"),
])
def test_a_file_naming_what_the_module_cannot_do_is_refused(change, message):
    values = copy.deepcopy(VALUES)
    change(values)
    with pytest.raises(ValueError, match=message):
        make(values)
