"""End of the trading day (spec 1, 9 and 11): did the day end flat, by the broker's word?

After the session's flatten time plus `[session] flatten_grace_seconds`, the latest reconcile
reads (positions and open orders, taken after the flatten time) must show nothing of ours left.
Our records alone never prove a flat day.

- `own_left` lists what of ours the broker still holds or works: positions in the symbol of
  an open trade, and open orders with our client order ids. Foreign exposure is not listed
  (it has its own fences and alert).
- `day_end_record` is the once-per-day journal row. `agree` is true only when the reads are
  known AND the broker's positions equal what our open trades expect (Detail fills, symbol by
  symbol) AND the broker's open orders are exactly our live orders; so any foreign position or
  order, or an order of ours whose state we do not know, means "not agree". `day_clean` is
  null when the reads are unknown, never a guess.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from aperture.calendar import NEW_YORK, CalendarRefused, SessionCalendar
from aperture.engine.core import BrokerView, Engine
from aperture.runtime.alerts import AlertKind

if TYPE_CHECKING:
    from aperture.runtime.watch import Watch

DAY_END = "day-end"
ZERO = Decimal(0)


def own_left(engine: Engine, view: BrokerView) -> tuple[list[str], list[str]]:
    """Our positions and our open orders the broker still shows, in plain words."""
    open_symbols = {t.symbol for t in engine.trades()}
    ours = {o.client_order_id for o in engine.orders()}
    positions = [f"position {p.symbol} {p.quantity}" for p in view.positions
                 if p.quantity != 0 and p.symbol in open_symbols]
    orders = [f"order {o.client_order_id} {o.symbol}" for o in view.open_orders
              if o.client_order_id in ours]
    return positions, orders


def _agree(engine: Engine, view: BrokerView) -> bool:
    expected: dict[str, Decimal] = {}
    for trade in engine.trades():
        quantity = engine.expected_quantity(engine.orders(trade.trade_id))
        if quantity != 0:
            expected[trade.symbol] = expected.get(trade.symbol, ZERO) + quantity
    held = {p.symbol: p.quantity for p in view.positions if p.quantity != 0}
    live = {o.client_order_id for o in engine.orders() if o.live}
    return held == expected and {o.client_order_id for o in view.open_orders} == live


def day_end_record(engine: Engine, view: BrokerView | None, now: datetime) -> dict[str, Any]:
    day = now.astimezone(NEW_YORK).date()
    opened = [r for r in engine.store.query("SELECT opened_at, closed_at FROM trades")
              if datetime.fromisoformat(r["opened_at"]).astimezone(NEW_YORK).date() == day]
    open_trades = len(engine.trades())
    record: dict[str, Any] = {
        "day": day.isoformat(), "broker_read": view is not None,
        "broker_flat": None, "own_positions": None, "own_open_orders": None,
        "trades_today": len(opened),
        "trades_closed": sum(1 for r in opened if r["closed_at"] is not None),
        "open_trades": open_trades, "agree": False, "day_clean": None}
    if view is None:
        return record
    positions, orders = own_left(engine, view)
    broker_flat = not any(p.quantity != 0 for p in view.positions) and not view.open_orders
    agree = _agree(engine, view)
    record.update(
        broker_flat=broker_flat,
        own_positions=len(positions), own_open_orders=len(orders),
        agree=agree, day_clean=broker_flat and agree and open_trades == 0)
    return record


class DayEnd:
    """Called once per loop pass with the day's flatten time, once that time has passed and the
    reads from before it were dropped (`Service.flatten_due`)."""

    def __init__(self, engine: Engine, calendar: SessionCalendar | None,
                 watch: Watch | None) -> None:
        self.engine = engine
        self.calendar = calendar
        self.watch = watch
        self._recorded: date | None = None
        self._alerted: tuple[str, datetime] | None = None  # last not-flat text and when

    def check(self, flatten_at: datetime, now: datetime) -> None:
        config = self.engine.config
        if now < flatten_at + timedelta(seconds=float(config.session.flatten_grace_seconds)):
            return
        view = self.engine.broker_view
        self._record(view, now)
        if view is None:
            return  # unknown, never "flat": failing reads have their own alert
        positions, orders = own_left(self.engine, view)
        if not positions and not orders:
            self._alerted = None
            return
        text = ("not flat after the flatten time: " + "; ".join(positions + orders)
                + ". Check the Webull app: the stop is DAY and expires at the close")
        window = timedelta(seconds=float(config.alerts.dedupe_window_seconds))
        if (self._alerted is not None and self._alerted[0] == text
                and now - self._alerted[1] < window):
            return  # same condition: shown again once the dedupe window has passed
        self._alerted = (text, now)
        if self.watch is not None:
            self.watch.alert(AlertKind.NOT_FLAT_AFTER_FLATTEN, text)

    def _record(self, view: BrokerView | None, now: datetime) -> None:
        """One day-end row per trading day (survives restarts: the journal is the memory)."""
        day = now.astimezone(NEW_YORK).date()
        if self._recorded == day:
            return
        self._recorded = day
        if self.calendar is None:
            return  # no known trading day, no day record
        try:
            self.calendar.day_at(now)
        except CalendarRefused:
            return
        store = self.engine.store
        if store.journal(DAY_END, day.isoformat()):
            return
        with store.transaction():
            store.append(DAY_END, day_end_record(self.engine, view, now), key=day.isoformat(),
                         run_id=self.engine.run_id)
