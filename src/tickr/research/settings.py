"""Research settings: every number the outcome analysis uses, from a commented TOML file.

The file has one table `[research]` with exactly the keys in `KEYS`. Missing or unknown keys,
wrong types and out-of-range values are refused with a `ResearchSettingsError`. The what-if
simulation does not use this file: it shares `config/replay.toml` with the replay tool so both
make the same fill assumptions. Output rounding (`output_decimal_places`) has one home too,
`config/replay.toml`, and the research reports read it from there.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from tickr.research.errors import ResearchSettingsError

KEYS = frozenset({"horizons_minutes", "atr_period", "session_timezone", "calibration_buckets",
                  "gate_positive_months", "gate_window_months", "gate_drop_best_percent",
                  "gate_tail_percents"})


@dataclass(frozen=True)
class ResearchSettings:
    horizons_minutes: tuple[int, ...]
    """Minutes after the end of the decision bar at which the price move is measured."""
    atr_period: int
    """Wilder ATR length in M1 bars used to express moves in ATR."""
    session_timezone: ZoneInfo
    """Exchange time zone: a horizon never crosses the decision's session date in it."""
    calibration_buckets: int
    """Equal-width probability buckets of the calibration table (10 = tenths)."""
    gate_positive_months: int
    """R23 gate report, sensitivity check: months in plus needed (K of the last M)."""
    gate_window_months: int
    """R23 gate report, sensitivity check: the last M calendar months, ending with --end."""
    gate_drop_best_percent: Decimal
    """R23 gate report, sensitivity check: percent of trades, best by after-cost result, removed
    before the net is looked at again."""
    gate_tail_percents: tuple[Decimal, ...]
    """R23 gate report: top percents of trades whose share of the net is reported."""


def _int(key: str, raw: Any, minimum: int) -> int:
    if isinstance(raw, bool) or not isinstance(raw, int):
        raise ResearchSettingsError(f"{key} must be an integer, got {type(raw).__name__}")
    if raw < minimum:
        raise ResearchSettingsError(f"{key} must be at least {minimum}: {raw}")
    return raw


def _percent(key: str, raw: Any, *, zero_ok: bool) -> Decimal:
    if isinstance(raw, bool) or not isinstance(raw, int | float):
        raise ResearchSettingsError(f"{key} must be a number")
    value = Decimal(str(raw))
    low_ok = value >= 0 if zero_ok else value > 0
    high_ok = value < 100
    if not value.is_finite() or not low_ok or not high_ok:
        low = "0" if zero_ok else "0 (excluded)"
        high = "100 (excluded)"
        raise ResearchSettingsError(f"{key} must be in {low}..{high}: {raw}")
    return value


def settings_from_mapping(data: dict[str, Any]) -> ResearchSettings:
    # [sealed] (the exam window) is read by research/sealed.py, from the repository's file only.
    if set(data) - {"sealed"} != {"research"} or not isinstance(data["research"], dict):
        raise ResearchSettingsError("settings must contain [research] and no table but [sealed]")
    table = data["research"]
    missing = sorted(KEYS - set(table))
    unknown = sorted(set(table) - KEYS)
    if missing or unknown:
        raise ResearchSettingsError(
            f"[research] keys mismatch: missing={missing} unknown={unknown}"
        )
    raw_horizons = table["horizons_minutes"]
    if not isinstance(raw_horizons, list) or not raw_horizons:
        raise ResearchSettingsError("horizons_minutes must be a non-empty list of integers")
    horizons = tuple(_int("horizons_minutes", h, 1) for h in raw_horizons)
    if any(b <= a for a, b in zip(horizons, horizons[1:], strict=False)):
        raise ResearchSettingsError(f"horizons_minutes must strictly increase: {raw_horizons}")
    zone_name = table["session_timezone"]
    if not isinstance(zone_name, str):
        raise ResearchSettingsError("session_timezone must be a string")
    try:
        zone = ZoneInfo(zone_name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ResearchSettingsError(f"session_timezone is unknown: {zone_name!r}") from exc
    positive = _int("gate_positive_months", table["gate_positive_months"], 1)
    window = _int("gate_window_months", table["gate_window_months"], positive)
    drop = _percent("gate_drop_best_percent", table["gate_drop_best_percent"], zero_ok=True)
    raw_tails = table["gate_tail_percents"]
    if not isinstance(raw_tails, list) or not raw_tails:
        raise ResearchSettingsError("gate_tail_percents must be a non-empty list of numbers")
    tails = tuple(_percent("gate_tail_percents", t, zero_ok=False) for t in raw_tails)
    if any(b <= a for a, b in zip(tails, tails[1:], strict=False)):
        raise ResearchSettingsError(f"gate_tail_percents must strictly increase: {raw_tails}")
    return ResearchSettings(
        horizons_minutes=horizons,
        atr_period=_int("atr_period", table["atr_period"], 1),
        session_timezone=zone,
        calibration_buckets=_int("calibration_buckets", table["calibration_buckets"], 1),
        gate_positive_months=positive,
        gate_window_months=window,
        gate_drop_best_percent=drop,
        gate_tail_percents=tails,
    )


def load_research_settings(path: Path) -> ResearchSettings:
    try:
        with open(path, "rb") as handle:
            data = tomllib.load(handle)
    except tomllib.TOMLDecodeError as exc:
        raise ResearchSettingsError(f"{path}: invalid TOML: {exc}") from exc
    return settings_from_mapping(data)
