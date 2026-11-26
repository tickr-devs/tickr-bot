"""Fill quality of real fills, from the bot's own records of any one environment (backlog M4).

Read only, per closed trade of one database (the environment is just which database, and the
optional `--environment` filter on its runs; nothing here knows environment names):

- entry slippage per share, positive = worse than the reference: fill - entry limit, and fill -
  ask of the last captured quote at the entry decision (the `order-intent` journal row);
- exit slippage per share: a market exit's bid at its decision - its fill. Stop exits are R5's
  measure (`tickr.research.slippage`) and are taken from it as they are;
- fees: the broker's fees recorded on the trade's filled orders (a trade with any unrecorded fee
  is counted apart and never taken as free), per trade and per share filled;
- fill delay: the `order-attempted` journal row to the first `order-observed` row with fills.
  The bot learns of fills by polling, so this is an upper bound of the broker's own delay.

Quotes come from captured `quotes.jsonl` files (`tickr.data.capture`); the quote is the latest
one of the symbol received at or before the decision on the same New York day. Its age is
reported, never cut off. Without a quote the quote-based figures are empty with a cause.
"""

from __future__ import annotations

import json
import statistics
from bisect import bisect_right
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from tickr.api.history import orders_of
from tickr.engine.core import Leg, OrderRow
from tickr.research import slippage
from tickr.research.errors import ResearchError
from tickr.research.latency import QUOTE_KEYS, _read, _time
from tickr.store import IN_LIST_LIMIT, Store, marks

ZERO = Decimal(0)
HUNDRED = Decimal(100)


@dataclass(frozen=True)
class Quote:
    received: datetime
    bid: Decimal
    ask: Decimal


class Quotes:
    """Captured quotes of one source by symbol, in receipt order."""

    def __init__(self, files: Sequence[Path], source: str, zone: ZoneInfo) -> None:
        self._zone = zone
        by_symbol: dict[str, list[Quote]] = {}

        def quote(where: str, record: dict[str, Any]) -> None:
            if record["source"] != source:
                return
            try:
                bid, ask = Decimal(record["bid"]), Decimal(record["ask"])
            except (InvalidOperation, TypeError) as exc:
                raise ResearchError(f"{where}: bid or ask is not a decimal") from exc
            by_symbol.setdefault(str(record["symbol"]), []).append(
                Quote(_time(where, record["receipt_time"]), bid, ask))

        for path in files:
            _read(path, QUOTE_KEYS, quote)
        self._quotes = {s: sorted(q, key=lambda x: x.received) for s, q in by_symbol.items()}
        self._times = {s: [x.received for x in q] for s, q in self._quotes.items()}

    def at(self, symbol: str, when: datetime | None) -> Quote | None:
        """Latest quote of `symbol` received at or before `when`, on the same New York day."""
        if when is None:
            return None
        index = bisect_right(self._times.get(symbol, []), when)
        if index == 0:
            return None
        found = self._quotes[symbol][index - 1]
        day = when.astimezone(self._zone).date()
        return found if found.received.astimezone(self._zone).date() == day else None


@dataclass(frozen=True)
class TradeFill:
    day: date
    trade_id: str
    symbol: str
    entry_shares: Decimal | None
    entry_limit: Decimal | None
    entry_fill: Decimal | None
    entry_ask: Decimal | None
    entry_quote_age_s: Decimal | None
    entry_delay_s: Decimal | None
    exit_shares: Decimal | None
    """Shares the market exits sold."""
    exit_fill: Decimal | None
    exit_bid: Decimal | None
    exit_delay_s: Decimal | None
    shares_filled: Decimal
    """Shares of every filled order of the trade, all legs."""
    fees_usd: Decimal | None
    """None: a filled order has no recorded fee yet."""
    cause: str | None
    """Why a part is empty (None: all of it measured)."""

    @property
    def entry_vs_limit_usd(self) -> Decimal | None:
        if self.entry_fill is None or self.entry_limit is None:
            return None
        return self.entry_fill - self.entry_limit

    @property
    def entry_vs_quote_usd(self) -> Decimal | None:
        if self.entry_fill is None or self.entry_ask is None:
            return None
        return self.entry_fill - self.entry_ask

    @property
    def exit_vs_quote_usd(self) -> Decimal | None:
        if self.exit_fill is None or self.exit_bid is None:
            return None
        return self.exit_bid - self.exit_fill


def _weighted(orders: Sequence[OrderRow]) -> Decimal | None:
    quantity = sum((o.filled_quantity for o in orders), ZERO)
    if quantity == 0 or any(o.fill_price is None for o in orders):
        return None
    return sum((o.fill_price * o.filled_quantity for o in orders), ZERO) / quantity


def _journal_times(store: Store, orders: Sequence[OrderRow]):
    """Journal times by client order id: decision, send attempt, first observed fill."""
    ids = [o.client_order_id for o in orders]
    intent: dict[str, datetime] = {}
    attempted: dict[str, datetime] = {}
    filled: dict[str, datetime] = {}
    first = {"order-intent": intent, "order-attempted": attempted}
    for start in range(0, len(ids), IN_LIST_LIMIT):
        part = ids[start:start + IN_LIST_LIMIT]
        rows = store.query(
            "SELECT at_utc, kind, key, body FROM journal WHERE kind IN "
            "('order-intent', 'order-attempted', 'order-observed') "
            f"AND key IN ({marks(part)}) ORDER BY seq", tuple(part))
        for row in rows:
            when = datetime.fromisoformat(row["at_utc"])
            if row["kind"] in first:
                first[row["kind"]].setdefault(row["key"], when)
            elif row["key"] not in filled:
                quantity = json.loads(row["body"]).get("filled_quantity")
                if quantity is not None and Decimal(str(quantity)) > 0:
                    filled[row["key"]] = when
    return intent, attempted, filled


