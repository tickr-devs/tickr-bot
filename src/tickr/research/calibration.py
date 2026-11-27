"""Calibration check "70 means 70" (R29): a reliability table per probability column.

Input is one CSV of stored model answers (format in docs/research.md): a header row, a 0/1
`outcome` column, a `day` column (the answer's trading day, YYYY-MM-DD, checked against the
sealed exam window) and one or more `p_<name>` probability columns. An answer is bucketed by
floor(p x buckets), with p = 1 in the last bucket. Per bucket: the count, the mean predicted
probability, the observed rate of outcome 1 and the gap (observed - predicted). Overall: the
Brier score, the expected calibration error (count-weighted mean absolute gap) and the base
rate. Report only: there is no pass/fail threshold, the owner has not chosen one.

A blank probability cell means that layer gave no answer for the row (a second layer may see
only some rows): it is skipped and counted. A bad probability or outcome is refused with file,
line and cause; nothing is clipped or repaired.
"""

from __future__ import annotations

import csv
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path

from tickr.archive import open_text
from tickr.research.errors import ResearchError

OUTCOME_COLUMN = "outcome"
DAY_COLUMN = "day"
PROBABILITY_PREFIX = "p_"


@dataclass(frozen=True)
class Bucket:
    index: int
    low: Decimal
    high: Decimal
    count: int
    mean_predicted: Decimal | None  # None when the bucket is empty
    observed_rate: Decimal | None
    gap: Decimal | None  # observed - predicted


@dataclass(frozen=True)
class ColumnReport:
    column: str
    answers: int
    skipped_blank: int
    base_rate: Decimal | None
    brier: Decimal | None
    ece: Decimal | None
    buckets: tuple[Bucket, ...]


def _number(where: str, text: str, what: str) -> Decimal:
    try:
        value = Decimal(text.strip())
    except InvalidOperation as exc:
        raise ResearchError(f"{where}: {what} is not a number: {text!r}") from exc
    if not value.is_finite():
        raise ResearchError(f"{where}: {what} is not finite: {text!r}")
    return value


def load_answers(path: Path) -> tuple[list[str], list[tuple[int, dict[str, str]]]]:
    """(probability column names, [(line number, row)]) from a stored-answers CSV."""
    with open_text(path) as handle:
        reader = csv.DictReader(handle)
        header = reader.fieldnames or []
        for required in (OUTCOME_COLUMN, DAY_COLUMN):
            if required not in header:
                raise ResearchError(f"{path}: no {required!r} column")
        columns = [c for c in header if c.startswith(PROBABILITY_PREFIX)]
        if not columns:
            raise ResearchError(
                f"{path}: no probability column (names start {PROBABILITY_PREFIX!r})")
        rows = []
        for row in reader:
            if None in row or None in row.values():
                raise ResearchError(f"{path}:{reader.line_num}: wrong number of cells")
            rows.append((reader.line_num, row))
    if not rows:
        raise ResearchError(f"{path}: no answers")
    return columns, rows


def answer_days(path: Path) -> list[date]:
    """The trading day of every stored answer, for the sealed-window check; a bad one refused."""
    _, rows = load_answers(path)
    days = []
    for number, row in rows:
        try:
            days.append(date.fromisoformat(row[DAY_COLUMN].strip()))
        except ValueError as exc:
            raise ResearchError(
                f"{path}:{number}: {DAY_COLUMN} is not a YYYY-MM-DD date: "
                f"{row[DAY_COLUMN]!r}") from exc
    return days


def reliability(path: Path, rows: Sequence[tuple[int, dict[str, str]]], column: str,
                buckets: int) -> ColumnReport:
    counts = [0] * buckets
    predicted = [Decimal(0)] * buckets
    hits = [0] * buckets
    skipped = 0
    squared = Decimal(0)
    for number, row in rows:
        where = f"{path}:{number}"
        raw = row[column]
        if not raw.strip():
            skipped += 1
            continue
        p = _number(where, raw, column)
        if not 0 <= p <= 1:
            raise ResearchError(f"{where}: {column} is outside 0..1: {raw!r}")
        outcome = _number(where, row[OUTCOME_COLUMN], OUTCOME_COLUMN)
        if outcome not in (0, 1):
            raise ResearchError(
                f"{where}: {OUTCOME_COLUMN} is not 0 or 1: {row[OUTCOME_COLUMN]!r}")
        i = min(int(p * buckets), buckets - 1)
        counts[i] += 1
        predicted[i] += p
        hits[i] += int(outcome)
        squared += (p - outcome) ** 2
    total = sum(counts)
    out = []
    ece = Decimal(0)
    for i in range(buckets):
        low, high = Decimal(i) / buckets, Decimal(i + 1) / buckets
        if counts[i] == 0:
            out.append(Bucket(i, low, high, 0, None, None, None))
            continue
        mean_p = predicted[i] / counts[i]
        rate = Decimal(hits[i]) / counts[i]
        out.append(Bucket(i, low, high, counts[i], mean_p, rate, rate - mean_p))
        ece += Decimal(counts[i]) * abs(rate - mean_p)
    if total == 0:
        return ColumnReport(column, 0, skipped, None, None, None, tuple(out))
    return ColumnReport(column, total, skipped, Decimal(sum(hits)) / total, squared / total,
                        ece / total, tuple(out))


def calibration(path: Path, buckets: int) -> list[ColumnReport]:
    columns, rows = load_answers(path)
    return [reliability(path, rows, c, buckets) for c in columns]
