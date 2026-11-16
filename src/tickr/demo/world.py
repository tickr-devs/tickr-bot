"""A simulated exchange day for running the real bot on: `tickr demo` and the rehearsals.

`World` owns the clock (wall time and monotonic seconds advance together when the bot sleeps),
the minute price paths of each symbol, and two fakes that read them: `Exchange` (a FakeBroker
that matches resting orders against the current price) and `Feed` (a FakeMarketData that
answers screeners, snapshots and bars from the paths and streams L1 quotes to the subscriber).
Nothing here talks to a network.

Prices inside a minute move linearly from the bar's open to its close; bid and ask sit one
`half_spread` either side. Orders match after every clock advance: a LIMIT BUY fills at the ask
when the ask is at or below its limit, a STOP_LOSS SELL fills at the bid once the bid is at or
below its stop (a gap fills below the stop), a MARKET SELL fills at the bid. Only in the regular
session of the world's market day.

`Exchange.unverify_stops` is a demo fault for QA: from then on the symbol's stop orders show a
status the adapter cannot map (`OrderState.UNKNOWN`), are not working and never fill, so the
bot sees a position without a verified stop and the apps show it unprotected.

`go_live` switches the clock from simulated to real time: from then on a bot sleep really
sleeps and the world's time moves with the wall clock, shifted by a fixed offset.
`call_soon` hands an action from another thread to the bot's thread (the next clock advance).

`daily_history` draws older days for the charts only: one D bar per weekday before the minute
history, a seeded random walk that ends at the first minute day's open. The feed serves them on
D charts; the bot never reads them (its history reads are minute bars, which they do not have).

`textured` roughens a planned path for `tickr demo`, so its charts look like a real tape:
bodies and wicks of different sizes and an uneven volume, the same on every run (seeded by
symbol and minute). The rehearsals keep the plain planned bars.
"""

from __future__ import annotations

import queue
import random
import time
import zlib
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, timedelta
from datetime import time as wall_time
from decimal import ROUND_FLOOR, Decimal

from tickr.broker.base import Acknowledgement
from tickr.broker.fake import FakeBroker
from tickr.calendar import NEW_YORK, MarketDay, ny_date_of
from tickr.data.base import (
    BarSpan,
    FakeMarketData,
    ScreenerKind,
    ScreenerRow,
    ScreenerSession,
    SessionBar,
)
from tickr.types import Bar, OrderRequest, OrderState, OrderType, Quote, Side

M1 = timedelta(minutes=1)
REGULAR_SESSION = "RTH"  # the provider's label of a regular-session bar (the world has no other)
CENT = Decimal("0.01")  # prices are quoted in cents
SHARE = Decimal(1)  # volumes are whole shares
SECONDS_PER_MINUTE = Decimal(60)
# Screener row fields the fake feed fills in (protocol values of a US common stock row).
SCREENER_EXCHANGE = "NSQ"
SCREENER_CURRENCY = "USD"
SCREENER_TYPE = "COMMON_STOCK"
# Raw status a stop shows after the stop-unverified demo fault: one the adapter does not map.
UNVERIFIED_STOP_STATUS = "DEMO_UNVERIFIED"
SATURDAY = 5  # date.weekday() of Saturday; Monday is 0
REGULAR_OPEN = "09:30"  # New York label of a chart-only D bar (protocol: the regular open)

# (from "HH:MM", to "HH:MM", price at the end, volume per minute): a straight price move.
Leg = tuple[str, str, Decimal, Decimal]


def ny(day: date, clock: str) -> datetime:
    """UTC instant of a New York wall time "HH:MM" on `day`."""
    return datetime.combine(day, wall_time.fromisoformat(clock), NEW_YORK).astimezone(UTC)


def session_minutes(market_day: MarketDay) -> int:
    return int((market_day.regular_close - market_day.regular_open) / M1)


def flat_bars(symbol: str, market_day: MarketDay, price: Decimal, volume: Decimal) -> list[Bar]:
    """A regular session that never moves: every bar at `price` with `volume`."""
    start = market_day.regular_open.astimezone(UTC)
    return [Bar(symbol, start + i * M1, price, price, price, price, volume)
            for i in range(session_minutes(market_day))]