def _seconds(start: datetime | None, end: datetime | None) -> Decimal | None:
    if start is None or end is None:
        return None
    return Decimal((end - start).total_seconds())


def trade_fills(database: Path, zone: ZoneInfo, quotes: Quotes | None,
                environment: str | None = None) -> list[TradeFill]:
    """Closed trades with any fill, oldest close first (the selection R5 uses)."""
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
        times = _journal_times(store, [o for orders in by_trade.values() for o in orders])
    finally:
        store.close()
    result = []
    for row in rows:
        orders = by_trade.get(row["trade_id"], [])
        if any(o.filled_quantity > 0 for o in orders):
            day = datetime.fromisoformat(row["closed_at"]).astimezone(zone).date()
            result.append(_measure(day, row, orders, quotes, times))
    return result


def _measure(day: date, row, orders: list[OrderRow], quotes: Quotes | None,
             times: tuple[dict[str, datetime], dict[str, datetime], dict[str, datetime]]
             ) -> TradeFill:
    intent, attempted, filled = times
    causes: list[str] = []
    fills = [o for o in orders if o.filled_quantity > 0]
    entries = [o for o in fills if o.leg is Leg.ENTRY]
    exits = [o for o in fills if o.leg is Leg.EXIT]
    entry_fill, exit_fill = _weighted(entries), _weighted(exits)
    if entries and entry_fill is None:
        causes.append("an entry has no fill price")
    if exits and exit_fill is None:
        causes.append("a market exit has no fill price")
    ask = bid = age = None
    if quotes is not None and entries:
        cid = entries[0].client_order_id
        found = quotes.at(row["symbol"], intent.get(cid))
        if found is None:
            causes.append("no captured quote before the entry decision")
        else:
            ask, age = found.ask, _seconds(found.received, intent[cid])
    if quotes is not None and exits:
        found = quotes.at(row["symbol"], intent.get(exits[0].client_order_id))
        if found is None:
            causes.append("no captured quote before the exit decision")
        else:
            bid = found.bid
    fees = None if any(o.fees_usd is None for o in fills) else sum(
        (o.fees_usd for o in fills), ZERO)
    if fees is None:
        causes.append("fees not recorded")

    def delay(group: list[OrderRow]) -> Decimal | None:
        return next((_seconds(attempted.get(o.client_order_id), filled[o.client_order_id])
                     for o in group if o.client_order_id in filled), None)

    return TradeFill(
        day=day, trade_id=row["trade_id"], symbol=row["symbol"],
        entry_shares=sum((o.filled_quantity for o in entries), ZERO) or None,
        entry_limit=entries[0].limit_price if entries else None, entry_fill=entry_fill,
        entry_ask=ask, entry_quote_age_s=age, entry_delay_s=delay(entries),
        exit_shares=sum((o.filled_quantity for o in exits), ZERO) or None,
        exit_fill=exit_fill, exit_bid=bid, exit_delay_s=delay(exits),
        shares_filled=sum((o.filled_quantity for o in fills), ZERO), fees_usd=fees,
        cause="; ".join(causes) or None)


def _percent(parts: Sequence[tuple[Decimal | None, Decimal | None, Decimal | None]]
             ) -> Decimal | None:
    """sum(slip x shares) / sum(reference x shares) x 100 over (slip, reference, shares)."""
    notional = sum((ref * n for _, ref, n in parts), ZERO)
    if notional == 0:
        return None
    return sum((s * n for s, _, n in parts), ZERO) / notional * HUNDRED


def metrics(trades: Sequence[TradeFill], stopped: Sequence[slippage.StoppedTrade],
            day: date | None) -> dict[str, Decimal | int | None]:
    """The report's figures over the trades of one day (every day when `day` is None)."""
    t = [x for x in trades if day is None or x.day == day]
    s = [x for x in stopped if day is None or x.day == day]
    vs_limit = [(x.entry_vs_limit_usd, x.entry_limit, x.entry_shares) for x in t
                if x.entry_vs_limit_usd is not None]
    vs_quote = [(x.entry_vs_quote_usd, x.entry_ask, x.entry_shares) for x in t
                if x.entry_vs_quote_usd is not None]
    exit_quote = [(x.exit_vs_quote_usd, x.exit_bid, x.exit_shares) for x in t
                  if x.exit_vs_quote_usd is not None]
    with_fees = [x for x in t if x.fees_usd is not None]
    fees = sum((x.fees_usd for x in with_fees), ZERO)
    shares = sum((x.shares_filled for x in with_fees), ZERO)
    entry_delays = [x.entry_delay_s for x in t if x.entry_delay_s is not None]
    exit_delays = [x.exit_delay_s for x in t if x.exit_delay_s is not None]
    return {
        "trades": len(t),
        "entry_slippage_vs_limit_percent": _percent(vs_limit),
        "entry_slippage_vs_quote_percent": _percent(vs_quote),
        "entry_quote_trades": len(vs_quote),
        "stopped_trades": len(s),
        "stop_slippage_percent": slippage.group(s, day).stop_slippage_percent,
        "exit_slippage_vs_quote_percent": _percent(exit_quote),
        "exit_quote_trades": len(exit_quote),
        "fee_trades": len(with_fees),
        "trades_without_fees": len(t) - len(with_fees),
        "fees_per_trade_usd": fees / len(with_fees) if with_fees else None,
        "fees_per_share_usd": fees / shares if shares else None,
        "entry_delay_median_s": statistics.median(entry_delays) if entry_delays else None,
        "entry_delay_max_s": max(entry_delays, default=None),
        "exit_delay_median_s": statistics.median(exit_delays) if exit_delays else None,
        "exit_delay_max_s": max(exit_delays, default=None),
    }
