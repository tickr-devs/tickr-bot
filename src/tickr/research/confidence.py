"""What-if: size by model confidence against flat size, on stored model answers. SIMULATED.

Input CSV, one row per trade the model answered. It follows the stored-answers convention of the
calibration check (R29, docs/research.md) and adds three columns of its own. Header required,
other columns ignored:
  outcome      0 or 1, the event the probability predicts (shared convention, validated)
  p_<name>     a probability 0..1; `--probability-column` names it (shared convention); a blank
               cell means the layer gave no answer, and a trade without one cannot be sized: refused
  day          (own) trading day, YYYY-MM-DD
  session      (own) `regular` or `extended`
  result_pct   (own) the trade's net result after fees, percent of its notional (signed)

Two money lines (docs/promotion.md): main session = `regular` rows, each position at the
position cap (`[risk] position_notional_cap_usd`); all sessions = every row, each position at
`--all-sessions-usd`. The sized variant uses the `[confidence_sizing]` bounds (or `--min-usd`,
`--max-usd`), which scale with the line: size = bound * line / cap, so the cap line uses them as is.
Extended sessions do not trade yet, so rows may all be `regular`; nothing is estimated.
"""

from __future__ import annotations

import csv
from collections import defaultdict
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path

from tickr.research.errors import ResearchError
from tickr.risk.confidence_sizing import size_usd

SESSIONS = ("regular", "extended")
COLUMNS = ("outcome", "day", "session", "result_pct")
PREFIX = "p_"
PERCENT = Decimal(100)  # unit factor: percent to fraction


@dataclass(frozen=True)
class Answer:
    day: str
    session: str
    probability: Decimal | None  # None: blank cell, the layer gave no answer
    result_pct: Decimal


@dataclass(frozen=True)
class Line:
    earned: Decimal
    lost: Decimal
    net: Decimal
    days_in_plus: int
    days: int
    trades: int


def load(path: Path, column: str | None) -> list[Answer]:
    with open(path, newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        header = reader.fieldnames or []
        missing = [c for c in COLUMNS if c not in header]
        if missing:
            raise ResearchError(f"{path}: missing columns {', '.join(missing)}")
        candidates = [c for c in header if c.startswith(PREFIX)]
        if column is None and len(candidates) == 1:
            column = candidates[0]
        if column not in candidates:
            raise ResearchError(f"{path}: name one probability column with "
                                f"--probability-column (found: {', '.join(candidates) or 'none'})")
        out = []
        for n, row in enumerate(reader, start=2):
            try:
                raw = (row[column] or "").strip()
                probability = Decimal(raw) if raw else None
                result = Decimal(row["result_pct"])
                outcome = Decimal(row["outcome"])
            except (InvalidOperation, TypeError) as exc:
                raise ResearchError(
                    f"{path}:{n}: {column}, outcome or result_pct is not a number") from exc
            if outcome not in (0, 1):
                raise ResearchError(f"{path}:{n}: outcome is not 0 or 1")
            if row["session"] not in SESSIONS:
                raise ResearchError(f"{path}:{n}: session must be one of {', '.join(SESSIONS)}")
            if not row["day"] or not result.is_finite():
                raise ResearchError(f"{path}:{n}: day empty or result_pct not finite")
            out.append(Answer(row["day"], row["session"], probability, result))
    if not out:
        raise ResearchError(f"{path}: no rows")
    return out


def _line(results: list[tuple[str, Decimal]]) -> Line:
    """`results` are (day, result in USD) pairs."""
    per_day: dict[str, Decimal] = defaultdict(Decimal)
    earned = lost = Decimal(0)
    for day, usd in results:
        per_day[day] += usd
        if usd > 0:
            earned += usd
        elif usd < 0:
            lost += -usd
    return Line(earned, lost, earned - lost, sum(1 for v in per_day.values() if v > 0),
                len(per_day), len(results))


def compare(answers: list[Answer], cap_usd: Decimal, all_sessions_usd: Decimal,
            min_usd: Decimal, max_usd: Decimal) -> dict[str, dict[str, Line]]:
    """Per money line, `flat` and `sized` results. Raises SizingRefused on a bad row or bounds."""
    if all_sessions_usd <= 0:
        raise ResearchError("--all-sessions-usd must be above 0")
    lines = {"main_session": ("regular", cap_usd), "all_sessions": (None, all_sessions_usd)}
    out: dict[str, dict[str, Line]] = {}
    for name, (session, usd) in lines.items():
        scale = usd / cap_usd
        flat, sized = [], []
        for a in answers:
            if session is not None and a.session != session:
                continue
            size = size_usd(a.probability, min_usd, max_usd, cap_usd) * scale
            flat.append((a.day, usd * a.result_pct / PERCENT))
            sized.append((a.day, size * a.result_pct / PERCENT))
        out[name] = {"flat": _line(flat), "sized": _line(sized)}
    return out
