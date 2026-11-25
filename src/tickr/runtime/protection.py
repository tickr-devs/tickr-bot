"""Protect first (spec 9): strategy work waits while an order of ours needs the loop.

`busy` names why the loop must not spend a pass on discovery, bars or the strategy: an entry
still working (it may fill at any moment), filled quantity not yet covered by a verified
stop, or an exit in progress. The engine step runs every pass regardless.

`LatencyLog` writes one `protection-latency` journal row per trade that had a fill, derived
only from journal rows the engine already wrote, so it survives restarts and is never
duplicated: fill observed (the first entry Detail showing filled quantity > 0), first stop
send attempt, and the first Detail showing a stop working. Webull Detail carries no fill time
(`types.OrderDetail`; spec F1 unproven), so "fill -> fill observed" is not measured and the
row says so. Only the first fill of a trade is measured; a later partial fill is not.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any

from tickr.engine.core import Engine, Leg, TradeState
from tickr.store import JournalRow
from tickr.types import OrderState

PROTECTION_LATENCY = "protection-latency"
ZERO = Decimal(0)
_WORKING = (str(OrderState.ACCEPTED), str(OrderState.PARTIALLY_FILLED))
FILL_TIME_UNKNOWN = "Detail has no fill time"


def busy(engine: Engine) -> str | None:
    """Why strategy work must wait this pass (first trade that needs the loop), or None."""
    for trade in engine.trades():
        orders = engine.orders(trade.trade_id)
        if any(o.leg is Leg.ENTRY and o.live for o in orders):
            return f"{trade.trade_id}: entry working"
        if (trade.exit_requested or trade.state is TradeState.EXITING
                or any(o.leg is Leg.EXIT and o.live for o in orders)):
            return f"{trade.trade_id}: exit in progress"
        expected = engine.expected_quantity(orders)
        verified = sum((o.open_quantity for o in orders if engine.protects(o)), ZERO)
        if expected > 0 and verified < expected:
            return f"{trade.trade_id}: filled {expected}, verified stop covers {verified}"
    return None


def _first(rows: list[JournalRow], after: int = 0) -> JournalRow | None:
    return min((r for r in rows if r.seq > after), key=lambda r: r.seq, default=None)


def _seconds(start: JournalRow, end: JournalRow | None) -> float | None:
    if end is None:
        return None
    return (datetime.fromisoformat(end.at_utc)
            - datetime.fromisoformat(start.at_utc)).total_seconds()


class LatencyLog:
    """Writes the per-trade latency row once: as soon as a stop is verified, or with what is
    known when the trade closed first. Trades already written (any run) are skipped."""

    def __init__(self, engine: Engine) -> None:
        self.engine = engine
        self._done = {r.key for r in engine.store.journal(PROTECTION_LATENCY)}

    def _rows(self, kind: str, cids: list[str]) -> list[JournalRow]:
        return [r for cid in cids for r in self.engine.store.journal(kind, cid)]

    def record(self) -> None:
        engine = self.engine
        for trade in engine.trades(open_only=False):
            if trade.trade_id in self._done:
                continue
            body = self._body(trade.trade_id, trade.state is TradeState.CLOSED)
            if body is None:
                continue
            self._done.add(trade.trade_id)
            if body:
                body["symbol"] = trade.symbol
                with engine.store.transaction():
                    engine.store.append(PROTECTION_LATENCY, body, key=trade.trade_id,
                                        run_id=engine.run_id)

    def _body(self, trade_id: str, closed: bool) -> dict[str, Any] | None:
        """The row, {} for nothing to write ever (closed without a fill), None for not yet."""
        orders = self.engine.orders(trade_id)
        entries = [o.client_order_id for o in orders if o.leg is Leg.ENTRY]
        stops = [o.client_order_id for o in orders if o.leg is Leg.STOP]
        fill = _first([r for r in self._rows("order-observed", entries)
                       if Decimal(r.body["filled_quantity"]) > 0])
        if fill is None:
            return {} if closed else None
        sent = _first(self._rows("order-attempted", stops), fill.seq)
        verified = _first([r for r in self._rows("order-observed", stops)
                           if r.body["state"] in _WORKING], fill.seq)
        if verified is None and not closed:
            return None
        return {
            "fill_observed_at": fill.at_utc,
            "fill_quantity": fill.body["filled_quantity"],
            "fill_to_observed_seconds": None, "fill_time": FILL_TIME_UNKNOWN,
            "stop_sent_at": None if sent is None else sent.at_utc,
            "stop_verified_at": None if verified is None else verified.at_utc,
            "observed_to_sent_seconds": _seconds(fill, sent),
            "observed_to_verified_seconds": _seconds(fill, verified),
            "closed_before_verified": verified is None,
        }
