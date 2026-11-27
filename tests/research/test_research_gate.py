"""R23 Step 0 gate report: descriptive checks on synthetic sized trades, frozen period, calendar."""

from __future__ import annotations

import csv
import dataclasses
import json
from datetime import date, datetime, timedelta
from decimal import Decimal

import pytest
from research_helpers import HERE

from tickr.calendar import SessionCalendar
from tickr.research.__main__ import main
from tickr.research.gate import (
    HOLDS,
    INCONCLUSIVE,
    INSUFFICIENT,
    MANUAL,
    NOT_HOLDS,
    OBSERVED,
    OUTSIDE,
    TRADE_COLUMNS,
    UNAVAILABLE,
    GateRefused,
    Trade,
    gate,
    load_covered_days,
    load_trades,
    market_day_lookup,
)
from tickr.research.settings import load_research_settings

ROOT = HERE.parent.parent
SETTINGS_PATH = HERE / "research_settings.toml"
SETTINGS = load_research_settings(SETTINGS_PATH)
CLOCK = ["--replay-settings", str(HERE / "replay_settings.toml"),
         "--config", str(ROOT / "config" / "bot.toml")]
CAP = Decimal(1000)
ALL = Decimal(4000)
MARKET_DAY = market_day_lookup(SessionCalendar.load(ROOT / "config" / "calendar"))
START, END = date(2026, 8, 1), date(2026, 10, 31)
CHECKS = ("net_after_costs_above_0", "sensitivity_k_of_m_months", "sensitivity_without_best")
ENTRY_HOUR = {"regular": "10:00", "extended": "17:00"}  # 10:00 regular, 17:00 post-market (ET)


def _days(first: date, last: date) -> set[date]:
    return {first + timedelta(days=i) for i in range((last - first).days + 1)
            if MARKET_DAY(first + timedelta(days=i)) is not None}


ALL_DAYS = _days(START, END)
_ids = iter(range(10**6))


def _t(day: str, result: str, session: str = "regular", *, fees: str | None = "0.05",
       slippage: str | None = "0.05", spread: str | None = "0.2", in_result: bool = True,
       notional: str = "1000", units: str = "pct_of_notional",
       provenance: str = "simulated") -> Trade:
    def num(raw):
        return None if raw is None else Decimal(raw)

    entry = datetime.fromisoformat(f"{day}T{ENTRY_HOUR[session]}:00-04:00")
    return Trade(f"t{next(_ids)}", "ABC", session, entry, entry + timedelta(minutes=30),
                 Decimal(notional), units, Decimal(result), num(fees), num(slippage),
                 num(spread), in_result, provenance, "costs-v1")


def _gate(trades, covered=None, start=START, end=END, settings=SETTINGS):
    return gate(trades, ALL_DAYS if covered is None else covered, start, end, MARKET_DAY, CAP,
                ALL, settings)


def test_descriptive_lines_checks_and_money():
    trades = [_t("2026-08-03", "1"), _t("2026-09-01", "2"), _t("2026-09-01", "-1"),
              _t("2026-10-05", "1.5"), _t("2026-10-06", "-0.5", "extended")]
    result = _gate(trades)
    assert result.conclusive
    assert [m.month for m in result.window] == ["2026-08", "2026-09", "2026-10"]
    main_line, all_line = result.lines
    assert (main_line.label, all_line.label) == ("descriptive", "hypothetical")
    assert main_line.checks == dict.fromkeys(CHECKS, HOLDS)
    assert (main_line.trades, main_line.days, main_line.days_in_plus) == (4, 3, 3)
    assert (main_line.earned, main_line.lost, main_line.net) == (45, 10, 35)
    assert (main_line.fees, main_line.slippage) == (2, 2)  # 4 x 0.05% of 1000, already in
    assert main_line.spread_in_results == 8 and main_line.spread_charged == 0
    assert main_line.same_trade_matched == 4 and main_line.same_trade_spread_cost == 8
    assert main_line.net_before_spread == 43
    assert main_line.dropped == 1 and main_line.net_without_best == 15  # best +20 removed
    assert all_line.sessions == "all"
    assert (all_line.earned, all_line.lost, all_line.net) == (180, 60, 120)


