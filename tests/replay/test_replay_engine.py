from __future__ import annotations

from dataclasses import replace
from datetime import time, timedelta
from decimal import Decimal

import pytest
from conftest import FLATTEN, LAST_ENTRY, at, enter, series

from aperture.replay.engine import ExitReason, SignalOutcome, replay
from aperture.replay.errors import ReplayError, StrategyContractError
from aperture.strategy.base import Decision, Verdict

D = Decimal
FLAT = ("10.00", "10.05", "9.95", "10.00")


def run(settings, strategy, bars, last_entry=LAST_ENTRY, flatten=FLATTEN):
    return replay(lambda: strategy, {"ABC": bars}, settings, last_entry, flatten)


def test_entry_at_top_of_window_plus_slippage_and_stop_exit(settings, scripted):
    bars = series(
        "10:00",
        [FLAT, ("10.05", "10.12", "10.02", "10.08"), ("10.00", "10.00", "9.70", "9.75")],
    )
    result = run(settings, scripted({0: enter("10.00", "10.10", "9.80")}), bars)
    (trade,) = result.trades
    assert trade.entry_price == D("10.10") + settings.slippage(D("10.10"))
    assert trade.entry_time == bars[1].start
    assert trade.exit_reason is ExitReason.STOP
    assert trade.exit_price == D("9.80") - settings.slippage(D("9.80"))
    assert trade.exit_time == bars[2].start
    assert trade.r_multiple < -1
    assert trade.return_percent < 0
    assert [s.outcome for s in result.signals] == [SignalOutcome.FILLED]


def test_gap_through_stop_exits_at_open(settings, scripted):
    bars = series(
        "10:00",
        [FLAT, ("10.05", "10.12", "10.02", "10.08"), ("9.50", "9.60", "9.40", "9.55")],
    )
    (trade,) = run(settings, scripted({0: enter("10.00", "10.10", "9.80")}), bars).trades
    assert trade.exit_price == D("9.50") - settings.slippage(D("9.50"))


def test_fill_and_stop_in_same_bar_assumes_stop(settings, scripted):
    bars = series("10:00", [FLAT, ("10.05", "10.12", "9.70", "9.90"), FLAT])
    (trade,) = run(settings, scripted({0: enter("10.00", "10.10", "9.80")}), bars).trades
    assert trade.exit_reason is ExitReason.STOP
    assert trade.entry_time == trade.exit_time == bars[1].start
    assert trade.bars_held == 1


def test_price_above_window_is_missed_never_chased(settings, scripted):
    above = ("10.50", "10.60", "10.40", "10.55")
    bars = series("10:00", [FLAT, above, above, FLAT])
    result = run(settings, scripted({0: enter("10.00", "10.10", "9.80")}), bars)
    assert result.trades == ()
    (signal,) = result.signals
    assert signal.outcome is SignalOutcome.MISSED
    assert signal.cause == "price outside window"


def test_entry_waits_configured_bars(settings, scripted):
    above = ("10.50", "10.60", "10.40", "10.55")
    bars = series("10:00", [FLAT, above, ("10.05", "10.08", "10.01", "10.05"), FLAT])
    result = run(settings, scripted({0: enter("10.00", "10.10", "9.80")}), bars)
    assert settings.entry_wait_bars == 2
    (trade,) = result.trades
    assert trade.entry_time == bars[2].start


def test_strategy_exit_fills_at_next_bar_open(settings, scripted):
    bars = series(
        "10:00",
        [
            FLAT,
            ("10.05", "10.12", "10.02", "10.08"),
            ("10.20", "10.30", "10.15", "10.25"),
            ("10.28", "10.30", "10.10", "10.12"),
        ],
    )
    strategy = scripted({0: enter("10.00", "10.10", "9.80"), 2: {"verdict": Verdict.EXIT}})
    (trade,) = run(settings, strategy, bars).trades
    assert trade.exit_reason is ExitReason.STRATEGY_EXIT
    assert trade.exit_time == bars[3].start
    assert trade.exit_price == D("10.28") - settings.slippage(D("10.28"))
    assert trade.r_multiple > 0
    assert trade.bars_held == 3


