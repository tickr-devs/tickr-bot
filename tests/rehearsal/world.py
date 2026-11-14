"""A simulated exchange day for rehearsing `aperture run` end to end on fakes.

`World` owns the clock (wall time and monotonic seconds advance together when the bot sleeps),
the minute price paths of each symbol, and two fakes that read them: `Exchange` (a FakeBroker
that matches resting orders against the current price) and `Feed` (a FakeMarketData that
answers screeners, snapshots and bars from the paths and streams L1 quotes to the subscriber).

Prices inside a minute move linearly from the bar's open to its close; bid and ask sit one
`half_spread` either side. Orders match after every clock advance: a LIMIT BUY fills at the ask
when the ask is at or below its limit, a STOP_LOSS SELL fills at the bid once the bid is at or
below its stop (a gap fills below the stop), a MARKET SELL fills at the bid. Only in the regular
session.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal

from aperture.broker.base import Acknowledgement
from aperture.broker.fake import FakeBroker
from aperture.calendar import NEW_YORK
from aperture.data.base import FakeMarketData, QuoteListener, ScreenerKind, ScreenerRow
from aperture.types import Bar, OrderRequest, OrderType, Quote, Side

M1 = timedelta(minutes=1)
CENT = Decimal("0.01")
OPEN = time(9, 30)
CLOSE = time(16, 0)
SESSION_MINUTES = 390


def ny(day: date, clock: str) -> datetime:
    return datetime.combine(day, time.fromisoformat(clock), NEW_YORK).astimezone(UTC)


def flat_session(symbol: str, day: date, price: Decimal, volume: Decimal) -> list[Bar]:
    start = ny(day, "09:30")
    return [Bar(symbol, start + i * M1, price, price, price, price, volume)
            for i in range(SESSION_MINUTES)]


def build_day(symbol: str, day: date, base: Decimal, volume: Decimal,
              legs: Sequence[tuple[str, str, Decimal, Decimal]],
              wick: Decimal = Decimal("0.05")) -> list[Bar]:
    """A full regular session starting at `base` with `volume` per minute, except legs
    (from "HH:MM", to "HH:MM", price at the end, volume per minute) that move the price
    linearly; the price holds between legs. Legs are in time order and do not overlap."""
    start = ny(day, "09:30")
    plan: dict[int, tuple[Decimal, Decimal]] = {}
    price = base
    for begin, end, target, leg_volume in legs:
        first, last = int((ny(day, begin) - start) / M1), int((ny(day, end) - start) / M1)
        steps = last - first
        for k in range(steps):
            plan[first + k] = ((price + (target - price) * (k + 1) / steps).quantize(CENT),
                               leg_volume)
        price = target
    bars = []
    price = base
    for i in range(SESSION_MINUTES):
        close, minute_volume = plan.get(i, (price, volume))
        bars.append(Bar(symbol, start + i * M1, price, max(price, close) + wick,
                        min(price, close) - wick, close, minute_volume))
        price = close
    return bars


class World:
    def __init__(self, start: datetime, half_spread: Decimal = CENT) -> None:
        self.now = start
        self.monotonic = 0.0
        self.half_spread = half_spread
        self.today: dict[str, list[Bar]] = {}
        self.history: dict[str, list[Bar]] = {}
        self.screeners: dict[ScreenerKind, list[str]] = {k: [] for k in ScreenerKind}
        self.exchange = Exchange(self)
        self.feed = Feed(self)
        self.events: list[tuple[datetime, Callable[[], None]]] = []
        self.stream_up = False
        # How far the exchange runs ahead of the quote feed (feed latency); orders match there.
        self.exchange_lead = timedelta(0)

    # --- scenario ------------------------------------------------------------------------

    def add_symbol(self, symbol: str, today: list[Bar], history: Iterable[Bar],
                   kinds: Iterable[ScreenerKind] = (ScreenerKind.GAINERS,)) -> None:
        self.today[symbol] = today
        self.history[symbol] = list(history)
        for kind in kinds:
            self.screeners[kind].append(symbol)

    def at(self, when: datetime, action: Callable[[], None]) -> None:
        self.events.append((when, action))
        self.events.sort(key=lambda e: e[0])

    # --- clock ---------------------------------------------------------------------------

    def clock(self) -> datetime:
        return self.now

    def mono(self) -> float:
        return self.monotonic

    def sleep(self, seconds: float) -> None:
        self.advance(seconds)

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)
        self.monotonic += seconds
        self.exchange.match()
        if self.stream_up:
            self.feed.stream()
        while self.events and self.events[0][0] <= self.now:
            _, action = self.events.pop(0)
            action()

    # --- prices --------------------------------------------------------------------------

    def in_session(self) -> bool:
        local = self.now.astimezone(NEW_YORK).time()
        return OPEN <= local < CLOSE

    def price(self, symbol: str, lead: timedelta = timedelta(0)) -> Decimal:
        bars = self.today[symbol]
        at = self.now + lead
        if at < bars[0].start:
            return bars[0].open
        for bar in bars:
            if bar.start <= at < bar.start + M1:
                fraction = Decimal((at - bar.start).total_seconds()) / Decimal(60)
                return (bar.open + (bar.close - bar.open) * fraction).quantize(CENT)
        return bars[-1].close

    def change(self, symbol: str) -> Decimal:
        """Today's change against the open, as a ratio."""
        first = self.today[symbol][0].open
        return (self.price(symbol) - first) / first

    def quote(self, symbol: str, lead: timedelta = timedelta(0)) -> Quote:
        """The quote now, or (with `lead`) where the exchange already is while the feed lags."""
        mid = self.price(symbol, lead)
        return Quote(symbol, mid - self.half_spread, mid + self.half_spread, Decimal(500),
                     Decimal(500), self.now, self.now)

    def completed_and_current(self, symbol: str) -> list[Bar]:
        """Bars that have started (the current one as if complete, like a provider may)."""
        return [b for b in self.today.get(symbol, []) if b.start <= self.now]


