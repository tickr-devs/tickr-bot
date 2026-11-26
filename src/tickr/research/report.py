"""Write research outputs as CSV and JSON. Numbers are rounded only here, at the boundary.

Outcome rows carry the label `observed` (real captured prices after a decision, never called
profit); what-if rows carry `simulated`.
"""

from __future__ import annotations

import csv
import json
from collections.abc import Iterable, Sequence
from dataclasses import asdict
from decimal import Decimal
from pathlib import Path
from typing import Any

from tickr.replay.measured import FORMAT as MEASURED_FORMAT
from tickr.research import calibration as cal
from tickr.research import collar as col
from tickr.research import fill_quality as fq
from tickr.research import gate as gt
from tickr.research import latency as lat
from tickr.research import observer_report as orep
from tickr.research import outcomes as oc
from tickr.research import refusals as rf
from tickr.research import slippage as sl
from tickr.research import whatif as wi
from tickr.research.compare import DiffResult
from tickr.research.records import DecisionRecord
from tickr.research.settings import ResearchSettings


def fmt(value: Any, places: int) -> str:
    if value is None:
        return ""
    if isinstance(value, Decimal):
        return str(round(value, places))
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


def _write_csv(path: Path, columns: Sequence[str], rows: Iterable[Sequence[Any]], places: int):
    with open(path, "w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(columns)
        for row in rows:
            writer.writerow([fmt(v, places) for v in row])


def _json(path: Path, body: dict[str, Any], places: int) -> dict[str, Any]:
    def convert(value: Any) -> Any:
        if isinstance(value, dict):
            return {k: convert(v) for k, v in value.items()}
        if isinstance(value, list | tuple):
            return [convert(v) for v in value]
        if isinstance(value, Decimal):
            return fmt(value, places)
        return value

    out = convert(body)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(out, handle, indent=2)
        handle.write("\n")
    return out


def _base(record: DecisionRecord) -> list[Any]:
    d = record.decision
    return [d.strategy, d.version, record.role.value, d.symbol, record.bar_start, d.verdict.value,
            d.setup, record.admission.value, record.refusal_cause]


BASE_COLUMNS = ("strategy", "version", "role", "symbol", "decision_bar", "verdict", "setup",
                "admission", "refusal_cause")

OUTCOME_COLUMNS = (
    "label", *BASE_COLUMNS, "horizon_minutes", "reference_price", "atr", "price_at_horizon",
    "move_percent", "move_atr", "max_up_percent", "max_up_atr", "max_down_percent",
    "max_down_atr", "cause",
)


def write_outcomes(rows: Sequence[oc.Outcome], out_dir: Path, places: int) -> dict[str, Any]:
    out_dir.mkdir(parents=True, exist_ok=True)
    _write_csv(
        out_dir / "outcomes.csv",
        OUTCOME_COLUMNS,
        (
            [oc.LABEL, *_base(r.record), r.horizon_minutes, r.reference, r.atr, r.price,
             r.percent(r.move), r.in_atr(r.move), r.percent(r.max_up), r.in_atr(r.max_up),
             r.percent(r.max_down), r.in_atr(r.max_down), r.cause]
            for r in rows
        ),
        places,
    )
    body = {"label": oc.LABEL, "groups": [asdict(s) for s in oc.summarize(rows)]}
    return _json(out_dir / "outcomes_summary.json", body, places)


WHATIF_COLUMNS = (
    "label", *BASE_COLUMNS, "entry_low", "entry_high", "stop", "outcome", "cause", "quantity",
    "entry_time", "entry_price", "exit_time", "exit_price", "exit_reason", "bars_held",
    "r_multiple", "result_usd", "risk_usd",
)


def write_whatif(rows: Sequence[wi.WhatIf], out_dir: Path, places: int) -> dict[str, Any]:
    out_dir.mkdir(parents=True, exist_ok=True)

    def line(w: wi.WhatIf) -> list[Any]:
        t, d = w.trade, w.record.decision
        trade = (
            [t.entry_time, t.entry_price, t.exit_time, t.exit_price, t.exit_reason.value,
             t.bars_held]
            if t is not None else [None] * 6
        )
        quantity = None if w.quantity is None else int(w.quantity)
        return [wi.LABEL, *_base(w.record), d.entry_low, d.entry_high, d.stop, w.outcome.value,
                w.cause, quantity, *trade, w.r_multiple, w.result_usd, w.risk_usd]

    _write_csv(out_dir / "whatif.csv", WHATIF_COLUMNS, (line(w) for w in rows), places)
    body = {"label": wi.LABEL, **asdict(wi.summarize(rows))}
    return _json(out_dir / "whatif_summary.json", body, places)


DIFF_COLUMNS = (
    "symbol", "decision_bar", "kind", "changed",
    "baseline_verdict", "baseline_setup", "baseline_entry_high", "baseline_stop",
    "baseline_admission", "baseline_first_failed_check", "baseline_records",
    "candidate_verdict", "candidate_setup", "candidate_entry_high", "candidate_stop",
    "candidate_first_failed_check", "candidate_records",
)


def _side(record: DecisionRecord | None, admission: bool) -> list[Any]:
    if record is None:
        return ["absent", None, None, None, *([None] if admission else []), None]
    d = record.decision
    failed = d.decisive.rule if d.decisive is not None else None
    extra = [record.admission.value] if admission else []
    return [d.verdict.value, d.setup, d.entry_high, d.stop, *extra, failed]


def write_diff(result: DiffResult, out_dir: Path, places: int) -> dict[str, Any]:
    out_dir.mkdir(parents=True, exist_ok=True)
    _write_csv(
        out_dir / "diff.csv",
        DIFF_COLUMNS,
        (
            [r.symbol, r.bar_start, r.kind.value, " ".join(r.changed),
             *_side(r.baseline, True), r.baseline_count,
             *_side(r.candidate, False), r.candidate_count]
            for r in result.rows
        ),
        places,
    )
    body = {"baseline": result.baseline, "candidate": result.candidate, "rows": len(result.rows),
            "by_kind": result.counts}
    return _json(out_dir / "diff_summary.json", body, places)


OBSERVER_COLUMNS = (
    "label", "strategy", "version", "role", "decisions", "enters", "enters_taken",
    "enters_refused", "trades", "wins", "losses", "total_r", "average_r", "total_usd",
    "usd_trades", "shared_enter_bars", "same", "verdict_changed", "levels_changed",
    "captured_trades", "average_captured_share",
)


def write_observer_report(
    result: orep.ObserverReport, out_dir: Path, places: int
) -> dict[str, Any]:
    out_dir.mkdir(parents=True, exist_ok=True)

    def line(r: orep.IdentityReport) -> list[Any]:
        diff = r.vs_active or {}
        return [orep.LABEL, r.strategy, r.version, r.role, r.decisions, r.enters,
                r.enters_taken, r.enters_refused, r.trades, r.wins, r.losses, r.total_r,
                r.average_r, r.total_usd, r.usd_trades, r.shared_enter_bars,
                diff.get("same"), diff.get("verdict_changed"), diff.get("levels_changed"),
                r.captured_trades, r.average_captured_share]

    _write_csv(out_dir / "observers.csv", OBSERVER_COLUMNS,
               (line(r) for r in result.identities), places)
    body = {"label": orep.LABEL, "active": result.active,
            "identities": [asdict(r) for r in result.identities]}
    return _json(out_dir / "observers.json", body, places)


LATENCY_COLUMNS = ("group", "key", "count", "median_ms", "p90_ms", "max_ms", "min_ms",
                   "clock_skew")
NOTICE_COLUMNS = ("hour", "notices", "drop_sum", "drop_max", "sent_sum", "sent_max",
                  "rtt_median", "rtt_max")


def write_latency(result: lat.LatencyReport, source: str, out_dir: Path,
                  places: int) -> dict[str, Any]:
    out_dir.mkdir(parents=True, exist_ok=True)
    groups = (("symbol", result.by_symbol), ("hour", result.by_hour))
    _write_csv(out_dir / "latency.csv", LATENCY_COLUMNS,
               ([group, key, s.count, s.median, s.p90, s.max, s.min, s.clock_skew]
                for group, rows in groups for key, s in rows.items()), places)
    _write_csv(out_dir / "notices.csv", NOTICE_COLUMNS,
               ([hour, *(row[c] for c in NOTICE_COLUMNS[1:])]
                for hour, row in result.notices_by_hour.items()), places)
    hours = result.by_hour.values()
    body = {"source": source, "quotes": sum(s.count for s in hours),
            "symbols": len(result.by_symbol), "hours": len(result.by_hour),
            "clock_skew_quotes": sum(s.clock_skew for s in hours),
            "min_latency_ms": min((s.min for s in hours), default=None),
            "notices": sum(int(n["notices"]) for n in result.notices_by_hour.values()),
            "partial_last_lines": result.partial_last_lines}
    return _json(out_dir / "latency_summary.json", body, places)


COLLAR_COLUMNS = ("entry", "reference", "symbol", "count",
                  *col.PERCENTILE_NAMES, "max")


def write_collar(result: col.CollarReport, out_dir: Path, places: int) -> dict[str, Any]:
    out_dir.mkdir(parents=True, exist_ok=True)
    _write_csv(out_dir / "collar.csv", COLLAR_COLUMNS,
               ([*key, d.count, *d.percentiles, d.max] for key, d in result.rows.items()),
               places)
    overall = {f"{kind}/{reference}": {"count": d.count, **dict(zip(
        col.PERCENTILE_NAMES, d.percentiles, strict=True)), "max": d.max}
               for (kind, reference, group), d in result.rows.items() if group == col.OVERALL}
    body = {"overall": overall,
            "missing_reference": {f"{k}/{r}": n for (k, r), n in result.missing.items()},
            "partial_last_lines": result.partial_last_lines}
    return _json(out_dir / "collar_summary.json", body, places)


REFUSAL_COLUMNS = ("day", "strategy", "version", "role", "setup", "kind", "name", "count")


def write_refusals(rows: Sequence[rf.RefusalRow], out_dir: Path, places: int) -> dict[str, Any]:
    out_dir.mkdir(parents=True, exist_ok=True)
    _write_csv(out_dir / "refusals.csv", REFUSAL_COLUMNS,
               ([getattr(r, c) for c in REFUSAL_COLUMNS] for r in rows), places)
    by_kind: dict[str, int] = {}
    for r in rows:
        by_kind[r.kind] = by_kind.get(r.kind, 0) + r.count
    body = {"days": sorted({r.day.isoformat() for r in rows}), "rows": len(rows),
            "by_kind": by_kind}
    return _json(out_dir / "refusals_summary.json", body, places)


SLIPPAGE_TRADE_COLUMNS = (
    "day", "trade_id", "symbol", "close_cause", "stop_quantity", "stop_price", "stop_fill_price",
    "slippage_usd", "slippage_percent", "entry_price", "sold_quantity", "planned_loss_usd",
    "realized_loss_usd", "cause")
SLIPPAGE_DAY_COLUMNS = (
    "day", "stopped_trades", "measured_trades", "planned_loss_usd", "realized_loss_usd",
    "stop_slippage_usd", "stop_slippage_percent")


def write_slippage(trades: Sequence[sl.StoppedTrade], environment: str | None, out_dir: Path,
                   places: int) -> dict[str, Any]:
    """`slippage_trades.csv`, `slippage_days.csv` and `slippage_summary.json`, the file the
    replay's `measured_slippage_file` selects (`tickr.replay.measured`)."""
    out_dir.mkdir(parents=True, exist_ok=True)
    _write_csv(out_dir / "slippage_trades.csv", SLIPPAGE_TRADE_COLUMNS,
               ([getattr(t, c) for c in SLIPPAGE_TRADE_COLUMNS] for t in trades), places)
    days = sl.by_day(trades)
    _write_csv(out_dir / "slippage_days.csv", SLIPPAGE_DAY_COLUMNS,
               ([getattr(d, c) for c in SLIPPAGE_DAY_COLUMNS] for d in days), places)
    total = sl.group(trades, None)
    body = {"format": MEASURED_FORMAT, "label": "measured", "environment": environment,
            "days": [d.day.isoformat() for d in days if d.day is not None],
            "stopped_trades": total.stopped_trades, "measured_trades": total.measured_trades,
            "planned_loss_usd": total.planned_loss_usd,
            "realized_loss_usd": total.realized_loss_usd,
            "stop_slippage_usd": total.stop_slippage_usd,
            "stop_slippage_percent": total.stop_slippage_percent}
    return _json(out_dir / "slippage_summary.json", body, places)


CALIBRATION_COLUMNS = ("column", "bucket", "low", "high", "count", "mean_predicted",
                       "observed_rate", "gap")


def write_calibration(reports: Sequence[cal.ColumnReport], out_dir: Path,
                      places: int) -> dict[str, Any]:
    out_dir.mkdir(parents=True, exist_ok=True)
    _write_csv(out_dir / "calibration.csv", CALIBRATION_COLUMNS,
               ([r.column, b.index, b.low, b.high, b.count, b.mean_predicted, b.observed_rate,
                 b.gap] for r in reports for b in r.buckets), places)
    body = {"columns": {r.column: {"answers": r.answers, "skipped_blank": r.skipped_blank,
                                   "base_rate": r.base_rate, "brier": r.brier, "ece": r.ece}
                        for r in reports}}
    return _json(out_dir / "calibration_summary.json", body, places)


FILL_TRADE_COLUMNS = (
    "day", "trade_id", "symbol", "entry_shares", "entry_limit", "entry_fill",
    "entry_vs_limit_usd", "entry_ask", "entry_vs_quote_usd", "entry_quote_age_s",
    "entry_delay_s", "exit_shares", "exit_fill", "exit_bid", "exit_vs_quote_usd",
    "exit_delay_s", "shares_filled", "fees_usd", "cause")
# Compared figures and the replay setting that assumes them (absent: the replay assumes nothing).
FILL_ASSUMED = {
    "entry_slippage_vs_quote_percent": "slippage_percent",
    "stop_slippage_percent": "slippage_percent",
    "exit_slippage_vs_quote_percent": "slippage_percent",
}
FEES_ASSUMPTION = "none: the replay and the backtest are gross of fees (no fee model in config)"


def write_fill_quality(sets: Sequence[tuple[str, Sequence[fq.TradeFill],
                                            Sequence[sl.StoppedTrade]]],
                       assumed: dict[str, Decimal], out_dir: Path, places: int) -> dict[str, Any]:
    """Per set (label, trades, stopped trades): `<label>/fill_quality_trades.csv` and
    `<label>/fill_quality_days.csv` (one row per day, then `all`); `fill_quality_compare.csv`
    (metric, assumed, one column per set, and the second minus the first when there are two);
    `fill_quality_summary.json`."""
    out_dir.mkdir(parents=True, exist_ok=True)
    overall = {}
    for label, trades, stopped in sets:
        folder = out_dir / label
        folder.mkdir(exist_ok=True)
        _write_csv(folder / "fill_quality_trades.csv", FILL_TRADE_COLUMNS,
                   ([getattr(t, c) for c in FILL_TRADE_COLUMNS] for t in trades), places)
        days = sorted({t.day for t in trades})
        rows = [(d, fq.metrics(trades, stopped, d)) for d in [*days, None]]
        columns = ("day", *rows[-1][1])
        _write_csv(folder / "fill_quality_days.csv", columns,
                   ([d if d is not None else "all", *m.values()] for d, m in rows), places)
        overall[label] = rows[-1][1]
    labels = list(overall)
    names = list(overall[labels[0]])
    header = ["metric", "assumed", *labels, *(["difference"] if len(labels) == 2 else [])]

    def line(name: str) -> list[Any]:
        values = [overall[label][name] for label in labels]
        row = [name, assumed.get(FILL_ASSUMED.get(name, ""))]
        row.extend(values)
        if len(labels) == 2:
            a, b = values
            row.append(None if a is None or b is None else b - a)
        return row

    _write_csv(out_dir / "fill_quality_compare.csv", header, (line(n) for n in names), places)
    body = {"sets": overall, "assumed": assumed, "fees_assumption": FEES_ASSUMPTION,
            "difference": "second set minus first set" if len(labels) == 2 else None}
    return _json(out_dir / "fill_quality_summary.json", body, places)


GATE_COLUMNS = ("label", "line", "line_label", "check", "kind", "outcome", "measured", "wanted",
                "inconclusive_because")
GATE_LINE_COLUMNS = ("label", "line", "line_label", "sizing", "sessions", "trades",
                     "unknown_cost_trades", "days", "days_in_plus", "earned", "lost", "net", "fees",
                     "slippage", "spread_in_results", "spread_charged", "same_trade_matched",
                     "same_trade_spread_cost", "net_before_spread")
GATE_MONTH_COLUMNS = ("label", "line", "month", "status", "trading_days", "period_days",
                      "period_fraction_pct", "covered_days", "coverage_pct", "trades", "net",
                      "in_plus")
GATE_TAIL_COLUMNS = ("label", "line", "top_percent", "trades", "net", "share_of_net_pct")
GATE_TRADE_COLUMNS = ("label", "trade_id", "symbol", "session", "day", "entry_time", "exit_time",
                      "notional_usd", "return_units", "result", "fees", "slippage", "spread",
                      "spread_in_result", "spread_charged", "cost_provenance", "cost_version",
                      "net_usd", "spread_usd")
GATE_BY_HAND = "checked by hand"
GATE_KIND = {"net_after_costs_above_0": "descriptive", "sensitivity_k_of_m_months": "sensitivity",
             "sensitivity_without_best": "sensitivity"}
GATE_NOTICE = ("Offline descriptive report. Nothing is promoted from it: binding promotion "
               "criteria are set with the owner on open development evidence (docs/promotion.md). "
               "Dollar "
               "lines sum per-trade results without a portfolio replay (cash, slots, repeated "
               "symbols, protection, session limits, actual exits).")


def _gate_causes(line: gt.Line, check: str) -> str:
    if line.checks[check] != gt.INCONCLUSIVE:
        return ""
    causes = list(line.causes)
    if check == "sensitivity_k_of_m_months":
        causes += [f"{m.month} {m.status}" for m in line.months if m.status != gt.OBSERVED]
    return "; ".join(causes)


def write_gate(result: gt.Gate, version: str, label: str, out_dir: Path, places: int,
               trades: Sequence[gt.Trade], frozen: dict[str, Any],
               settings: ResearchSettings) -> dict[str, Any]:
    """`gate.csv` (each check per line, then the items checked by hand), `gate_lines.csv`,
    `gate_months.csv`, `gate_tails.csv`, `gate_trades.csv` (the per-trade cost audit) and
    `gate_summary.json`. Descriptive wording only: a check "holds", "does not hold" or is
    "inconclusive"; nothing here passes a gate."""
    out_dir.mkdir(parents=True, exist_ok=True)
    window = len(result.window)
    rows: list[list[Any]] = []
    for line in result.lines:
        measured = {"net_after_costs_above_0": line.net,
                    "sensitivity_k_of_m_months": f"{line.months_in_plus} of {window}",
                    "sensitivity_without_best": line.net_without_best}
        wanted = {"net_after_costs_above_0": "net above 0",
                  "sensitivity_k_of_m_months": f"at least {settings.gate_positive_months} of the "
                                               f"last {window} months to --end in plus",
                  "sensitivity_without_best": f"net above 0 without the best {line.dropped} of "
                                              f"{line.trades - line.unknown_cost_trades} trades"}
        rows.extend([label, line.name, line.label, name, GATE_KIND[name], outcome,
                     measured[name], wanted[name], _gate_causes(line, name)]
                    for name, outcome in line.checks.items())
    rows.append([label, "both", "", "paired_baseline", "paired baseline", gt.UNAVAILABLE, "",
                 "", gt.PAIRED_BASELINE_UNAVAILABLE])
    rows.extend([label, "both", "", item, "not a number", GATE_BY_HAND, "", "", ""]
                for item in gt.MANUAL)
    _write_csv(out_dir / "gate.csv", GATE_COLUMNS, rows, places)
    _write_csv(out_dir / "gate_lines.csv", GATE_LINE_COLUMNS,
               ([label, line.name, line.label, *(getattr(line, c) for c in GATE_LINE_COLUMNS[3:])]
                for line in result.lines), places)
    coverage = {m.month: m for m in result.window}
    _write_csv(out_dir / "gate_months.csv", GATE_MONTH_COLUMNS,
               ([label, line.name, m.month, m.status, c.trading_days, c.period_days,
                 c.period_fraction * gt.PERCENT, c.covered_days, c.coverage * gt.PERCENT,
                 m.trades, m.net, m.in_plus]
                for line in result.lines for m in line.months for c in (coverage[m.month],)),
               places)
    _write_csv(out_dir / "gate_tails.csv", GATE_TAIL_COLUMNS,
               ([label, line.name, t.percent, t.trades, t.net, t.share_pct]
                for line in result.lines for t in line.tails), places)
    _write_csv(out_dir / "gate_trades.csv", GATE_TRADE_COLUMNS,
               ([label, t.trade_id, t.symbol, t.session, t.day, t.entry_time, t.exit_time,
                 t.notional_usd, t.return_units, t.result, t.fees, t.slippage, t.spread,
                 t.spread_in_result, t.spread_charged, t.cost_provenance, t.cost_version,
                 t.net(), t.usd(t.spread)] for t in trades),
               places)
    body = {
        "label": label, "model_version": version, "report": "descriptive", "notice": GATE_NOTICE,
        "frozen": frozen,
        "sensitivity_parameters": {
            "k_positive_months": settings.gate_positive_months,
            "m_window_months": settings.gate_window_months,
            "x_drop_best_percent": settings.gate_drop_best_percent},
        "tail_percents": list(settings.gate_tail_percents),
        "coverage_rule": "observed only when every calendar trading day inside start..end "
                         "is in --covered-days; otherwise inconclusive",
        "coverage": {"trading_days": result.trading_days, "covered_days": result.covered_days,
                     "coverage_pct": result.coverage * gt.PERCENT,
                     "window": [{"month": m.month, "status": m.status,
                                 "trading_days": m.trading_days, "period_days": m.period_days,
                                 "period_fraction_pct": m.period_fraction * gt.PERCENT,
                                 "covered_days": m.covered_days,
                                 "coverage_pct": m.coverage * gt.PERCENT}
                                for m in result.window]},
        "costs": {"sign": gt.COST_SIGN, "provenance_version_trades": result.provenance,
                  "unknown_cell": "empty = unknown (line inconclusive); 0 = a valid zero",
                  "same_trade_spread_cost": "diagnostic: spread paid on the model's own trades, "
                                            "over trades with a known net and spread; "
                                            "net_before_spread only when every trade matches"},
        "paired_baseline": {"status": gt.UNAVAILABLE, "why": gt.PAIRED_BASELINE_UNAVAILABLE},
        "conclusive": result.conclusive,
        "checked_by_hand": {item: GATE_BY_HAND for item in gt.MANUAL},
        "lines": {line.name: {"line_label": line.label}
                  | {c: getattr(line, c) for c in GATE_LINE_COLUMNS[3:]}
                  | {"months_in_plus": line.months_in_plus,
                     "dropped_best_trades": line.dropped,
                     "net_without_best": line.net_without_best,
                     "tails": [{"top_percent": t.percent, "trades": t.trades, "net": t.net,
                                "share_of_net_pct": t.share_pct} for t in line.tails],
                     "checks": line.checks, "inconclusive_because": list(line.causes)}
                  for line in result.lines},
    }
    return _json(out_dir / "gate_summary.json", body, places)
