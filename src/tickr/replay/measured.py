"""The measured slippage file (backlog R5): written by `python -m tickr.research slippage`,
read by the replay settings when `measured_slippage_file` selects it.

One JSON object. The replay reads only `format`, `measured_trades` and
`stop_slippage_percent` (a decimal string); everything else in it is for people.
"""

from __future__ import annotations

import json
from decimal import Decimal, InvalidOperation
from pathlib import Path

from tickr.replay.errors import MeasuredSlippageError

FORMAT = "tickr-measured-slippage-v1"
# `slippage_operations.json` of the same command: every live day, the sealed exam window
# included (owner ruling 2026-11-26). Operations evidence only, never a replay input.
OPERATIONS_FORMAT = "tickr-slippage-operations-v1"


def load_measured_slippage(path: Path, min_trades: int) -> Decimal:
    """`stop_slippage_percent` of the file at `path`, refused when the file is missing or
    malformed, measured fewer than `min_trades` stopped trades, or measured a negative value
    (the replay never assumes a better fill than planned)."""
    if not path.is_file():
        raise MeasuredSlippageError(f"measured slippage file not found: {path}")
    try:
        body = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise MeasuredSlippageError(f"{path}: not a readable JSON file: {exc}") from exc
    if isinstance(body, dict) and body.get("format") == OPERATIONS_FORMAT:
        raise MeasuredSlippageError(
            f"{path}: an operations summary of live days (sealed days included) is never a "
            "replay input; select slippage_summary.json")
    if not isinstance(body, dict) or body.get("format") != FORMAT:
        raise MeasuredSlippageError(f"{path}: format is not {FORMAT!r}")
    count = body.get("measured_trades")
    if isinstance(count, bool) or not isinstance(count, int):
        raise MeasuredSlippageError(f"{path}: measured_trades is not an integer")
    if count < min_trades:
        raise MeasuredSlippageError(
            f"{path}: {count} measured stopped trades, measured_slippage_min_trades is "
            f"{min_trades}")
    raw = body.get("stop_slippage_percent")
    if not isinstance(raw, str):
        raise MeasuredSlippageError(f"{path}: stop_slippage_percent is not a decimal string")
    try:
        value = Decimal(raw)
    except InvalidOperation as exc:
        raise MeasuredSlippageError(f"{path}: stop_slippage_percent is not a number: {raw!r}") \
            from exc
    if not value.is_finite() or value < 0:
        raise MeasuredSlippageError(
            f"{path}: stop_slippage_percent {raw} is negative or not finite; the replay "
            "never assumes a fill better than planned")
    return value