def test_month_window_anchored_to_end_includes_months_after_last_trade():
    # The last trade is in August; --end in October still puts September and October in the
    # window as observed months with zero trades (net 0, not in plus).
    trades = [_t("2026-08-03", "1"), _t("2026-08-04", "1")]
    line = _gate(trades).lines[0]
    assert [(m.month, m.status, m.trades, m.in_plus) for m in line.months] == [
        ("2026-08", OBSERVED, 2, True), ("2026-09", OBSERVED, 0, False),
        ("2026-10", OBSERVED, 0, False)]
    assert line.checks["sensitivity_k_of_m_months"] == NOT_HOLDS
    assert line.checks["net_after_costs_above_0"] == HOLDS


def test_missing_coverage_is_not_a_zero_trade_month():
    # September has no data at all: insufficient coverage, not a month with zero trades.
    covered = ALL_DAYS - _days(date(2026, 9, 1), date(2026, 9, 30))
    trades = [_t("2026-08-03", "1"), _t("2026-10-05", "1")]
    result = _gate(trades, covered)
    sept = result.window[1]
    assert (sept.status, sept.covered_days, sept.coverage) == (INSUFFICIENT, 0, 0)
    assert not result.conclusive
    assert set(result.lines[0].checks.values()) == {INCONCLUSIVE}


def test_one_missing_day_makes_the_month_and_period_inconclusive():
    covered = ALL_DAYS - {date(2026, 9, 15)}
    result = _gate([_t("2026-08-03", "1"), _t("2026-10-05", "1")], covered)
    sept = result.window[1]
    assert sept.status == INSUFFICIENT and sept.covered_days == sept.period_days - 1
    assert [m.status for m in result.window] == [OBSERVED, INSUFFICIENT, OBSERVED]
    assert "insufficient coverage of the period (a trading day without data)" in \
        result.lines[0].causes


def test_partial_month_reports_its_period_fraction():
    # --end on 2026-10-20: October is cut by the period but fully covered inside it, so it is
    # observed, and its period fraction shows how much of the month the period holds.
    end = date(2026, 10, 20)
    covered = _days(START, end)
    result = _gate([_t("2026-08-03", "1")], covered, end=end)
    assert result.conclusive
    october = result.window[2]
    assert october.trading_days == len(_days(date(2026, 10, 1), date(2026, 10, 31)))
    assert october.period_days == october.covered_days == len(_days(date(2026, 10, 1), end))
    assert october.period_fraction == Decimal(october.period_days) / Decimal(october.trading_days)
    assert october.period_fraction < 1 and october.coverage == 1
    assert october.status == OBSERVED


def test_window_month_before_start_is_outside_period():
    result = _gate([_t("2026-10-05", "1")], _days(date(2026, 9, 15), END),
                   start=date(2026, 9, 15))
    assert result.window[0].status == OUTSIDE
    assert result.lines[0].checks["sensitivity_k_of_m_months"] == INCONCLUSIVE


def test_insufficient_period_coverage_is_inconclusive():
    covered = _days(date(2026, 8, 3), date(2026, 8, 31))  # September and October missing
    result = _gate([_t("2026-08-03", "1")], covered)
    assert not result.conclusive
    assert set(result.lines[0].checks.values()) == {INCONCLUSIVE}
    assert "insufficient coverage of the period (a trading day without data)" in \
        result.lines[0].causes


def test_zero_trades_is_inconclusive_not_refused():
    result = _gate([])
    assert not result.conclusive
    for line in result.lines:
        assert set(line.checks.values()) == {INCONCLUSIVE}
        assert "zero assessable trades" in line.causes


def test_unknown_cost_is_inconclusive_and_zero_is_valid():
    known_zero = _gate([_t("2026-08-03", "1", fees="0", slippage="0"), _t("2026-09-01", "1"),
                        _t("2026-10-05", "1")])
    assert known_zero.conclusive
    unknown = _gate([_t("2026-08-03", "1", fees=None), _t("2026-09-01", "1"),
                     _t("2026-10-05", "1")])
    line = unknown.lines[0]
    assert line.unknown_cost_trades == 1 and line.fees is None
    assert line.checks["net_after_costs_above_0"] == INCONCLUSIVE
    assert "1 trades with an unknown cost" in line.causes


def test_spread_is_charged_once():
    inside = _t("2026-08-03", "1", spread="0.2", in_result=True)
    outside = _t("2026-08-03", "1", spread="0.2", in_result=False)
    assert inside.net() == 10 and outside.net() == 8
    line = _gate([inside, outside]).lines[0]
    assert (line.spread_in_results, line.spread_charged, line.net) == (2, 2, 18)
    # A mid-priced result whose spread is unknown cannot be netted: unknown, never zero.
    assert _t("2026-08-03", "1", spread=None, in_result=False).net() is None


