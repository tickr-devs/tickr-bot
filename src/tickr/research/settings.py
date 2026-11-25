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
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from tickr.research.errors import ResearchSettingsError

KEYS = frozenset({"horizons_minutes", "atr_period", "session_timezone", "calibration_buckets"})


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


def _int(key: str, raw: Any, minimum: int) -> int:
    if isinstance(raw, bool) or not isinstance(raw, int):
        raise ResearchSettingsError(f"{key} must be an integer, got {type(raw).__name__}")
    if raw < minimum:
        raise ResearchSettingsError(f"{key} must be at least {minimum}: {raw}")
    return raw


def settings_from_mapping(data: dict[str, Any]) -> ResearchSettings:
    if set(data) != {"research"} or not isinstance(data["research"], dict):
        raise ResearchSettingsError("settings must contain exactly one table: [research]")
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
    return ResearchSettings(
        horizons_minutes=horizons,
        atr_period=_int("atr_period", table["atr_period"], 1),
        session_timezone=zone,
        calibration_buckets=_int("calibration_buckets", table["calibration_buckets"], 1),
    )


def load_research_settings(path: Path) -> ResearchSettings:
    try:
        with open(path, "rb") as handle:
            data = tomllib.load(handle)
    except tomllib.TOMLDecodeError as exc:
        raise ResearchSettingsError(f"{path}: invalid TOML: {exc}") from exc
    return settings_from_mapping(data)
