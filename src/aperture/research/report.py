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

from aperture.research import observer_report as orep
from aperture.research import outcomes as oc
from aperture.research import refusals as rf
from aperture.research import whatif as wi
from aperture.research.compare import DiffResult
from aperture.research.records import DecisionRecord


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
                diff.get("same"), diff.get("verdict_changed"), diff.get("levels_changed")]

    _write_csv(out_dir / "observers.csv", OBSERVER_COLUMNS,
               (line(r) for r in result.identities), places)
    body = {"label": orep.LABEL, "active": result.active,
            "identities": [asdict(r) for r in result.identities]}
    return _json(out_dir / "observers.json", body, places)


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
