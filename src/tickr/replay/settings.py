"""Replay settings: every number the simulation uses, loaded from a commented TOML file.

The file has one table `[replay]` with exactly the keys in `KEYS`. Missing or unknown keys,
wrong types and out-of-range values are refused with a `SettingsError`. An example with the
required comment for every key is in `docs/replay.md`.

`measured_slippage_file` (empty: none) selects a file written by `python -m tickr.research
slippage`; its measured stop slippage then replaces `slippage_percent` (backlog R5). A relative
path is read from the settings file's folder. A missing file, a malformed one or one with fewer
than `measured_slippage_min_trades` measured stopped trades is refused, never replaced by the
fixed value.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

from tickr.replay.errors import MeasuredSlippageError, SettingsError
from tickr.replay.measured import load_measured_slippage

KEYS = frozenset(
    {
        "slippage_percent",
        "slippage_min_usd",
        "entry_wait_bars",
        "output_decimal_places",
        "history_sessions",
        "synthesize_quote",
        "synthetic_spread_percent",
        "synthetic_spread_min_usd",
        "synthetic_quote_size_shares",
        "measured_slippage_file",
        "measured_slippage_min_trades",
    }
)


@dataclass(frozen=True)
class ReplaySettings:
    slippage_percent: Decimal
    """Adverse slippage per simulated fill, percent of the fill price."""
    slippage_min_usd: Decimal
    """Minimum adverse slippage per simulated fill, USD per share."""
    entry_wait_bars: int
    """Bars after the ENTER decision during which the simulated entry may fill."""
    output_decimal_places: int
    """Decimal places for prices, R and percent in the outputs."""
    history_sessions: int
    """Prior sessions of the same symbol passed to the strategy as history."""
    synthesize_quote: bool
    """True: give the strategy a synthetic quote around each completed bar's close."""
    synthetic_spread_percent: Decimal
    """Synthetic quote spread (ask - bid), percent of the close."""
    synthetic_spread_min_usd: Decimal
    """Minimum synthetic quote spread, USD."""
    synthetic_quote_size_shares: Decimal
    """Bid and ask size of the synthetic quote, shares."""
    measured_slippage_file: Path | None = None
    """The measured slippage file `slippage_percent` came from (None: the fixed value)."""

    def slippage(self, price: Decimal) -> Decimal:
        """Adverse slippage per share for a fill at `price`."""
        return max(price * self.slippage_percent / 100, self.slippage_min_usd)

    def synthetic_spread(self, price: Decimal) -> Decimal:
        """Spread (ask - bid) of the synthetic quote around `price`."""
        return max(price * self.synthetic_spread_percent / 100, self.synthetic_spread_min_usd)


def _number(key: str, raw: Any) -> Decimal:
    if isinstance(raw, bool) or not isinstance(raw, int | float):
        raise SettingsError(f"{key} must be a number, got {type(raw).__name__}")
    value = Decimal(str(raw))
    if value < 0:
        raise SettingsError(f"{key} must not be negative: {raw}")
    return value


def _count(key: str, raw: Any, minimum: int) -> int:
    if isinstance(raw, bool) or not isinstance(raw, int):
        raise SettingsError(f"{key} must be an integer, got {type(raw).__name__}")
    if raw < minimum:
        raise SettingsError(f"{key} must be at least {minimum}: {raw}")
    return raw


def settings_from_mapping(data: dict[str, Any], base_dir: Path | None = None) -> ReplaySettings:
    """`base_dir` is the settings file's folder: a relative `measured_slippage_file` is read
    from it (refused without one)."""
    if set(data) != {"replay"} or not isinstance(data["replay"], dict):
        raise SettingsError("settings must contain exactly one table: [replay]")
    table = data["replay"]
    missing = sorted(KEYS - set(table))
    unknown = sorted(set(table) - KEYS)
    if missing or unknown:
        raise SettingsError(f"[replay] keys mismatch: missing={missing} unknown={unknown}")
    flag = table["synthesize_quote"]
    if not isinstance(flag, bool):
        raise SettingsError("synthesize_quote must be true or false")
    slippage_percent = _number("slippage_percent", table["slippage_percent"])
    min_trades = _count("measured_slippage_min_trades", table["measured_slippage_min_trades"], 1)
    name = table["measured_slippage_file"]
    if not isinstance(name, str):
        raise SettingsError("measured_slippage_file must be a string (empty: none)")
    measured: Path | None = None
    if name:
        measured = Path(name)
        if not measured.is_absolute():
            if base_dir is None:
                raise MeasuredSlippageError(
                    f"measured_slippage_file {name!r} is relative and no settings folder is "
                    "known")
            measured = base_dir / measured
        slippage_percent = load_measured_slippage(measured, min_trades)
    return ReplaySettings(
        slippage_percent=slippage_percent,
        slippage_min_usd=_number("slippage_min_usd", table["slippage_min_usd"]),
        entry_wait_bars=_count("entry_wait_bars", table["entry_wait_bars"], 1),
        output_decimal_places=_count("output_decimal_places", table["output_decimal_places"], 0),
        history_sessions=_count("history_sessions", table["history_sessions"], 0),
        synthesize_quote=flag,
        synthetic_spread_percent=_number(
            "synthetic_spread_percent", table["synthetic_spread_percent"]
        ),
        synthetic_spread_min_usd=_number(
            "synthetic_spread_min_usd", table["synthetic_spread_min_usd"]
        ),
        synthetic_quote_size_shares=Decimal(
            _count("synthetic_quote_size_shares", table["synthetic_quote_size_shares"], 1)
        ),
        measured_slippage_file=measured,
    )


def load_settings(path: Path) -> ReplaySettings:
    try:
        with open(path, "rb") as handle:
            data = tomllib.load(handle)
    except tomllib.TOMLDecodeError as exc:
        raise SettingsError(f"{path}: invalid TOML: {exc}") from exc
    return settings_from_mapping(data, Path(path).parent)