def day_bars(symbol: str, market_day: MarketDay, base: Decimal, volume: Decimal,
             legs: Sequence[Leg], wick: Decimal) -> list[Bar]:
    """A full regular session starting at `base` with `volume` per minute, except legs that move
    the price linearly; the price holds between legs. Every bar's high and low stick out by
    `wick`. Legs are in time order, do not overlap and lie inside the session."""
    start = market_day.regular_open.astimezone(UTC)
    total = session_minutes(market_day)
    plan: dict[int, tuple[Decimal, Decimal]] = {}
    price = base
    previous_end = 0
    for begin, end, target, leg_volume in legs:
        first = int((ny(market_day.ny_date, begin) - start) / M1)
        last = int((ny(market_day.ny_date, end) - start) / M1)
        if not previous_end <= first < last <= total:
            raise ValueError(f"{symbol}: leg {begin}-{end} overlaps or leaves the session")
        steps = last - first
        for k in range(steps):
            plan[first + k] = ((price + (target - price) * (k + 1) / steps).quantize(CENT),
                               leg_volume)
        price, previous_end = target, last
    bars = []
    price = base
    for i in range(total):
        close, minute_volume = plan.get(i, (price, volume))
        bars.append(Bar(symbol, start + i * M1, price, max(price, close) + wick,
                        min(price, close) - wick, close, minute_volume))
        price = close
    return bars


@dataclass(frozen=True)
class DailyWalk:
    """How `daily_history` draws a day; every field is a fraction of the price.

    move: spread of a day's close against its open. gap: spread of a day's open against the
    previous close. wick: largest high above (and low below) the day's body."""

    move: Decimal
    gap: Decimal
    wick: Decimal


def weekdays_before(day: date, count: int) -> list[date]:
    """The `count` weekdays before `day`, oldest first (chart decoration: holidays included)."""
    found: list[date] = []
    while len(found) < count:
        day -= timedelta(days=1)
        if day.weekday() < SATURDAY:
            found.append(day)
    return list(reversed(found))


def daily_history(symbol: str, days: Sequence[date], first_open: Decimal, volume: Decimal,
                  walk: DailyWalk) -> list[Bar]:
    """One D bar per day of `days` (oldest first), labelled at the regular open; the last
    one closes so that the next day opens at `first_open`. Deterministic: seeded by symbol."""
    rng = random.Random(zlib.crc32(f"{symbol} daily".encode()))
    move, gap, wick = float(walk.move), float(walk.gap), float(walk.wick)
    bars: list[Bar] = []
    next_open = first_open
    for day in reversed(days):
        close = max((next_open / Decimal(repr(1.0 + rng.gauss(0.0, gap)))).quantize(CENT), CENT)
        open_ = max((close / Decimal(repr(1.0 + rng.gauss(0.0, move)))).quantize(CENT), CENT)
        high = (max(open_, close) * Decimal(repr(1.0 + rng.uniform(0.0, wick)))).quantize(CENT)
        low = (min(open_, close) * Decimal(repr(1.0 - rng.uniform(0.0, wick)))).quantize(CENT)
        bars.append(Bar(symbol, ny(day, REGULAR_OPEN), open_, high, max(low, CENT), close,
                        volume))
        next_open = open_
    return list(reversed(bars))


@dataclass(frozen=True)
class Texture:
    """How `textured` roughens planned bars; every field is a fraction, 0 turns that part off.

    drift: spread of the random step the price's deviation from the plan takes each minute,
    of the price. pull: share of the deviation kept into the next minute (below 1), so the price
    wanders around the plan and comes back to it. wick: largest extra wick beyond the body, of the
    price. move: extra spread of that step on a moving bar, of the bar's planned move, so a run-up
    has red bars and pullbacks. move_cap: the most that extra spread can be, of the price (a
    collapse stays a collapse). volume: largest change of a minute's volume either way, of that
    volume (below 1)."""

    drift: Decimal
    pull: Decimal
    wick: Decimal
    move: Decimal
    move_cap: Decimal
    volume: Decimal