class Exchange(FakeBroker):
    def __init__(self, world: World) -> None:
        super().__init__()
        self.world = world
        self.requests: dict[str, OrderRequest] = {}
        self.fills: list[tuple[datetime, str, Side, OrderType, Decimal, Decimal]] = []

    def place(self, request: OrderRequest) -> Acknowledgement:
        ack = super().place(request)
        self.requests[request.client_order_id] = request
        self._match(request.client_order_id)  # a marketable order fills on arrival
        return ack

    def match(self) -> None:
        for cid in list(self.orders):
            self._match(cid)

    def _match(self, cid: str) -> None:
        order = self.orders[cid]
        if order.state.terminal or not self.world.in_session():
            return
        request = self.requests[cid]
        quote = self.world.quote(order.symbol, self.world.exchange_lead)
        price: Decimal | None = None
        if order.order_type is OrderType.LIMIT and order.side is Side.BUY:
            assert request.limit_price is not None
            if quote.ask <= request.limit_price:
                price = quote.ask
        elif order.order_type is OrderType.STOP_LOSS and order.side is Side.SELL:
            assert request.stop_price is not None
            if quote.bid <= request.stop_price:
                price = quote.bid
        elif order.order_type is OrderType.MARKET:
            price = quote.bid if order.side is Side.SELL else quote.ask
        if price is None:
            return
        quantity = order.quantity - order.filled_quantity
        self.fill(cid, quantity, price)
        self.fills.append((self.world.now, order.symbol, order.side, order.order_type,
                           quantity, price))




class Feed(FakeMarketData):
    def __init__(self, world: World) -> None:
        super().__init__("rehearsal")
        self.world = world

    def stream(self) -> None:
        for symbol in sorted(self.subscribed):
            if symbol in self.world.today:
                self.push_quote(self.world.quote(symbol))

    def screener(self, kind: ScreenerKind) -> list[ScreenerRow]:
        self._call("screener", [kind.value])
        # Ranked like a gainers list: by today's change, best first (losers: worst first).
        symbols = sorted(self.world.screeners[kind], key=self.world.change,
                         reverse=kind is not ScreenerKind.LOSERS)
        return [ScreenerRow(kind, rank, f"id-{symbol}", symbol, "NSQ", "USD", "COMMON_STOCK",
                            {"symbol": symbol})
                for rank, symbol in enumerate(symbols, start=1)]

    def bars(self, symbols: Sequence[str], count: int) -> list[Bar]:
        self._call("bars", symbols)
        return [b for s in symbols for b in self.world.completed_and_current(s)[-count:]]

    def bars_before(self, symbol: str, end: datetime, count: int) -> list[Bar]:
        self._call("bars_before", [symbol])
        older = [b for b in self.world.history.get(symbol, []) + self.world.today.get(symbol, [])
                 if b.start < end and b.start <= self.world.now]
        older.sort(key=lambda b: b.start)
        return older[-count:] if count > 0 else []

    def snapshot(self, symbols: Sequence[str]) -> list[Quote]:
        self._call("snapshot", symbols)
        return [self.world.quote(s) for s in symbols if s in self.world.today]

    def subscribe(self, symbols: Iterable[str], listener: QuoteListener) -> None:
        super().subscribe(symbols, listener)
