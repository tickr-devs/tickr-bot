"""GET /journal: the latest rows of the bot's journal (database opened read-only), for the
Overview's journal card and its "line #N". Only the row number, time, kind and the symbol the
body names are served; the bot reads and writes nothing for it."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from tickr.api import contract as api
from tickr.calendar import NEW_YORK, ny_date_of
from tickr.store import Store

NO_DATABASE = "the bot has not created its database yet"


def journal_document(database: Path, rows: int) -> api.JournalV1:
    if not database.exists():
        return api.JournalV1(None, (), NO_DATABASE)
    try:
        store = Store.open_readonly(database)
        try:
            found = store.query("SELECT seq, at_utc, kind, body FROM journal ORDER BY seq DESC "
                                "LIMIT ?", (rows,))
        finally:
            store.close()
        shown = tuple(api.JournalRowV1(int(r["seq"]), datetime.fromisoformat(r["at_utc"]),
                                       str(r["kind"]), _symbol(r["body"]))
                      for r in reversed(found))
    except (sqlite3.Error, ValueError) as exc:
        return api.JournalV1(None, (), f"database unreadable ({type(exc).__name__})")
    return api.JournalV1(shown[-1].seq if shown else None, shown,
                         None if shown else "the journal has no rows yet")


def _symbol(body: str) -> str | None:
    parsed = json.loads(body)
    value = parsed.get("symbol") if isinstance(parsed, dict) else None
    return value if isinstance(value, str) else None


# The journal rows a person reads as the day's events (GET /events): the rest (reconciles,
# reads, timings) is the bot's own bookkeeping.
_RUN_STARTED = "run-started"
_RUN_ENDED = "run-ended"
_TRADE_OPENED = "trade-opened"
_ORDER_OBSERVED = "order-observed"
_ORDER_REFUSED = "order-refused"
_ORDER_ACKNOWLEDGED = "order-acknowledged"
_REPLACE_CONFIRMED = "replace-confirmed"
_EXIT_REQUESTED = "exit-requested"
_TRADE_CLOSED = "trade-closed"
_FENCE_SET = "fence-set"
_FENCE_CLEARED = "fence-cleared"
_COMMAND = "command"
_READ_KINDS = (_RUN_STARTED, _RUN_ENDED, _TRADE_OPENED, _ORDER_OBSERVED, _ORDER_REFUSED,
               _ORDER_ACKNOWLEDGED,
               _REPLACE_CONFIRMED, _EXIT_REQUESTED, _TRADE_CLOSED, _FENCE_SET, _FENCE_CLEARED,
               _COMMAND)
_FILLED = "FILLED"  # OrderState.FILLED as the journal body writes it
_REJECTED = "REJECTED"  # OrderState.REJECTED
_FENCE_KEY = ":"  # "<fence>:<key>" (engine/core.py fence names)
_ROUTINE_FENCE = "unprotected"  # Fence.UNPROTECTED: set at every entry fill (DayEventKind)
_STOP_LEG = "stop"  # Leg.STOP
_SQL_BATCH = 500  # keys per IN (...) query, under SQLite's bound-parameter limit


def day_events(database: Path, day: date) -> api.DayEventsV1:
    """The readable events of New York date `day`, oldest first."""
    if not database.exists():
        return api.DayEventsV1(day, (), NO_DATABASE)
    start = datetime.combine(day, time(), NEW_YORK).astimezone(UTC)
    end = datetime.combine(day + timedelta(days=1), time(), NEW_YORK).astimezone(UTC)
    try:
        store = Store.open_readonly(database)
        try:
            marks = ", ".join("?" for _ in _READ_KINDS)
            rows = store.query(
                f"SELECT seq, at_utc, kind, key, body FROM journal WHERE kind IN ({marks}) "
                "AND at_utc >= ? AND at_utc < ? ORDER BY seq",
                (*_READ_KINDS, start.isoformat(), end.isoformat()))
            keys = sorted({_target(r["kind"], r["key"]) for r in rows} - {None})
            orders = _by_key(store, "SELECT client_order_id AS id, symbol, leg, quantity, "
                             "stop_price FROM orders "
                             "WHERE client_order_id IN", keys)
            trades = _by_key(store, "SELECT trade_id AS id, symbol FROM trades WHERE "
                             "trade_id IN", keys)
        finally:
            store.close()
        events = _events(rows, orders, trades, day)
    except (sqlite3.Error, ValueError, KeyError, TypeError, ArithmeticError) as exc:
        return api.DayEventsV1(day, (), f"database unreadable ({type(exc).__name__})")
    return api.DayEventsV1(day, events, None)


def _target(kind: str, key: str | None) -> str | None:
    """The order id or trade id a row is about: a fence's key after its name."""
    if key is not None and kind in (_FENCE_SET, _FENCE_CLEARED):
        return key.partition(_FENCE_KEY)[2] or None
    return key