def test_flatten_time_closes_at_open(settings, scripted):
    rows = [FLAT] + [("10.05", "10.12", "10.02", "10.08")] * 5
    bars = series("15:50", rows)
    strategy = scripted({0: enter("10.00", "10.10", "9.80")})
    (trade,) = run(settings, strategy, bars, last_entry=time(15, 52)).trades
    assert trade.exit_reason is ExitReason.FLATTEN
    assert trade.exit_time == at("15:55")


def test_end_of_data_closes_at_last_close(settings, scripted):
    bars = series("10:00", [FLAT, ("10.05", "10.12", "10.02", "10.08")])
    (trade,) = run(settings, scripted({0: enter("10.00", "10.10", "9.80")}), bars).trades
    assert trade.exit_reason is ExitReason.END_OF_DATA
    assert trade.exit_price == D("10.08") - settings.slippage(D("10.08"))


def test_enter_decided_at_last_entry_time_is_rejected(settings, scripted):
    # The 15:44 bar completes at 15:45 = last entry: live admission would refuse it.
    bars = series("15:44", [FLAT, ("10.05", "10.12", "10.02", "10.08")])
    result = run(settings, scripted({0: enter("10.00", "10.10", "9.80")}), bars)
    assert result.trades == ()
    (signal,) = result.signals
    assert (signal.outcome, signal.cause) == (SignalOutcome.REJECTED, "after last entry time")


def test_pending_entry_is_missed_at_last_entry_time(settings, scripted):
    above = ("10.50", "10.60", "10.40", "10.55")
    bars = series("15:43", [FLAT, above, ("10.05", "10.12", "10.02", "10.08")])
    result = run(settings, scripted({0: enter("10.00", "10.10", "9.80")}), bars)
    assert result.trades == ()
    (signal,) = result.signals
    assert (signal.outcome, signal.cause) == (SignalOutcome.MISSED, "last entry time")


def test_enter_before_regular_open_is_rejected(settings, scripted):
    bars = series("09:28", [FLAT, FLAT, FLAT])
    result = run(settings, scripted({0: enter("10.00", "10.10", "9.80")}), bars)
    assert result.signals[0].outcome is SignalOutcome.REJECTED
    assert result.trades == ()


@pytest.mark.parametrize(
    ("spec", "cause"),
    [
        (enter("10.00", None, "9.80"), "ENTER without entry_high"),
        (enter("10.00", "10.10", None), "ENTER without stop"),
        (enter("10.20", "10.10", "9.80"), "entry_low above entry_high"),
        (enter("10.00", "10.10", "10.10"), "stop not below entry_high"),
    ],
)
def test_incomplete_enter_is_rejected_with_cause(settings, scripted, spec, cause):
    bars = series("10:00", [FLAT, FLAT])
    result = run(settings, scripted({0: spec}), bars)
    (signal,) = result.signals
    assert (signal.outcome, signal.cause) == (SignalOutcome.REJECTED, cause)


def test_enter_while_pending_or_open_is_ignored(settings, scripted):
    e = enter("10.00", "10.10", "9.80")
    bars = series("10:00", [FLAT, ("10.05", "10.12", "10.02", "10.08"), FLAT, FLAT])
    result = run(settings, scripted({0: e, 1: e}), bars)
    assert [s.outcome for s in result.signals] == [SignalOutcome.FILLED, SignalOutcome.IGNORED]
    assert result.signals[1].cause == "position open"


def test_position_view_is_given_to_strategy(settings, scripted):
    bars = series("10:00", [FLAT, ("10.05", "10.12", "10.02", "10.08"), FLAT])
    strategy = scripted({0: enter("10.00", "10.10", "9.80")})
    run(settings, strategy, bars)
    assert strategy.contexts[0].position is None
    view = strategy.contexts[1].position
    assert view is not None and view.stop == D("9.80") and view.quantity == 1