def test_same_trade_spread_cost_is_a_diagnostic_not_a_baseline():
    trades = [_t("2026-08-03", "-0.1", spread="0.3"), _t("2026-09-01", "-0.1", spread="0.3"),
              _t("2026-10-05", "-0.1", spread="0.3")]
    line = _gate(trades).lines[0]
    assert line.net == -3 and line.same_trade_spread_cost == 9 and line.net_before_spread == 6
    assert "net_above_spread_only_baseline" not in line.checks
    assert line.checks["net_after_costs_above_0"] == NOT_HOLDS
    line = _gate([*trades, _t("2026-10-06", "1", spread=None)]).lines[0]
    assert line.same_trade_matched == 3 and line.net_before_spread is None


def test_incomplete_delta_is_suppressed():
    # Codex's case: two 1,000 USD trades, one with an unknown cost. The model makes 10 USD on the
    # known one; the old comparator took -4 USD of spread over BOTH rows and printed +14.
    known = _t("2026-08-03", "1", fees="0", slippage="0", spread="0.2")
    unknown = _t("2026-08-04", "1", fees=None, slippage="0", spread="0.2")
    line = _gate([known, unknown]).lines[0]
    assert line.net == 10 and line.unknown_cost_trades == 1
    assert line.same_trade_matched == 1 and line.same_trade_spread_cost == 2  # matched rows only
    assert line.net_before_spread is None


def test_usd_return_units_and_hypothetical_rescale():
    trade = _t("2026-08-03", "5", units="usd", notional="500", fees="0.5", spread="1")
    line_main, line_all = _gate([trade]).lines
    assert line_main.net == 5 and line_all.net == 40  # x 4000 / 500
    assert line_all.fees == 4


def test_drop_best_is_sensitivity_when_one_trade_carries_the_period():
    trades = [_t("2026-08-03", "10"), *(_t("2026-09-01", "-0.1") for _ in range(50)),
              _t("2026-10-05", "0.5")]
    line = _gate(trades).lines[0]
    assert line.net > 0 and line.dropped == 1  # ceil(52 x 1%)
    assert line.checks["sensitivity_without_best"] == NOT_HOLDS
    assert line.checks["net_after_costs_above_0"] == HOLDS


def test_tail_concentration_shares():
    trades = [_t("2026-08-03", "10"), *(_t("2026-09-01", "0.1") for _ in range(19))]
    tails = {t.percent: t for t in _gate(trades).lines[0].tails}
    # 20 trades: top 1% = 1 trade, top 5% = 1 trade, top 10% = 2 trades; net 119 USD.
    assert (tails[1].trades, tails[5].trades, tails[10].trades) == (1, 1, 2)
    assert tails[1].net == 100 and tails[10].net == 101
    assert tails[1].share_pct == Decimal(100) * 100 / 119
    losing = _gate([_t("2026-08-03", "-1"), _t("2026-08-04", "0.5")]).lines[0]
    assert all(t.share_pct is None for t in losing.tails)


def test_all_sessions_line_says_regular_only_without_extended_trades():
    result = _gate([_t("2026-08-03", "1"), _t("2026-10-05", "1")])
    assert result.lines[1].sessions == "all (regular only: no extended trades)"


@pytest.mark.parametrize(
    ("trades", "covered", "cause"),
    [
        ([_t("2026-07-31", "1")], None, GateRefused.OUTSIDE_PERIOD),
        ([_t("2026-08-03", "1")], ALL_DAYS - {date(2026, 8, 3)}, GateRefused.NOT_COVERED),
        ([_t("2026-08-03", "1", notional="1000.01")], None, GateRefused.LIMIT),
        ([], ALL_DAYS | {date(2026, 11, 2)}, GateRefused.OUTSIDE_PERIOD),
        ([], ALL_DAYS | {date(2026, 8, 8)}, GateRefused.BAD_VALUE),  # a Saturday
    ],
)
def test_gate_refusals(trades, covered, cause):
    with pytest.raises(GateRefused) as info:
        _gate(trades, covered)
    assert info.value.cause == cause