def textured(bars: Sequence[Bar], texture: Texture) -> list[Bar]:
    """`bars` with the price wandering around their closes, each bar opening at the previous
    close, uneven wicks and volume. On a bar whose planned move is no bigger than its wick (the
    plan holds or drifts) the close never rises above the planned close and the high reaches it,
    so the noise never closes above the recent highs: a breakout happens only on a leg of the
    plan. Deterministic: seeded by the first bar's symbol and minute."""
    if not bars:
        return []
    first = bars[0]
    rng = random.Random(zlib.crc32(f"{first.symbol} {first.start.isoformat()}".encode()))
    drift, pull = float(texture.drift), float(texture.pull)
    move, move_cap = float(texture.move), float(texture.move_cap)
    wick, volume = float(texture.wick), float(texture.volume)
    # Deviations from the plan, one on legs and one while it holds, so a leg's wide swings do
    # not carry into the quiet minutes after it.
    leg, hold = 0.0, 0.0
    previous: Decimal | None = None
    out = []
    for bar in bars:
        planned = float(abs(bar.close - bar.open) / bar.close)
        open_ = bar.open if previous is None else previous
        upper_wick = bar.high - max(bar.open, bar.close)
        if abs(bar.close - bar.open) > upper_wick:  # a leg: the price may run either side
            leg = pull * leg + rng.gauss(0.0, drift + min(move * planned, move_cap))
            close = (bar.close * Decimal(repr(1.0 + leg))).quantize(CENT)
            top = max(open_, close) + upper_wick
        else:  # holding or drifting: never close above the plan, and reach it
            leg, hold = 0.0, pull * hold + rng.gauss(0.0, drift)
            close = (bar.close * Decimal(repr(1.0 - abs(hold)))).quantize(CENT, ROUND_FLOOR)
            top = max(open_, bar.close) + upper_wick
        bottom = min(open_, close) - (min(bar.open, bar.close) - bar.low)
        high = (top + bar.close * Decimal(repr(rng.uniform(0.0, wick)))).quantize(CENT)
        low = (bottom - bar.close * Decimal(repr(rng.uniform(0.0, wick)))).quantize(CENT)
        shares = (bar.volume * Decimal(repr(1.0 + rng.uniform(-volume, volume)))).quantize(SHARE)
        out.append(Bar(bar.symbol, bar.start, open_, high, max(low, CENT), close, shares))
        previous = close
    return out


