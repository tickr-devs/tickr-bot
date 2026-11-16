"""The demo scenario, loaded strictly from `config/demo.toml` (every number lives there).

Unknown or missing keys, wrong types and bad values are refused with a `ScenarioError` naming
the single cause; nothing is defaulted.
"""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass, replace
from datetime import date, datetime, time, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from tickr.api.contract import CommandKind
from tickr.data.base import ScreenerKind
from tickr.demo.world import DailyWalk, Leg, Texture

SYMBOL = re.compile(r"^[A-Z][A-Z0-9.]*$")
LEG_FIELDS = 4  # from, to, price at the end, volume per minute
CENT = Decimal("0.01")  # prices are quoted in cents
# Demo fault kinds ([scenario] faults): the simulated exchange's stops of a symbol stop working.
STOP_UNVERIFIED = "stop-unverified"
FAULT_KINDS = (STOP_UNVERIFIED,)


SECONDS_PER_MINUTE = 60
LOOP_LEG_FIELDS = 4  # from minute, to minute (of the cycle), change of the price, volume per minute
ANY_DAY = date(2000, 1, 3)  # carrier date for adding minutes to a New York wall time


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
class DemoFault:
    at: str
    kind: str
    symbol: str


@dataclass(frozen=True)
class LoopRole:
    """One part of the live loop: in cycle k it moves `symbols[k % len(symbols)]` along `legs`,
    each (from minute, to minute of the cycle, change of the price as a ratio, volume)."""

    role: str
    symbols: tuple[str, ...]
    legs: tuple[tuple[int, int, Decimal, Decimal], ...]


@dataclass(frozen=True)
class LoopCommand:
    """An operator command in every cycle, `at_seconds` after the cycle starts; a flatten names
    the role whose symbol of that cycle it closes."""

    at_seconds: int
    kind: CommandKind
    role: str | None


@dataclass(frozen=True)
class LiveLoop:
    cycle_minutes: int
    listed_minutes_before_clock: int
    roles: tuple[LoopRole, ...]
    commands: tuple[LoopCommand, ...]


@dataclass(frozen=True)
class Recording:
    """What `tickr demo --record` puts in the apps' recording (`[recording]`): the loop cycle it
    holds, the GET /performance window the apps ask for, and the simulated seconds the bot gets
    to act on each command the apps can send, after that cycle."""

    cycle: int
    performance_days: int
    command_seconds: int


@dataclass(frozen=True)
class Scenario:
    root_dir: str
    environment: str
    observers: tuple[str, ...]
    provider: str
    bot_start: str
    past_stop: str
    clock: str
    live_until: str
    half_spread: Decimal
    quote_size: Decimal
    wick: Decimal
    history_volume: Decimal
    texture: Texture
    chart_days: int
    chart_walk: DailyWalk
    today: tuple[SymbolPlan, ...]
    commands: tuple[DemoCommand, ...]
    past: tuple[tuple[SymbolPlan, ...], ...]
    faults: tuple[DemoFault, ...] = ()
    answer_within_seconds: int = 0  # [demo]: start to the API's first answer (tests)
    live: LiveLoop | None = None  # already folded into `today` and `commands` by `load`
    # (symbol, New York time "HH:MM:SS") of symbols only the live loop moves: the simulated
    # screeners list them from then on, so the fast part of the day does not evaluate them.
    listed: tuple[tuple[str, str], ...] = ()
    recording: Recording | None = None


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


def _fraction(where: str, value: Any, below_one: bool) -> Decimal:
    try:
        number = Decimal(_text(where, value))
    except InvalidOperation as exc:
        raise ScenarioError(f"{where} is not a decimal string: {value!r}") from exc
    if not number.is_finite() or number < 0 or (below_one and number >= 1):
        bound = ", below 1" if below_one else ""
        raise ScenarioError(f"{where} must be 0 or more{bound}: {value!r}")
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


