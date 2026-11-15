"""Backtest settings from `config/backtest.toml` (one comment per key, tested).

Exactly the keys in `KEYS` under `[backtest]`; missing, unknown, mistyped or out-of-range values
are refused. Platform values (universe lists, dollar-volume floor, request sizes, risk limits)
are read from `config/bot.toml`, simulation values from `config/replay.toml`, never duplicated.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

from tickr.backtest.errors import BacktestError

KEYS = frozenset({
    "gainer_change_percent",
    "max_pool_per_day",
    "instrument_max_pages",
    "minute_max_pages_per_run",
    "symbols_per_wave",
    "workers",
    "bootstrap_samples",
    "bootstrap_seed",
    "confidence_percent",
    "price_bucket_edges_usd",
    "split_tolerance_percent",
    "holdout_percent",
    "instrument_statuses",
    "liquid_top_count",
    "liquid_min_price_usd",
    "nightly_window_sessions",
    "nightly_max_new_sessions",
    "nightly_min_free_disk_mb",
    "nightly_stop_minutes_before_open",
})


@dataclass(frozen=True)
class BacktestSettings:
    gainer_change_percent: Decimal
    max_pool_per_day: int
    instrument_max_pages: int
    minute_max_pages_per_run: int
    symbols_per_wave: int
    workers: int
    bootstrap_samples: int
    bootstrap_seed: int
    confidence_percent: Decimal
    price_bucket_edges_usd: tuple[Decimal, ...]
    split_tolerance_percent: Decimal
    holdout_percent: Decimal
    instrument_statuses: tuple[str, ...]
    liquid_top_count: int
    liquid_min_price_usd: Decimal
    nightly_window_sessions: int
    nightly_max_new_sessions: int
    nightly_min_free_disk_mb: int
    nightly_stop_minutes_before_open: int


def _count(key: str, raw: Any, minimum: int) -> int:
    if isinstance(raw, bool) or not isinstance(raw, int) or raw < minimum:
        raise BacktestError(f"[backtest] {key} must be an integer >= {minimum}, got {raw!r}")
    return raw


def _number(key: str, raw: Any) -> Decimal:
    if isinstance(raw, bool) or not isinstance(raw, int | float):
        raise BacktestError(f"[backtest] {key} must be a number, got {raw!r}")
    value = Decimal(str(raw))
    if value <= 0:
        raise BacktestError(f"[backtest] {key} must be positive, got {raw!r}")
    return value


def settings_from_mapping(data: dict[str, Any]) -> BacktestSettings:
    if set(data) != {"backtest"} or not isinstance(data["backtest"], dict):
        raise BacktestError("settings must contain exactly one table: [backtest]")
    table = data["backtest"]
    missing, unknown = sorted(KEYS - set(table)), sorted(set(table) - KEYS)
    if missing or unknown:
        raise BacktestError(f"[backtest] keys mismatch: missing={missing} unknown={unknown}")
    confidence = _number("confidence_percent", table["confidence_percent"])
    if confidence >= 100:
        raise BacktestError("[backtest] confidence_percent must be below 100")
    return BacktestSettings(
        gainer_change_percent=_number("gainer_change_percent", table["gainer_change_percent"]),
        max_pool_per_day=_count("max_pool_per_day", table["max_pool_per_day"], 1),
        instrument_max_pages=_count("instrument_max_pages", table["instrument_max_pages"], 1),
        minute_max_pages_per_run=_count(
            "minute_max_pages_per_run", table["minute_max_pages_per_run"], 1),
        symbols_per_wave=_count("symbols_per_wave", table["symbols_per_wave"], 1),
        workers=_count("workers", table["workers"], 1),
        bootstrap_samples=_count("bootstrap_samples", table["bootstrap_samples"], 1),
        bootstrap_seed=_count("bootstrap_seed", table["bootstrap_seed"], 0),
        confidence_percent=confidence,
        price_bucket_edges_usd=_edges(table["price_bucket_edges_usd"]),
        split_tolerance_percent=_number("split_tolerance_percent",
                                        table["split_tolerance_percent"]),
        holdout_percent=_percent("holdout_percent", table["holdout_percent"]),
        instrument_statuses=_texts("instrument_statuses", table["instrument_statuses"]),
        liquid_top_count=_count("liquid_top_count", table["liquid_top_count"], 1),
        liquid_min_price_usd=_number("liquid_min_price_usd", table["liquid_min_price_usd"]),
        nightly_window_sessions=_count("nightly_window_sessions",
                                       table["nightly_window_sessions"], 1),
        nightly_max_new_sessions=_count("nightly_max_new_sessions",
                                        table["nightly_max_new_sessions"], 1),
        nightly_min_free_disk_mb=_count("nightly_min_free_disk_mb",
                                        table["nightly_min_free_disk_mb"], 0),
        nightly_stop_minutes_before_open=_count("nightly_stop_minutes_before_open",
                                                table["nightly_stop_minutes_before_open"], 0),
    )


def _texts(key: str, raw: Any) -> tuple[str, ...]:
    if not isinstance(raw, list) or not raw or not all(isinstance(x, str) and x for x in raw):
        raise BacktestError(f"[backtest] {key} must be a non-empty list of strings")
    return tuple(raw)


def _percent(key: str, raw: Any) -> Decimal:
    value = _number(key, raw)
    if value >= 100:
        raise BacktestError(f"[backtest] {key} must be below 100")
    return value


def _edges(raw: Any) -> tuple[Decimal, ...]:
    if not isinstance(raw, list) or not raw:
        raise BacktestError("[backtest] price_bucket_edges_usd must be a non-empty list")
    edges = tuple(_number("price_bucket_edges_usd", x) for x in raw)
    if list(edges) != sorted(set(edges)):
        raise BacktestError("[backtest] price_bucket_edges_usd must be strictly increasing")
    return edges


def load_settings(path: Path) -> BacktestSettings:
    try:
        with open(path, "rb") as handle:
            data = tomllib.load(handle)
    except tomllib.TOMLDecodeError as exc:
        raise BacktestError(f"{path}: invalid TOML: {exc}") from exc
    return settings_from_mapping(data)