class World:
    def __init__(self, start: datetime, market_day: MarketDay, half_spread: Decimal,
                 quote_size: Decimal, provider: str) -> None:
        self.now = start
        self.monotonic = 0.0
        self.market_day = market_day
        self.half_spread = half_spread
        self.quote_size = quote_size
        self.today: dict[str, list[Bar]] = {}
        self.history: dict[str, list[Bar]] = {}
        self.daily: dict[str, list[Bar]] = {}  # chart-only D bars before the minute history
        self.screeners: dict[ScreenerKind, list[str]] = {k: [] for k in ScreenerKind}
        self.listed_from: dict[str, datetime] = {}  # symbols on a screener only from then on
        self.exchange = Exchange(self)
        self.feed = Feed(self, provider)
        self.events: list[tuple[datetime, Callable[[], None]]] = []
        self.stream_up = False
        # How far the exchange runs ahead of the quote feed (feed latency); orders match there.
        self.exchange_lead = timedelta(0)
        # Set by go_live: world time = wall time + this offset, and sleeps are real.
        self.live_offset: timedelta | None = None
        # Actions handed over from other threads (`call_soon`), run at the next clock advance.
        self.soon: queue.SimpleQueue[Callable[[], None]] = queue.SimpleQueue()

    # --- scenario ------------------------------------------------------------------------

    def add_symbol(self, symbol: str, today: list[Bar], history: Iterable[Bar],
                   kinds: Iterable[ScreenerKind] = (ScreenerKind.GAINERS,),
                   daily: Iterable[Bar] = (), listed_from: datetime | None = None) -> None:
        """`listed_from`: the screeners list the symbol only from then on (None: all day)."""
        self.today[symbol] = today
        self.history[symbol] = list(history)
        self.daily[symbol] = list(daily)
        for kind in kinds:
            self.screeners[kind].append(symbol)
        if listed_from is not None:
            self.listed_from[symbol] = listed_from

    def at(self, when: datetime, action: Callable[[], None]) -> None:
        self.events.append((when, action))
        self.events.sort(key=lambda e: e[0])

    def call_soon(self, action: Callable[[], None]) -> None:
        """From any thread: run `action` on the bot's thread at the next clock advance (`at`
        is for the bot's thread only)."""
        self.soon.put(action)

    # --- clock ---------------------------------------------------------------------------

    def clock(self) -> datetime:
        return self.now

    def mono(self) -> float:
        return self.monotonic

    def go_live(self) -> None:
        """From now on the world moves with the wall clock (the demo's live phase)."""
        self.live_offset = self.now - datetime.now(UTC)

    def sleep(self, seconds: float) -> None:
        if self.live_offset is None:
            self.advance(seconds)
            return
        time.sleep(seconds)
        target = datetime.now(UTC) + self.live_offset
        self.advance(max((target - self.now).total_seconds(), seconds))

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)
        self.monotonic += seconds
        self.exchange.match()
        if self.stream_up:
            self.feed.stream()
        while self.events and self.events[0][0] <= self.now:
            _, action = self.events.pop(0)
            action()
        while not self.soon.empty():
            self.soon.get()()

    # --- prices --------------------------------------------------------------------------

    def in_session(self) -> bool:
        return self.market_day.in_regular_session(self.now)

    def price(self, symbol: str, lead: timedelta = timedelta(0)) -> Decimal:
        bars = self.today[symbol]
        at = self.now + lead
        if at < bars[0].start:
            return bars[0].open
        for bar in bars:
            if bar.start <= at < bar.start + M1:
                fraction = Decimal((at - bar.start).total_seconds()) / SECONDS_PER_MINUTE
                return (bar.open + (bar.close - bar.open) * fraction).quantize(CENT)
        return bars[-1].close

    def change(self, symbol: str) -> Decimal:
        """Today's change against the open, as a ratio."""
        first = self.today[symbol][0].open
        return (self.price(symbol) - first) / first

    def quote(self, symbol: str, lead: timedelta = timedelta(0)) -> Quote:
        """The quote now, or (with `lead`) where the exchange already is while the feed lags."""
        mid = self.price(symbol, lead)
        return Quote(symbol, mid - self.half_spread, mid + self.half_spread, self.quote_size,
                     self.quote_size, self.now, self.now)

    def completed_and_current(self, symbol: str) -> list[Bar]:
        """Bars that have started (the current one as if complete, like a provider may)."""
        return [b for b in self.today.get(symbol, []) if b.start <= self.now]


