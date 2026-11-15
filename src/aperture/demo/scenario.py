"""The demo scenario, loaded strictly from `config/demo.toml` (every number lives there).

Unknown or missing keys, wrong types and bad values are refused with a `ScenarioError` naming
the single cause; nothing is defaulted.
"""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass
from datetime import time
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from aperture.api.contract import CommandKind
from aperture.data.base import ScreenerKind
from aperture.demo.world import Leg

SYMBOL = re.compile(r"^[A-Z][A-Z0-9.]*$")
LEG_FIELDS = 4  # from, to, price at the end, volume per minute


class ScenarioError(ValueError):
    """The demo file is not a valid scenario; the message is the single cause."""


@dataclass(frozen=True)
class SymbolPlan:
    symbol: str
    base: Decimal
    volume: Decimal
    screener: ScreenerKind
    legs: tuple[Leg, ...]


@dataclass(frozen=True)
class DemoCommand:
    at: str
    kind: CommandKind
    symbol: str | None


@dataclass(frozen=True)
class Scenario:
    root_dir: str
    environment: str
    api_port: int
    observers: tuple[str, ...]
    provider: str
    bot_start: str
    past_stop: str
    clock: str
    half_spread: Decimal
    quote_size: Decimal
    wick: Decimal
    history_volume: Decimal
    today: tuple[SymbolPlan, ...]
    commands: tuple[DemoCommand, ...]
    past: tuple[tuple[SymbolPlan, ...], ...]


def _keys(where: str, raw: Any, keys: set[str]) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ScenarioError(f"{where} must be a table")
    if set(raw) != keys:
        missing, unknown = keys - set(raw), set(raw) - keys
        raise ScenarioError(f"{where}: missing {sorted(missing)}, unknown {sorted(unknown)}")
    return raw


def _text(where: str, value: Any) -> str:
    if not isinstance(value, str) or not value:
        raise ScenarioError(f"{where} must be a non-empty string")
    return value


def _clock(where: str, value: Any) -> str:
    text = _text(where, value)
    try:
        time.fromisoformat(text)
    except ValueError as exc:
        raise ScenarioError(f"{where} is not a time HH:MM: {text!r}") from exc
    return text


def _positive(where: str, value: Any) -> Decimal:
    try:
        number = Decimal(_text(where, value))
    except InvalidOperation as exc:
        raise ScenarioError(f"{where} is not a decimal string: {value!r}") from exc
    if not number.is_finite() or number <= 0:
        raise ScenarioError(f"{where} must be above zero: {value!r}")
    return number


def _count(where: str, value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ScenarioError(f"{where} must be a positive integer")
    return value


def _symbol(where: str, raw: Any) -> SymbolPlan:
    row = _keys(where, raw, {"symbol", "base", "volume", "screener", "legs"})
    symbol = _text(f"{where}.symbol", row["symbol"])
    if not SYMBOL.match(symbol):
        raise ScenarioError(f"{where}.symbol {symbol!r} is not a ticker")
    try:
        screener = ScreenerKind(row["screener"])
    except ValueError as exc:
        raise ScenarioError(f"{where}.screener {row['screener']!r} is not a screener") from exc
    if not isinstance(row["legs"], list):
        raise ScenarioError(f"{where}.legs must be a list")
    legs = []
    for n, leg in enumerate(row["legs"]):
        at = f"{where}.legs[{n}]"
        if not isinstance(leg, list) or len(leg) != LEG_FIELDS:
            raise ScenarioError(f"{at} must be [from, to, price, volume]")
        legs.append((_clock(f"{at}.from", leg[0]), _clock(f"{at}.to", leg[1]),
                     _positive(f"{at}.price", leg[2]), _positive(f"{at}.volume", leg[3])))
    return SymbolPlan(symbol, _positive(f"{where}.base", row["base"]),
                      _positive(f"{where}.volume", row["volume"]), screener, tuple(legs))


def _day(where: str, raw: Any) -> tuple[SymbolPlan, ...]:
    if not isinstance(raw, list) or not raw:
        raise ScenarioError(f"{where} must be a non-empty list of symbols")
    plans = tuple(_symbol(f"{where}[{n}]", row) for n, row in enumerate(raw))
    names = [p.symbol for p in plans]
    if len(set(names)) != len(names):
        raise ScenarioError(f"{where} names a symbol twice")
    return plans


def _command(where: str, raw: Any) -> DemoCommand:
    row = _keys(where, raw, {"at", "kind", "symbol"})
    try:
        kind = CommandKind(row["kind"])
    except ValueError as exc:
        raise ScenarioError(f"{where}.kind {row['kind']!r} is not a command") from exc
    if not isinstance(row["symbol"], str):
        raise ScenarioError(f"{where}.symbol must be a string (empty = none)")
    symbol = row["symbol"] or None
    if symbol is not None and kind is not CommandKind.FLATTEN:
        raise ScenarioError(f"{where}: only flatten takes a symbol")
    return DemoCommand(_clock(f"{where}.at", row["at"]), kind, symbol)


def load(path: Path) -> Scenario:
    try:
        raw = tomllib.loads(path.read_text("utf-8"))
    except (OSError, tomllib.TOMLDecodeError, UnicodeDecodeError) as exc:
        raise ScenarioError(f"{path}: {exc}") from exc
    top = _keys("demo.toml", raw, {"demo", "market", "scenario"})
    plan = _keys("[scenario]", top["scenario"], {"today", "commands", "past"})
    demo = _keys("[demo]", top["demo"], {
        "root_dir", "environment", "api_port", "observers", "provider",
        "bot_start", "past_stop", "clock"})
    market = _keys("[market]", top["market"], {
        "half_spread_usd", "quote_size_shares", "wick_usd", "history_volume"})
    observers = demo["observers"]
    if not isinstance(observers, list) or not all(isinstance(o, str) for o in observers):
        raise ScenarioError("[demo] observers must be a list of strings")
    if not isinstance(plan["commands"], list) or not isinstance(plan["past"], list):
        raise ScenarioError("commands and past must be lists")
    environment = _text("[demo] environment", demo["environment"])
    if not SYMBOL.match(environment.upper()):
        raise ScenarioError("[demo] environment must be a plain name")
    return Scenario(
        root_dir=_text("[demo] root_dir", demo["root_dir"]),
        environment=environment,
        api_port=_count("[demo] api_port", demo["api_port"]),
        observers=tuple(observers),
        provider=_text("[demo] provider", demo["provider"]),
        bot_start=_clock("[demo] bot_start", demo["bot_start"]),
        past_stop=_clock("[demo] past_stop", demo["past_stop"]),
        clock=_clock("[demo] clock", demo["clock"]),
        half_spread=_positive("[market] half_spread_usd", market["half_spread_usd"]),
        quote_size=_positive("[market] quote_size_shares", market["quote_size_shares"]),
        wick=_positive("[market] wick_usd", market["wick_usd"]),
        history_volume=_positive("[market] history_volume", market["history_volume"]),
        today=_day("today", plan["today"]),
        commands=tuple(_command(f"commands[{n}]", c) for n, c in enumerate(plan["commands"])),
        past=tuple(_day(f"past[{n}]", day) for n, day in enumerate(plan["past"])),
    )
