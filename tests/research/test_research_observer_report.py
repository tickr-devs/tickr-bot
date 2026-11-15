from __future__ import annotations

from decimal import Decimal

import pytest
from research_helpers import (
    CLOCK,
    HERE,
    bar_json,
    flat,
    record,
    replay_settings,
    series,
    write_jsonl,
)

from tickr.research.__main__ import main
from tickr.research.errors import ResearchError
from tickr.research.observer_report import report
from tickr.research.records import Admission, write_records
from tickr.strategy.base import Role, Verdict

ROOT = HERE.parent.parent
DECISION = ("10.00", "10.05", "9.95", "10.05")
# 10:00 signal bar, 10:01 fills [10.00, 10.10], 10:02 rises, 10:03 hits the 9.80 stop.
BARS = series("10:00", [DECISION, ("10.05", "10.08", "10.00", "10.06"), flat("10.40"),
                        ("10.00", "10.00", "9.70", "9.75"), flat("9.75")])


def enter(clock, **kw):
    kw.setdefault("quantity", "10")
    return record(clock, low="10.00", high="10.10", stop="9.80", **kw)


def active(clock, verdict=Verdict.ENTER, **kw):
    if verdict is Verdict.ENTER:
        return enter(clock, **kw)
    return record(clock, verdict, **kw)


def observer(clock, verdict=Verdict.ENTER, **kw):
    kw.update(role=Role.OBSERVER, version="2", admission=None)
    if verdict is Verdict.ENTER:
        kw["admission"] = Admission.NOT_EVALUATED
        return enter(clock, **kw)
    return record(clock, verdict, **kw)


RECORDS = [
    active("10:00", admission=Admission.TAKEN, cause=None),
    active("10:01", Verdict.WATCH),
    observer("10:00"),
    observer("10:01"),                        # still in the 10:01-10:03 trade: overlap
    observer("10:02", quantity=None),         # overlap too (the trade exits at 10:03)
    observer("10:03", quantity=None),         # price never back in the window: missed
]


def run(records=RECORDS):
    return report(records, {"ABC": BARS}, replay_settings(), CLOCK)


def test_active_first_then_observers_with_comparison():
    result = run()
    assert result.active == "s v1"
    act, obs = result.identities
    assert (act.role, act.enters, act.enters_taken, act.trades) == ("active", 1, 1, 1)
    assert act.vs_active is None and act.shared_enter_bars is None
    assert (obs.role, obs.version, obs.enters) == ("observer", "2", 4)
    assert (obs.simulated["skipped_overlap"], obs.simulated["missed"]) == (2, 1)
    assert obs.trades == 1 and obs.losses == 1
    assert obs.usd_trades == 1 and obs.total_usd == act.total_usd  # same 10:00 trade, 10 shares
    assert obs.shared_enter_bars == 1
    # 10:00 same ENTER; 10:01 ENTER vs WATCH; 10:02 and 10:03 ENTER vs absent.
    assert obs.vs_active == {"same": 1, "verdict_changed": 3, "levels_changed": 0}


def test_trade_r_matches_the_whatif_model():
    act = run().identities[0]
    entry, exit_ = Decimal("10.10") + Decimal("0.0101"), Decimal("9.80") - Decimal("0.01")
    assert act.total_r == (exit_ - entry) / (entry - Decimal("9.80"))
    assert act.total_usd == (exit_ - entry) * 10


def test_without_active_records_observers_are_still_reported():
    result = run([observer("10:00")])
    assert result.active is None and result.identities[0].vs_active is None


def test_two_active_strategies_are_refused():
    with pytest.raises(ResearchError, match="more than one active strategy"):
        run([active("10:00", Verdict.WATCH), active("10:01", Verdict.WATCH, version="9")])


def test_cli_writes_the_daily_report(tmp_path, capsys):
    decisions = tmp_path / "decisions.jsonl"
    write_records(decisions, RECORDS)
    bars = write_jsonl(tmp_path / "bars.jsonl", [bar_json(b) for b in BARS])
    code = main(["observers", "--bars", str(bars), "--decisions", str(decisions),
                 "--replay-settings", str(HERE / "replay_settings.toml"),
                 "--config", str(ROOT / "config" / "bot.toml"), "--out", str(tmp_path / "o")])
    assert code == 0
    assert (tmp_path / "o" / "observers.csv").read_text().count("\n") == 3
    assert '"active": "s v1"' in capsys.readouterr().out


def test_a_repeated_enter_of_a_traded_signal_is_not_a_second_trade():
    from dataclasses import replace

    def signal(clock):
        rec = observer(clock)
        return replace(rec, decision=replace(rec.decision, setup="x", trigger=Decimal("10.05")))

    # 10:00 trades and stops out at 10:03; the same signal's ENTER at 10:04 is not re-traded
    obs = run([signal("10:00"), signal("10:04")]).identities[0]
    assert obs.simulated["skipped_same_signal"] == 1 and obs.trades == 1
    other = run([signal("10:00"), observer("10:04")]).identities[0]
    assert other.simulated["skipped_same_signal"] == 0  # no trigger: never deduplicated
    during = run([signal("10:00"), signal("10:01")]).identities[0]
    assert (during.simulated["skipped_overlap"], during.simulated["skipped_same_signal"]) == (1, 0)


def test_the_active_strategy_is_not_deduplicated():
    from dataclasses import replace

    def signal(clock):
        rec = active(clock)
        return replace(rec, decision=replace(rec.decision, setup="x", trigger=Decimal("10.05")))

    act = run([signal("10:00"), signal("10:04")]).identities[0]
    assert act.simulated["skipped_same_signal"] == 0
