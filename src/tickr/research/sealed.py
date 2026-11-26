"""The sealed exam window (backlog R20, docs/promotion.md rung 0): no research command reads it.

The window has ONE home, the `[sealed]` table of the repository's `config/research.toml`, read
from that fixed file (never from a `--settings` argument): closed date ranges plus every day from
a first date on. A day is the exchange-time-zone date (`[research] session_timezone` of the same
file) of the input's time. There is no override flag; the exam unseals only when the owner
changes that table.

Every research command passes the dates it was asked for, or the days of the inputs it loaded,
to `refuse_sealed` before it computes anything. A range or a date set that touches the window is
refused as a whole with `SealedWindowRefused` (cause `sealed_window`): a sealed day is never
dropped and the rest reported, since a report that silently skips days would look complete.

A broken or missing `[sealed]` table is refused too (`ResearchSettingsError`): without a known
window nothing can be proven open.
"""

from __future__ import annotations

import tomllib
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from tickr.research.errors import ResearchError, ResearchSettingsError
from tickr.research.settings import settings_from_mapping

# The repository's research settings: <root>/src/tickr/research/sealed.py -> <root>/config.
SEALED_FILE = Path(__file__).resolve().parents[3] / "config" / "research.toml"
KEYS = frozenset({"ranges", "every_day_from"})
PAIR = 2  # a range is [first, last]


class SealedWindowRefused(ResearchError):
    """A requested range or a loaded input touches the sealed exam window."""

    CAUSE = "sealed_window"

    def __init__(self, what: str, day: date) -> None:
        super().__init__(f"{self.CAUSE}: {what} touches the sealed exam window on "
                         f"{day.isoformat()} (config/research.toml [sealed])")
        self.cause = self.CAUSE
        self.what = what
        self.day = day


@dataclass(frozen=True)
class SealedWindow:
    ranges: tuple[tuple[date, date], ...]
    """Closed ranges, first and last day both sealed."""
    every_day_from: date
    """This day and every later one is sealed."""

    def first_sealed(self, first: date, last: date) -> date | None:
        """The first sealed day of first..last (both ends), or None when all of it is open."""
        hits = [max(first, a) for a, b in self.ranges if a <= last and b >= first]
        if last >= self.every_day_from:
            hits.append(max(first, self.every_day_from))
        return min(hits, default=None)


def _day(key: str, raw: Any) -> date:
    if isinstance(raw, datetime) or not isinstance(raw, date):
        raise ResearchSettingsError(f"[sealed] {key} must hold TOML dates (YYYY-MM-DD)")
    return raw


def window_from_mapping(data: dict[str, Any]) -> SealedWindow:
    table = data.get("sealed")
    if not isinstance(table, dict) or set(table) != KEYS:
        raise ResearchSettingsError(f"[sealed] must hold exactly the keys {sorted(KEYS)}")
    raw = table["ranges"]
    if not isinstance(raw, list):
        raise ResearchSettingsError("[sealed] ranges must be a list of [first, last] dates")
    ranges = []
    for pair in raw:
        if not isinstance(pair, list) or len(pair) != PAIR:
            raise ResearchSettingsError("[sealed] ranges must be a list of [first, last] dates")
        first, last = _day("ranges", pair[0]), _day("ranges", pair[1])
        if first > last:
            raise ResearchSettingsError(f"[sealed] range {first}..{last} ends before it starts")
        ranges.append((first, last))
    return SealedWindow(tuple(ranges), _day("every_day_from", table["every_day_from"]))


def load(path: Path = SEALED_FILE) -> tuple[SealedWindow, ZoneInfo]:
    """(the window, the exchange time zone) from the repository's research settings."""
    try:
        with open(path, "rb") as handle:
            data = tomllib.load(handle)
    except tomllib.TOMLDecodeError as exc:
        raise ResearchSettingsError(f"{path}: invalid TOML: {exc}") from exc
    return window_from_mapping(data), settings_from_mapping(data).session_timezone


def refuse_sealed(what: str, *, first: date | None = None, last: date | None = None,
                  days: Iterable[date | datetime] = ()) -> None:
    """Refuse when the range first..last or any of `days` touches the sealed window.

    A `datetime` counts on its exchange-time-zone date (it must be aware). `what` names the
    input in the refusal, for example `--start..--end` or `--bars`."""
    window, zone = load()
    if (first is None) != (last is None):
        raise ResearchError(f"{what}: a range needs both its first and its last day")
    if first is not None and last is not None:
        hit = window.first_sealed(first, last)
        if hit is not None:
            raise SealedWindowRefused(what, hit)
    for item in days:
        if isinstance(item, datetime):
            if item.utcoffset() is None:
                raise ResearchError(f"{what}: a time without a UTC offset: {item.isoformat()}")
            item = item.astimezone(zone).date()
        if window.first_sealed(item, item) is not None:
            raise SealedWindowRefused(what, item)
