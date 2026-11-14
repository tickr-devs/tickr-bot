"""Load completed M1 bars from JSONL files.

One JSON object per line with exactly the keys `symbol`, `start`, `open`, `high`, `low`,
`close`, `volume`. `start` is ISO-8601 with an explicit UTC offset of zero; prices and volume
are JSON strings parsed as Decimal (floats are refused because they lose precision). Blank
lines are ignored. Any other deviation is a `BarFileError` naming the file, line and cause.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path

from aperture.replay.errors import BarFileError
from aperture.types import Bar

BAR_KEYS = frozenset({"symbol", "start", "open", "high", "low", "close", "volume"})
PRICE_KEYS = ("open", "high", "low", "close")


def _decimal(path: str, line: int, key: str, raw: object) -> Decimal:
    if not isinstance(raw, str):
        raise BarFileError(path, line, f"{key} must be a string, got {type(raw).__name__}")
    try:
        value = Decimal(raw)
    except InvalidOperation as exc:
        raise BarFileError(path, line, f"{key} is not a decimal: {raw!r}") from exc
    if not value.is_finite():
        raise BarFileError(path, line, f"{key} is not finite: {raw!r}")
    return value


def _start(path: str, line: int, raw: object) -> datetime:
    if not isinstance(raw, str):
        raise BarFileError(path, line, "start must be an ISO-8601 string")
    try:
        start = datetime.fromisoformat(raw)
    except ValueError as exc:
        raise BarFileError(path, line, f"start is not ISO-8601: {raw!r}") from exc
    if start.tzinfo is None or start.utcoffset() != timedelta(0):
        raise BarFileError(path, line, f"start must be UTC with an explicit offset: {raw!r}")
    if start.second != 0 or start.microsecond != 0:
        raise BarFileError(path, line, f"start is not on a whole minute: {raw!r}")
    return start


def parse_bar(path: str, line: int, text: str) -> Bar:
    try:
        record = json.loads(text)
    except json.JSONDecodeError as exc:
        raise BarFileError(path, line, f"invalid JSON: {exc.msg}") from exc
    if not isinstance(record, dict):
        raise BarFileError(path, line, "record is not a JSON object")
    keys = set(record)
    if keys != BAR_KEYS:
        missing = sorted(BAR_KEYS - keys)
        unknown = sorted(keys - BAR_KEYS)
        raise BarFileError(path, line, f"keys mismatch: missing={missing} unknown={unknown}")
    symbol = record["symbol"]
    if not isinstance(symbol, str) or not symbol or symbol != symbol.strip():
        raise BarFileError(path, line, f"symbol is not a non-empty string: {symbol!r}")
    prices = {key: _decimal(path, line, key, record[key]) for key in PRICE_KEYS}
    volume = _decimal(path, line, "volume", record["volume"])
    if any(price <= 0 for price in prices.values()):
        raise BarFileError(path, line, "prices must be positive")
    if volume < 0:
        raise BarFileError(path, line, "volume must not be negative")
    if prices["high"] < max(prices["open"], prices["close"], prices["low"]):
        raise BarFileError(path, line, "high is below open, close or low")
    if prices["low"] > min(prices["open"], prices["close"]):
        raise BarFileError(path, line, "low is above open or close")
    return Bar(
        symbol=symbol,
        start=_start(path, line, record["start"]),
        volume=volume,
        **prices,
    )


def load_bars(paths: Iterable[Path]) -> dict[str, tuple[Bar, ...]]:
    """Read every file and return bars per symbol, oldest first.

    Within one symbol, starts must be strictly increasing across all files in the given order
    (duplicates and out-of-order bars are refused, never sorted or merged silently).
    """
    by_symbol: dict[str, list[Bar]] = {}
    where: dict[str, tuple[str, int]] = {}
    for path in paths:
        name = str(path)
        with open(path, encoding="utf-8") as handle:
            for number, text in enumerate(handle, start=1):
                if not text.strip():
                    continue
                bar = parse_bar(name, number, text)
                series = by_symbol.setdefault(bar.symbol, [])
                if series and bar.start <= series[-1].start:
                    prev_file, prev_line = where[bar.symbol]
                    raise BarFileError(
                        name,
                        number,
                        f"{bar.symbol} bar at {bar.start.isoformat()} is not after the bar "
                        f"at {series[-1].start.isoformat()} ({prev_file}:{prev_line})",
                    )
                series.append(bar)
                where[bar.symbol] = (name, number)
    return {symbol: tuple(bars) for symbol, bars in by_symbol.items()}