def _by_key(store: Store, select: str, keys: list[str]) -> dict[str, sqlite3.Row]:
    found: dict[str, sqlite3.Row] = {}
    for first in range(0, len(keys), _SQL_BATCH):
        batch = keys[first:first + _SQL_BATCH]
        marks = ", ".join("?" for _ in batch)
        found.update((r["id"], r) for r in store.query(f"{select} ({marks})", tuple(batch)))
    return found


def _events(rows: list[sqlite3.Row], orders: dict[str, sqlite3.Row],
            trades: dict[str, sqlite3.Row], day: date) -> tuple[api.DayEventV1, ...]:
    events: list[api.DayEventV1] = []
    filled: set[str] = set()
    for row in rows:
        at = datetime.fromisoformat(row["at_utc"])
        if ny_date_of(at) != day:
            continue
        kind, key, body = row["kind"], row["key"], json.loads(row["body"])
        target = _target(kind, key)
        about = None if target is None else orders.get(target) or trades.get(target)
        order = None if key is None else orders.get(key)
        if kind == _ORDER_OBSERVED:
            if order is None or body["state"] not in (_FILLED, _REJECTED) or key in filled:
                continue
            if body["state"] == _FILLED:
                filled.add(key)
        if kind == _ORDER_REFUSED and order is None:
            continue
        if kind == _ORDER_ACKNOWLEDGED:
            if order is None or order["leg"] != _STOP_LEG:
                continue
            body = {"quantity": order["quantity"], "stop_price": order["stop_price"]}
        if (kind in (_FENCE_SET, _FENCE_CLEARED)
                and (key or "").partition(_FENCE_KEY)[0] == _ROUTINE_FENCE):
            continue
        what, fields = _read(kind, key, body)
        symbol = fields.pop("symbol", None) or (None if about is None else about["symbol"])
        leg = None if order is None else order["leg"]
        events.append(api.DayEventV1(
            int(row["seq"]), at, what, None if symbol is None else str(symbol),
            None if leg is None or what not in _LEG_EVENTS else str(leg),
            _decimal(fields.get("quantity")), _decimal(fields.get("price")),
            _decimal(fields.get("stop_price")), _text(fields.get("subject")),
            _text(fields.get("cause"))))
    return tuple(events)


_LEG_EVENTS = (api.DayEventKind.FILLED, api.DayEventKind.ORDER_REFUSED)


def _read(kind: str, key: str | None, body: dict[str, Any]
          ) -> tuple[api.DayEventKind, dict[str, Any]]:
    """One read journal row as an event kind and the fields it fills."""
    events = api.DayEventKind
    if kind == _RUN_STARTED:
        return events.BOT_STARTED, {"subject": f"{body['strategy']}@{body['strategy_version']}"}
    if kind == _RUN_ENDED:
        return events.BOT_STOPPED, {"cause": body["cause"]}
    if kind == _TRADE_OPENED:
        return events.ENTRY_SENT, {"symbol": body["symbol"], "quantity": body["quantity"],
                                   "price": body["limit_price"],
                                   "stop_price": body["stop_price"]}
    if kind == _ORDER_OBSERVED and body["state"] == _FILLED:
        return events.FILLED, {"quantity": body["filled_quantity"],
                               "price": body["average_fill_price"]}
    if kind == _ORDER_OBSERVED:
        return events.ORDER_REFUSED, {"cause": body.get("raw_status") or body["state"]}
    if kind == _ORDER_REFUSED:
        return events.ORDER_REFUSED, {"cause": body["code"]}
    if kind == _ORDER_ACKNOWLEDGED:
        return events.STOP_PLACED, body
    if kind == _REPLACE_CONFIRMED:
        return events.STOP_MOVED, {"stop_price": body["stop_price"]}
    if kind == _EXIT_REQUESTED:
        return events.EXIT_REQUESTED, {"cause": body["cause"]}
    if kind == _TRADE_CLOSED:
        return events.TRADE_CLOSED, {"cause": body["cause"]}
    if kind in (_FENCE_SET, _FENCE_CLEARED):
        fence = (key or "").partition(_FENCE_KEY)[0]
        if kind == _FENCE_SET:
            return events.FENCE_SET, {"subject": fence, "cause": body.get("cause")}
        return events.FENCE_CLEARED, {"subject": fence, "cause": body.get("by")}
    done = body["outcome"] == api.CommandOutcome.DONE
    return (events.COMMAND_DONE if done else events.COMMAND_REFUSED,
            {"subject": body["kind"], "cause": body.get("cause")})


def _text(value: Any) -> str | None:
    return None if value is None else str(value)


def _decimal(value: Any) -> Decimal | None:
    return None if value is None else Decimal(str(value))