class Exchange(FakeBroker):
    def __init__(self, world: World) -> None:
        super().__init__()
        self.world = world
        self.requests: dict[str, OrderRequest] = {}
        self.fills: list[tuple[datetime, str, Side, OrderType, Decimal, Decimal]] = []
        self.unverified_symbols: set[str] = set()

    def place(self, request: OrderRequest) -> Acknowledgement:
        ack = super().place(request)
        self.requests[request.client_order_id] = request
        self._unverify(request.client_order_id)
        self._match(request.client_order_id)  # a marketable order fills on arrival
        return ack

    def unverify_stops(self, symbol: str) -> None:
        """The demo fault: `symbol`'s working stops, and every later one, stop being working."""
        self.unverified_symbols.add(symbol)
        for cid in list(self.orders):
            self._unverify(cid)

    def _unverify(self, cid: str) -> None:
        order = self.orders[cid]
        if (order.symbol in self.unverified_symbols and order.order_type is OrderType.STOP_LOSS
                and not order.state.terminal):
            self.orders[cid] = replace(order, state=OrderState.UNKNOWN,
                                       raw_status=UNVERIFIED_STOP_STATUS)

    def match(self) -> None:
        for cid in list(self.orders):
            self._match(cid)

    def _match(self, cid: str) -> None:
        order = self.orders[cid]
        if (order.state.terminal or order.state is OrderState.UNKNOWN
                or not self.world.in_session()):
            return  # an unverified stop is not working, so it never fills
        request = self.requests[cid]
        quote = self.world.quote(order.symbol, self.world.exchange_lead)
        price: Decimal | None = None
        if order.order_type is OrderType.LIMIT and order.side is Side.BUY:
            if request.limit_price is not None and quote.ask <= request.limit_price:
                price = quote.ask
        elif order.order_type is OrderType.STOP_LOSS and order.side is Side.SELL:
            if request.stop_price is not None and quote.bid <= request.stop_price:
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
    def __init__(self, world: World, provider: str) -> None:
        super().__init__(provider)
        self.world = world

    def stream(self) -> None:
        for symbol in sorted(self.subscribed):
            if symbol in self.world.today:
                self.push_quote(self.world.quote(symbol))

    def screener(self, kind: ScreenerKind,
                 session: ScreenerSession = ScreenerSession.REGULAR) -> list[ScreenerRow]:
        self._call("screener", [kind.value])
        # Ranked like a gainers list: by today's change, best first (losers: worst first).
        listed = [s for s in self.world.screeners[kind]
                  if self.world.listed_from.get(s, self.world.now) <= self.world.now]
        symbols = sorted(listed, key=self.world.change,
                         reverse=kind is not ScreenerKind.LOSERS)
        # Provider row keys as Webull names them; the world's previous close is today's open.
        return [ScreenerRow(kind, rank, f"id-{symbol}", symbol, SCREENER_EXCHANGE,
                            SCREENER_CURRENCY, SCREENER_TYPE, {
                                "symbol": symbol, "price": str(self.world.price(symbol)),
                                "pre_close": str(self.world.today[symbol][0].open)})
                for rank, symbol in enumerate(symbols, start=1)]

    def bars(self, symbols: Sequence[str], count: int, *, budget_reserve: int = 0) -> list[Bar]:
        self._call("bars", symbols)
        return [b for s in symbols for b in self.world.completed_and_current(s)[-count:]]

    def bars_before(self, symbols: Sequence[str], end: datetime, count: int, *,
                    budget_reserve: int = 0) -> list[Bar]:
        self._call("bars_before", symbols)
        out: list[Bar] = []
        for symbol in symbols:
            older = [b for b in self.world.history.get(symbol, [])
                     + self.world.today.get(symbol, [])
                     if b.start < end and b.start <= self.world.now]
            older.sort(key=lambda b: b.start)
            out.extend(older[-count:] if count > 0 else [])
        return out

    def history_bars(self, symbol: str, span: BarSpan, end: datetime,
                     count: int) -> list[SessionBar]:
        """GET /chart in the demo: the world's regular-session minutes started by now, or one
        D bar per session day merged from them (labelled with its first minute), after the
        chart-only D bars of the days before the minute history."""
        self._call("history_bars", [symbol, span.value])
        minutes = sorted((b for b in self.world.history.get(symbol, [])
                          + self.world.today.get(symbol, [])
                          if b.start < end and b.start <= self.world.now),
                         key=lambda b: b.start)
        if span is BarSpan.M1:
            rows = [SessionBar(b, REGULAR_SESSION) for b in minutes]
        else:
            days: dict[date, list[Bar]] = {}
            for b in minutes:
                days.setdefault(ny_date_of(b.start), []).append(b)
            older = [SessionBar(b, None) for b in self.world.daily.get(symbol, [])
                     if b.start < end]
            rows = older + [
                SessionBar(Bar(symbol, bars[0].start, bars[0].open, max(b.high for b in bars),
                               min(b.low for b in bars), bars[-1].close,
                               sum((b.volume for b in bars), Decimal(0))), None)
                for bars in days.values()]
        return rows[-count:] if count > 0 else []

    def snapshot(self, symbols: Sequence[str]) -> list[Quote]:
        self._call("snapshot", symbols)
        return [self.world.quote(s) for s in symbols if s in self.world.today]
