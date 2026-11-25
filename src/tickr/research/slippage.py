"""Stop slippage measured from the bot's own records (backlog R5).

For every closed trade with a filled stop order, read from the bot's database (read only):

- stop slippage per share = planned stop price - stop fill price (the exact fill price when
  recorded, else the broker's order row); positive = filled worse than planned;
- planned loss = the trade's recorded `planned_loss_usd`; realized loss = (entry fill - sell
  fill) x shares sold, over every sell of the trade (stops and closes), gross of fees.

`stop_slippage_percent` = sum((stop - fill) x shares) / sum(stop x shares) x 100 over the
measured stop fills: the number the replay's `slippage_percent` stands for on a stop exit. A
trade whose fills cannot be read has a cause and is left out of every sum, never guessed.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

from tickr.api.history import orders_of
from tickr.engine.core import Leg, OrderRow
from tickr.research.errors import ResearchError
from tickr.store import Store

ZERO = Decimal(0)
HUNDRED = Decimal(100)


@dataclass(frozen=True)
class StoppedTrade:
    day: date
    trade_id: str
    symbol: str
    close_cause: str | None
    planned_loss_usd: Decimal
    stop_quantity: Decimal | None
    """Shares the stop orders sold."""
    stop_price: Decimal | None
    """Planned stop price, share-weighted over the filled stop orders."""
    stop_fill_price: Decimal | None
    """Stop fill price, share-weighted over the filled stop orders."""
    entry_price: Decimal | None
    sold_quantity: Decimal | None
    realized_loss_usd: Decimal | None
    """(entry - sells' average) x shares sold; negative = a gain."""
    cause: str | None
    """Why the trade is not measured (None: measured)."""

    @property
    def slippage_usd(self) -> Decimal | None:
        """Per share, positive = the stop filled below its planned price."""
        if self.stop_price is None or self.stop_fill_price is None:
            return None
        return self.stop_price - self.stop_fill_price

    @property
    def slippage_percent(self) -> Decimal | None:
        usd = self.slippage_usd
        return None if usd is None or self.stop_price is None else (
            usd / self.stop_price * HUNDRED)


@dataclass(frozen=True)
class SlippageGroup:
    """Sums over the measured trades of one day, or of all days (`day` None)."""

    day: date | None
    stopped_trades: int
    measured_trades: int
    planned_loss_usd: Decimal
    realized_loss_usd: Decimal
    stop_slippage_usd: Decimal
    """Sum of (stop - fill) x shares over the measured stop fills."""
    stop_slippage_percent: Decimal | None
    """None when nothing was measured."""


def _weighted(orders: Sequence[OrderRow],
              price: Callable[[OrderRow], Decimal | None]) -> Decimal | None:
    filled = [o for o in orders if o.filled_quantity > 0]
    quantity = sum((o.filled_quantity for o in filled), ZERO)
    if quantity == 0 or any(price(o) is None for o in filled):
        return None
    return sum((price(o) * o.filled_quantity for o in filled), ZERO) / quantity


def _measure(day: date, row, orders: list[OrderRow]) -> StoppedTrade:
    stops = [o for o in orders if o.leg is Leg.STOP and o.filled_quantity > 0]
    entries = [o for o in orders if o.leg is Leg.ENTRY]
    sells = [o for o in orders if o.leg is not Leg.ENTRY]
    stop_quantity = sum((o.filled_quantity for o in stops), ZERO)
    stop_price = _weighted(stops, lambda o: o.stop_price)
    stop_fill = _weighted(stops, lambda o: o.fill_price)
    entry_price = _weighted(entries, lambda o: o.fill_price)
    exit_price = _weighted(sells, lambda o: o.fill_price)
    sold = sum((o.filled_quantity for o in sells), ZERO)
    causes = [text for missing, text in (
        (stop_price is None, "a filled stop order has no stop price"),
        (stop_fill is None, "a filled stop order has no fill price"),
        (entry_price is None, "the entry has no fill price"),
        (exit_price is None and stop_fill is not None, "a filled close has no fill price"),
    ) if missing]
    realized = (None if entry_price is None or exit_price is None
                else (entry_price - exit_price) * sold)
    return StoppedTrade(day=day, trade_id=row["trade_id"], symbol=row["symbol"],
                        close_cause=row["close_cause"],
                        planned_loss_usd=Decimal(row["planned_loss_usd"]),
                        stop_quantity=stop_quantity, stop_price=stop_price,
                        stop_fill_price=stop_fill, entry_price=entry_price, sold_quantity=sold,
                        realized_loss_usd=realized, cause="; ".join(causes) or None)


def stopped_trades(database: Path, zone: ZoneInfo,
                   environment: str | None = None) -> list[StoppedTrade]:
    """Closed trades with a filled stop order, oldest close first. `environment` keeps only
    trades opened by runs of that environment (None: every run)."""
    if not database.is_file():
        raise ResearchError(f"database not found: {database}")
    store = Store.open_readonly(database)
    try:
        query = ("SELECT t.* FROM trades t JOIN runs r ON r.run_id = t.run_id "
                 "WHERE t.closed_at IS NOT NULL")
        params: tuple[str, ...] = ()
        if environment is not None:
            query += " AND r.environment = ?"
            params = (environment,)
        rows = store.query(query + " ORDER BY t.closed_at, t.trade_id", params)
        by_trade = orders_of(store, [row["trade_id"] for row in rows])
    finally:
        store.close()
    result = []
    for row in rows:
        orders = by_trade.get(row["trade_id"], [])
        if not any(o.leg is Leg.STOP and o.filled_quantity > 0 for o in orders):
            continue
        day = datetime.fromisoformat(row["closed_at"]).astimezone(zone).date()
        result.append(_measure(day, row, orders))
    return result


def group(trades: Sequence[StoppedTrade], day: date | None) -> SlippageGroup:
    measured = 0
    planned = realized = slip = notional = ZERO
    for t in trades:
        if (t.cause is not None or t.slippage_usd is None or t.stop_price is None
                or t.stop_quantity is None or t.realized_loss_usd is None):
            continue
        measured += 1
        planned += t.planned_loss_usd
        realized += t.realized_loss_usd
        slip += t.slippage_usd * t.stop_quantity
        notional += t.stop_price * t.stop_quantity
    return SlippageGroup(
        day=day, stopped_trades=len(trades), measured_trades=measured,
        planned_loss_usd=planned, realized_loss_usd=realized, stop_slippage_usd=slip,
        stop_slippage_percent=None if notional == 0 else slip / notional * HUNDRED)


def by_day(trades: Sequence[StoppedTrade]) -> list[SlippageGroup]:
    days = sorted({t.day for t in trades})
    return [group([t for t in trades if t.day == d], d) for d in days]