@pytest.mark.parametrize(
    ("entry", "exit_", "session", "cause"),
    [
        # Entered inside the period, exited a year later, after a report ending 2026-10-31.
        ("2026-10-05T10:00:00-04:00", "2027-10-05T10:00:00-04:00", "regular",
         GateRefused.OUTSIDE_PERIOD),
        ("2026-10-05T10:00:00-04:00", "2026-10-06T10:00:00-04:00", "regular",
         GateRefused.NOT_SAME_DAY),
        ("2026-10-05T10:00:00-04:00", "2026-10-05T20:30:00-04:00", "regular",
         GateRefused.NOT_SAME_DAY),
        ("2026-10-05T10:00:00-04:00", "2026-10-05T10:30:00-04:00", "extended",
         GateRefused.SESSION_MISMATCH),
        ("2026-10-05T17:00:00-04:00", "2026-10-05T17:30:00-04:00", "regular",
         GateRefused.SESSION_MISMATCH),
        ("2026-10-05T03:00:00-04:00", "2026-10-05T10:30:00-04:00", "extended",
         GateRefused.SESSION_MISMATCH),
    ],
)
def test_timing_refusals(entry, exit_, session, cause):
    trade = dataclasses.replace(_t("2026-10-05", "1"), session=session,
                                entry_time=datetime.fromisoformat(entry),
                                exit_time=datetime.fromisoformat(exit_))
    with pytest.raises(GateRefused) as info:
        _gate([trade])
    assert info.value.cause == cause


def test_premarket_entry_is_extended():
    trade = dataclasses.replace(
        _t("2026-10-05", "1", "extended"),
        entry_time=datetime.fromisoformat("2026-10-05T08:00:00-04:00"),
        exit_time=datetime.fromisoformat("2026-10-05T09:45:00-04:00"))
    assert _gate([trade]).lines[1].trades == 1


def test_start_after_end_refused():
    with pytest.raises(GateRefused) as info:
        _gate([], start=END, end=START)
    assert info.value.cause == GateRefused.BAD_VALUE


ROW = {"trade_id": "a1", "symbol": "ABC", "session": "regular",
       "entry_time": "2026-08-03T10:00:00-04:00", "exit_time": "2026-08-03T10:30:00-04:00",
       "notional_usd": "1000", "return_units": "pct_of_notional", "result": "1", "fees": "0.05",
       "slippage": "", "spread": "0.2", "spread_in_result": "true",
       "cost_provenance": "simulated", "cost_version": "costs-v1"}