def _fault(where: str, raw: Any, today: tuple[SymbolPlan, ...]) -> DemoFault:
    row = _keys(where, raw, {"at", "kind", "symbol"})
    if row["kind"] not in FAULT_KINDS:
        raise ScenarioError(f"{where}.kind {row['kind']!r} is not one of {list(FAULT_KINDS)}")
    symbol = _text(f"{where}.symbol", row["symbol"])
    if symbol not in {p.symbol for p in today}:
        raise ScenarioError(f"{where}.symbol {symbol!r} is not a symbol of today")
    return DemoFault(_clock(f"{where}.at", row["at"]), row["kind"], symbol)


def _ratio(where: str, value: Any) -> Decimal:
    try:
        number = Decimal(_text(where, value))
    except InvalidOperation as exc:
        raise ScenarioError(f"{where} is not a decimal string: {value!r}") from exc
    if not number.is_finite() or number <= -1:
        raise ScenarioError(f"{where} must be above -1: {value!r}")
    return number


def _minute(where: str, value: Any, cycle: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= cycle:
        raise ScenarioError(f"{where} must be a whole minute from 0 to {cycle}")
    return value


def _loop(raw: Any, today: tuple[SymbolPlan, ...]) -> LiveLoop:
    table = _keys("[live]", raw, {"cycle_minutes", "listed_minutes_before_clock", "roles",
                                  "commands"})
    cycle = _count("[live] cycle_minutes", table["cycle_minutes"])
    listed = _count("[live] listed_minutes_before_clock", table["listed_minutes_before_clock"])
    if not isinstance(table["roles"], list) or not table["roles"]:
        raise ScenarioError("[live] roles must be a non-empty list")
    if not isinstance(table["commands"], list):
        raise ScenarioError("[live] commands must be a list")
    known = {p.symbol for p in today}
    roles = []
    for n, row in enumerate(table["roles"]):
        where = f"[live] roles[{n}]"
        role = _keys(where, row, {"role", "symbols", "legs"})
        name = _text(f"{where}.role", role["role"])
        symbols = role["symbols"]
        if not isinstance(symbols, list) or not symbols:
            raise ScenarioError(f"{where}.symbols must be a non-empty list")
        for symbol in symbols:
            if symbol not in known:
                raise ScenarioError(f"{where}.symbols: {symbol!r} is not a symbol of today")
        if not isinstance(role["legs"], list) or not role["legs"]:
            raise ScenarioError(f"{where}.legs must be a non-empty list")
        legs = []
        end = 0
        for k, leg in enumerate(role["legs"]):
            at = f"{where}.legs[{k}]"
            if not isinstance(leg, list) or len(leg) != LOOP_LEG_FIELDS:
                raise ScenarioError(f"{at} must be [from minute, to minute, change, volume]")
            first = _minute(f"{at}.from", leg[0], cycle)
            last = _minute(f"{at}.to", leg[1], cycle)
            if not end <= first < last:
                raise ScenarioError(f"{at} overlaps the leg before it or ends before it starts")
            legs.append((first, last, _ratio(f"{at}.change", leg[2]),
                         _positive(f"{at}.volume", leg[3])))
            end = last
        roles.append(LoopRole(name, tuple(symbols), tuple(legs)))
    names = [r.role for r in roles]
    if len(set(names)) != len(names):
        raise ScenarioError("[live] roles names a role twice")
    commands = []
    for n, row in enumerate(table["commands"]):
        where = f"[live] commands[{n}]"
        command = _keys(where, row, {"at_seconds", "kind", "role"})
        at = command["at_seconds"]
        inside = isinstance(at, int) and not isinstance(at, bool)
        if not inside or not 0 <= at < cycle * SECONDS_PER_MINUTE:
            raise ScenarioError(f"{where}.at_seconds must be a whole second inside the cycle")
        try:
            kind = CommandKind(command["kind"])
        except ValueError as exc:
            raise ScenarioError(f"{where}.kind {command['kind']!r} is not a command") from exc
        if not isinstance(command["role"], str):
            raise ScenarioError(f"{where}.role must be a string (empty = none)")
        role_name = command["role"] or None
        if role_name is not None and kind is not CommandKind.FLATTEN:
            raise ScenarioError(f"{where}: only flatten takes a role")
        if role_name is not None and role_name not in names:
            raise ScenarioError(f"{where}.role {role_name!r} is not a role of [live] roles")
        commands.append(LoopCommand(at, kind, role_name))
    return LiveLoop(cycle, listed, tuple(roles), tuple(commands))


def _wall(clock: str) -> datetime:
    return datetime.combine(ANY_DAY, time.fromisoformat(clock))


def _plus(clock: str, minutes: int = 0, seconds: int = 0) -> str:
    return (_wall(clock) + timedelta(minutes=minutes, seconds=seconds)).time().isoformat()


def loop_cycles(clock: str, live_until: str, cycle_minutes: int) -> int:
    """Whole cycles between going live and `live_until`."""
    span = _wall(live_until) - _wall(clock)
    return int(span // timedelta(minutes=cycle_minutes))


def unroll(scenario: Scenario) -> Scenario:
    """The scenario with its live loop written out: from `clock` on, every whole cycle up to
    `live_until` moves each role's symbol of that cycle along the role's legs (prices as changes
    of where the symbol is by then) and queues the cycle's commands. Symbols only the loop moves
    (no legs of their own) are listed on their screener `listed_minutes_before_clock` before
    `clock`, not all day. Pure and deterministic."""
    loop = scenario.live
    if loop is None:
        return scenario
    plans = {p.symbol: p for p in scenario.today}
    legs = {p.symbol: list(p.legs) for p in scenario.today}
    commands = list(scenario.commands)
    listed_at = _plus(scenario.clock, -loop.listed_minutes_before_clock)
    listed = tuple((symbol, listed_at) for symbol in dict.fromkeys(
        s for role in loop.roles for s in role.symbols) if not plans[symbol].legs)
    for cycle in range(loop_cycles(scenario.clock, scenario.live_until, loop.cycle_minutes)):
        start = _plus(scenario.clock, cycle * loop.cycle_minutes)
        chosen: dict[str, str] = {}
        for role in loop.roles:
            symbol = role.symbols[cycle % len(role.symbols)]
            chosen[role.role] = symbol
            plan = legs[symbol]
            price = plan[-1][2] if plan else plans[symbol].base
            for first, last, change, volume in role.legs:
                begin = _plus(start, first)
                if plan and _wall(begin) < _wall(plan[-1][1]):
                    raise ScenarioError(f"[live] cycle {cycle}: {symbol} of role {role.role!r} "
                                        f"moves again at {begin} before its leg to "
                                        f"{plan[-1][1]} ends")
                price = (price * (1 + change)).quantize(CENT)
                if price <= 0:
                    raise ScenarioError(f"[live] cycle {cycle}: {symbol} falls to {price}")
                plan.append((begin, _plus(start, last), price, volume))
        for command in loop.commands:
            commands.append(DemoCommand(_plus(start, seconds=command.at_seconds), command.kind,
                                        None if command.role is None else chosen[command.role]))
    commands.sort(key=lambda c: _wall(c.at))
    return replace(scenario, today=tuple(replace(p, legs=tuple(legs[p.symbol]))
                                         for p in scenario.today),
                   commands=tuple(commands), listed=listed)


def load(path: Path) -> Scenario:
    try:
        raw = tomllib.loads(path.read_text("utf-8"))
    except (OSError, tomllib.TOMLDecodeError, UnicodeDecodeError) as exc:
        raise ScenarioError(f"{path}: {exc}") from exc
    top = _keys("demo.toml", raw, {"demo", "market", "scenario", "live", "recording"})
    plan = _keys("[scenario]", top["scenario"], {"today", "commands", "past", "faults"})
    demo = _keys("[demo]", top["demo"], {
        "root_dir", "environment", "observers", "provider",
        "bot_start", "past_stop", "clock", "live_until", "answer_within_seconds"})
    market = _keys("[market]", top["market"], {
        "half_spread_usd", "quote_size_shares", "wick_usd", "history_volume", "texture_drift",
        "texture_pull", "texture_wick", "texture_move", "texture_move_cap", "texture_volume",
        "chart_days", "chart_daily_move", "chart_daily_gap", "chart_daily_wick"})
    observers = demo["observers"]
    if not isinstance(observers, list) or not all(isinstance(o, str) for o in observers):
        raise ScenarioError("[demo] observers must be a list of strings")
    if not all(isinstance(plan[k], list) for k in ("commands", "past", "faults")):
        raise ScenarioError("commands, past and faults must be lists")
    environment = _text("[demo] environment", demo["environment"])
    if not SYMBOL.match(environment.upper()):
        raise ScenarioError("[demo] environment must be a plain name")
    today = _day("today", plan["today"])
    clock = _clock("[demo] clock", demo["clock"])
    live_until = _clock("[demo] live_until", demo["live_until"])
    if time.fromisoformat(live_until) <= time.fromisoformat(clock):
        raise ScenarioError("[demo] live_until must be after [demo] clock")
    return unroll(Scenario(
        root_dir=_text("[demo] root_dir", demo["root_dir"]),
        environment=environment,
        observers=tuple(observers),
        provider=_text("[demo] provider", demo["provider"]),
        bot_start=_clock("[demo] bot_start", demo["bot_start"]),
        past_stop=_clock("[demo] past_stop", demo["past_stop"]),
        clock=clock,
        live_until=live_until,
        half_spread=_positive("[market] half_spread_usd", market["half_spread_usd"]),
        quote_size=_positive("[market] quote_size_shares", market["quote_size_shares"]),
        wick=_positive("[market] wick_usd", market["wick_usd"]),
        history_volume=_positive("[market] history_volume", market["history_volume"]),
        texture=Texture(
            drift=_fraction("[market] texture_drift", market["texture_drift"], False),
            pull=_fraction("[market] texture_pull", market["texture_pull"], True),
            wick=_fraction("[market] texture_wick", market["texture_wick"], False),
            move=_fraction("[market] texture_move", market["texture_move"], False),
            move_cap=_fraction("[market] texture_move_cap", market["texture_move_cap"], False),
            volume=_fraction("[market] texture_volume", market["texture_volume"], True)),
        chart_days=_count("[market] chart_days", market["chart_days"]),
        chart_walk=DailyWalk(
            move=_fraction("[market] chart_daily_move", market["chart_daily_move"], True),
            gap=_fraction("[market] chart_daily_gap", market["chart_daily_gap"], True),
            wick=_fraction("[market] chart_daily_wick", market["chart_daily_wick"], True)),
        today=today,
        commands=tuple(_command(f"commands[{n}]", c) for n, c in enumerate(plan["commands"])),
        past=tuple(_day(f"past[{n}]", day) for n, day in enumerate(plan["past"])),
        faults=tuple(_fault(f"faults[{n}]", f, today) for n, f in enumerate(plan["faults"])),
        live=_loop(top["live"], today),
        answer_within_seconds=_count("[demo] answer_within_seconds",
                                     demo["answer_within_seconds"]),
        recording=_recording(top["recording"], clock, live_until, top["live"]),
    ))


def _recording(raw: Any, clock: str, live_until: str, live: Any) -> Recording:
    table = _keys("[recording]", raw, {"cycle", "performance_days", "command_seconds"})
    cycle = table["cycle"]
    cycles = loop_cycles(clock, live_until, _count("[live] cycle_minutes", live["cycle_minutes"]))
    # The cycle after it must exist too: the commands are recorded into its start.
    if isinstance(cycle, bool) or not isinstance(cycle, int) or not 0 <= cycle < cycles - 1:
        raise ScenarioError(f"[recording] cycle must be a whole loop cycle from 0 to {cycles - 2}")
    return Recording(cycle, _count("[recording] performance_days", table["performance_days"]),
                     _count("[recording] command_seconds", table["command_seconds"]))