def test_wrong_tag_is_a_contract_error(settings):
    class Liar:
        name, version = "a", "1"

        def evaluate(self, context):
            return Decision("b", "1", context.symbol, Verdict.IGNORE, ())

    with pytest.raises(StrategyContractError):
        run(settings, Liar(), series("10:00", [FLAT]))


def test_clock_order_is_checked(settings, scripted):
    with pytest.raises(ReplayError):
        run(settings, scripted({}), series("10:00", [FLAT]), FLATTEN, LAST_ENTRY)


def test_no_look_ahead(settings, scripted):
    rows = [FLAT, ("10.05", "10.12", "10.02", "10.08"), ("10.20", "10.30", "10.15", "10.25")]
    bars = series("10:00", rows + [FLAT] * 3)
    first = scripted({})
    run(settings, first, bars)
    for i, context in enumerate(first.contexts):
        assert context.bars == bars[: i + 1]
        quote = context.quote
        assert quote is not None and quote.provider_time == bars[i].start + timedelta(minutes=1)
        spread = settings.synthetic_spread(bars[i].close)
        assert (quote.bid, quote.ask) == (bars[i].close - spread / 2, bars[i].close + spread / 2)

    # Altering bars after index 2 must not change any context up to index 2.
    altered = bars[:3] + tuple(replace(b, high=D("99"), close=D("98")) for b in bars[3:])
    second = scripted({})
    run(settings, second, altered)
    assert [c.bars for c in second.contexts[:3]] == [c.bars for c in first.contexts[:3]]
    assert [c.quote for c in second.contexts[:3]] == [c.quote for c in first.contexts[:3]]


def test_synthetic_quote_can_be_disabled(settings, scripted):
    strategy = scripted({})
    run(replace(settings, synthesize_quote=False), strategy, series("10:00", [FLAT]))
    assert strategy.contexts[0].quote is None


def test_sessions_split_by_new_york_date_with_prior_history(settings, scripted):
    from datetime import date

    day1 = series("15:58", [FLAT, FLAT])
    day2 = series("09:30", [FLAT, FLAT], day=date(2026, 11, 11))
    strategy = scripted({})
    result = run(settings, strategy, day1 + day2)
    assert result.sessions == 2 and result.bars == 4
    # History is in `bars` too (contract: current session plus history), oldest first.
    assert strategy.contexts[2].bars == day1 + day2[:1]
    assert strategy.contexts[2].prior_sessions == (day1,)


def test_history_is_limited_to_configured_sessions(settings, scripted):
    from datetime import date

    days = [series("10:00", [FLAT], day=date(2026, 11, n)) for n in (9, 10, 11, 12)]
    strategy = scripted({})
    run(settings, strategy, sum(days, ()))
    assert settings.history_sessions == 2
    last = strategy.contexts[-1]
    assert last.prior_sessions == (days[1], days[2])
    assert last.bars == days[1] + days[2] + days[3]


def test_position_never_crosses_sessions(settings, scripted):
    from datetime import date

    day1 = series("10:00", [FLAT, ("10.05", "10.12", "10.02", "10.08")])
    day2 = series("09:30", [("10.50", "10.60", "10.40", "10.55")], day=date(2026, 11, 11))
    (trade,) = run(settings, scripted({0: enter("10.00", "10.10", "9.80")}), day1 + day2).trades
    assert trade.exit_reason is ExitReason.END_OF_DATA
    assert trade.exit_time == day1[1].start


def test_each_symbol_gets_a_fresh_strategy(settings, scripted):
    made = []

    def factory():
        made.append(scripted({}))
        return made[-1]

    bars = {"AAA": series("10:00", [FLAT], "AAA"), "BBB": series("10:00", [FLAT], "BBB")}
    replay(factory, bars, settings, LAST_ENTRY, FLATTEN)
    assert [m.contexts[0].symbol for m in made] == ["AAA", "BBB"]