def _write(path, header, rows):
    with open(path, "w", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(header)
        writer.writerows(rows)
    return path


def _trades_file(path, *rows):
    return _write(path, TRADE_COLUMNS, [[r[c] for c in TRADE_COLUMNS] for r in rows])


def test_load_keeps_unknown_apart_from_zero(tmp_path):
    (trade,) = load_trades(_trades_file(tmp_path / "t.csv", ROW | {"fees": "0"}))
    assert trade.fees == 0 and trade.slippage is None and trade.day == date(2026, 8, 3)


@pytest.mark.parametrize(
    ("change", "cause"),
    [
        ({"result": "NaN"}, GateRefused.NOT_FINITE),
        ({"result": "x"}, GateRefused.BAD_VALUE),
        ({"fees": "-0.1"}, GateRefused.BAD_VALUE),
        ({"spread": "-0.1"}, GateRefused.BAD_VALUE),
        ({"session": "premarket"}, GateRefused.BAD_VALUE),
        ({"return_units": "bps"}, GateRefused.BAD_VALUE),
        ({"entry_time": "2026-08-03T10:00:00"}, GateRefused.BAD_VALUE),
        ({"exit_time": "2026-08-03T09:00:00-04:00"}, GateRefused.BAD_VALUE),
        ({"notional_usd": "0"}, GateRefused.BAD_VALUE),
        ({"cost_version": ""}, GateRefused.BAD_VALUE),
        ({"cost_provenance": "guess"}, GateRefused.BAD_VALUE),
        ({"cost_provenance": "broker_fills", "spread_in_result": "false"},
         GateRefused.BAD_VALUE),
        ({"spread_in_result": "yes"}, GateRefused.BAD_VALUE),
    ],
)
def test_load_refusals(tmp_path, change, cause):
    with pytest.raises(GateRefused) as info:
        load_trades(_trades_file(tmp_path / "t.csv", ROW | change))
    assert info.value.cause == cause


def test_load_refuses_missing_column_and_duplicates(tmp_path):
    with pytest.raises(GateRefused) as info:
        load_trades(_write(tmp_path / "a.csv", TRADE_COLUMNS[:-1], []))
    assert info.value.cause == GateRefused.MISSING_COLUMN
    with pytest.raises(GateRefused) as info:
        load_trades(_trades_file(tmp_path / "b.csv", ROW, ROW))
    assert info.value.cause == GateRefused.DUPLICATE
    with pytest.raises(GateRefused) as info:
        load_covered_days(_write(tmp_path / "c.csv", ["day"], [["2026-08-03"], ["2026-08-03"]]))
    assert info.value.cause == GateRefused.DUPLICATE


# The command refuses the sealed exam window (config/research.toml [sealed]): its tests use an
# open period, April to June 2026, ending the day before the window opens on 2026-07-01.
CLI_START, CLI_END = "2026-04-01", "2026-06-30"
OPEN_DAYS = _days(date(2026, 4, 1), date(2026, 6, 30))


def _cli(tmp_path, trades, days, start=CLI_START, end=CLI_END):
    out = tmp_path / "o"
    code = main(["gate", "--trades", str(trades), "--covered-days", str(days),
                 "--start", start, "--end", end, "--all-sessions-usd", "4000",
                 "--model-version", "layer2-test", "--results", "simulated",
                 "--settings", str(SETTINGS_PATH), "--out", str(out), *CLOCK])
    return code, out


def _open_row(trade_id, day, **change):
    return ROW | {"trade_id": trade_id, "slippage": "0", "entry_time": f"{day}T10:00:00-04:00",
                  "exit_time": f"{day}T11:00:00-04:00"} | change


def test_cli_writes_descriptive_outputs(tmp_path, capsys):
    trades = _trades_file(tmp_path / "t.csv", _open_row("a1", "2026-04-06",
                                                        exit_time="2026-04-06T10:30:00-04:00"),
                          _open_row("a2", "2026-05-05", result="-0.5"),
                          _open_row("a3", "2026-06-01", result="2", spread_in_result="false"))
    days = _write(tmp_path / "d.csv", ["day"], [[d.isoformat()] for d in sorted(OPEN_DAYS)])
    code, out = _cli(tmp_path, trades, days)
    assert code == 0
    printed = capsys.readouterr().out
    assert "passed" not in printed.lower()
    with open(out / "gate.csv", newline="") as handle:
        rows = list(csv.DictReader(handle))
    assert [(r["line"], r["check"], r["kind"], r["outcome"]) for r in rows[:3]] == [
        ("main_session", "net_after_costs_above_0", "descriptive", HOLDS),
        ("main_session", "sensitivity_k_of_m_months", "sensitivity", HOLDS),
        ("main_session", "sensitivity_without_best", "sensitivity", HOLDS)]  # 23 - 18
    assert [(r["check"], r["outcome"]) for r in rows if r["kind"] == "paired baseline"] == [
        ("paired_baseline", UNAVAILABLE)]
    assert rows[0]["measured"] == "23.0000"  # 10 - 5 + (20 - 2 spread charged once)
    assert [r["check"] for r in rows if r["outcome"] == "checked by hand"] == list(MANUAL)
    summary = json.loads((out / "gate_summary.json").read_text())
    assert summary["report"] == "descriptive" and summary["conclusive"] is True
    assert summary["frozen"]["start"] == CLI_START and summary["frozen"]["end"] == CLI_END
    assert set(summary["frozen"]["calendar_files_sha256"]) >= {"us-equity-2026.json"}
    assert summary["lines"]["all_sessions"]["line_label"] == "hypothetical"
    assert summary["lines"]["main_session"]["line_label"] == "descriptive"
    assert summary["costs"]["provenance_version_trades"] == {
        "simulated costs-v1 (spread in result: false)": 1,
        "simulated costs-v1 (spread in result: true)": 2}
    assert [t["top_percent"] for t in summary["lines"]["main_session"]["tails"]] == \
        ["1.0000", "5.0000", "10.0000"]
    assert "passed" not in json.dumps(summary).lower()
    assert summary["paired_baseline"]["status"] == UNAVAILABLE
    with open(out / "gate_trades.csv", newline="") as handle:
        audit = list(csv.DictReader(handle))
    assert [(a["trade_id"], a["spread_charged"], a["net_usd"]) for a in audit] == [
        ("a1", "0.0000", "10.0000"), ("a2", "0.0000", "-5.0000"), ("a3", "0.2000", "18.0000")]
    with open(out / "gate_months.csv", newline="") as handle:
        assert len(list(csv.DictReader(handle))) == 6
    assert (out / "gate_lines.csv").exists() and (out / "gate_tails.csv").exists()


def test_cli_refuses_outside_calendar_coverage(tmp_path, capsys):
    trades = _trades_file(tmp_path / "t.csv")
    days = _write(tmp_path / "d.csv", ["day"], [])
    code, _ = _cli(tmp_path, trades, days, start="2024-01-02", end="2024-03-29")
    assert code == 2
    assert "OUT_OF_COVERAGE" in capsys.readouterr().err
