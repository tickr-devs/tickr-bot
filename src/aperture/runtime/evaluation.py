"""Run the active strategy once per completed minute (spec 5) and hand each decision to the
entry desk with exactly the bar and quote the strategy saw.

Per completed minute, in the regular session: the open positions' symbols, then the WATCH
symbols, then the latest discovery candidates. Today's completed regular-session M1 bars come
from `MarketData.bars`, the prior sessions from `HistoryReader` (once per symbol per day, then
cached). A history refusal is not an error here: the strategy gets no prior sessions and
refuses with its own cause, and the decision is recorded like any other. A budget refusal,
delayed data or a failed read stops the remaining symbols for this minute (journaled); a
strategy exception is journaled and that symbol skipped. As soon as a decision leaves an order
needing the loop (an entry sent, an exit asked; runtime/protection.py), the rest of the minute
is dropped (journaled), so the fill and its stop are not kept waiting behind more reads.
Nothing here ever ends the trading loop.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import date, datetime, timedelta

from aperture.broker.base import BrokerError
from aperture.calendar import NEW_YORK, CalendarRefused, SessionCalendar
from aperture.config import Config
from aperture.data.base import MarketData, MarketDataError
from aperture.data.history import HistoryReader, HistoryUnavailable, SessionHistory
from aperture.engine.core import Engine, Leg
from aperture.runtime.entries import EntryDesk
from aperture.runtime.market import StreamInbox
from aperture.runtime.protection import busy
from aperture.strategy.base import PositionView, Strategy, SymbolContext, Verdict
from aperture.types import Bar

M1 = timedelta(minutes=1)
EVALUATION_STOPPED = "evaluation-stopped"
STRATEGY_ERROR = "strategy-error"
HISTORY_REFUSED = "history-refused"


def _chunks(items: Sequence[str], size: int) -> list[Sequence[str]]:
    return [items[i:i + size] for i in range(0, len(items), size)]


class Evaluator:
    def __init__(self, engine: Engine, config: Config, source: MarketData, *,
                 strategy: Strategy, desk: EntryDesk, history: HistoryReader,
                 inbox: StreamInbox, calendar: SessionCalendar,
                 now: Callable[[], datetime]) -> None:
        self.engine = engine
        self.config = config
        self.source = source
        self.strategy = strategy
        self.desk = desk
        self.history = history
        self.inbox = inbox
        self.calendar = calendar
        self.now = now
        self.watch: set[str] = set()
        self.contexts: list[SymbolContext] = []  # of the latest pass, for observers
        self.evaluated_at: datetime | None = None  # decision time of `contexts`
        self._last_bar: datetime | None = None
        self._history_refused: set[tuple[str, date]] = set()

    def _journal(self, kind: str, body: dict[str, object]) -> None:
        with self.engine.store.transaction():
            self.engine.store.append(kind, body, run_id=self.engine.run_id)

    def _positions(self) -> dict[str, PositionView]:
        views = {}
        for trade in self.engine.trades():
            orders = self.engine.orders(trade.trade_id)
            quantity = self.engine.expected_quantity(orders)
            entry = next((o for o in orders if o.leg is Leg.ENTRY), None)
            if quantity > 0 and entry is not None and entry.average_fill_price is not None:
                views[trade.symbol] = PositionView(entry.average_fill_price, quantity,
                                                   trade.stop_price)
        return views

    def tick(self, candidates: Sequence[str]) -> None:
        """Once per loop pass; does work only when a new minute has completed."""
        self.contexts = []
        now = self.now()
        self.evaluated_at = now
        try:
            day = self.calendar.day_at(now)
        except CalendarRefused:
            return
        if not day.in_regular_session(now):
            return
        bar_start = now.replace(second=0, microsecond=0) - M1
        if bar_start < day.regular_open or bar_start == self._last_bar:
            return
        self._last_bar = bar_start
        positions = self._positions()
        ordered = list(dict.fromkeys([*sorted(positions), *sorted(self.watch), *candidates]))
        today = now.astimezone(NEW_YORK).date()
        md = self.config.market_data
        for chunk in _chunks(ordered, md.bars_symbols_per_request):
            try:
                bars = self.source.bars(list(chunk), md.bars_per_symbol)
            except (MarketDataError, BrokerError) as exc:  # RateLimited, DelayedData included
                self._journal(EVALUATION_STOPPED, {"bar_start": bar_start.isoformat(),
                                                   "stage": "bars",
                                                   "error": type(exc).__name__})
                return
            for symbol in chunk:
                own = [b for b in bars if b.symbol == symbol
                       and day.regular_open <= b.start and b.start + M1 <= now]
                if not own:
                    continue
                error, history = self._history(symbol, today)
                if error is not None:
                    self._journal(EVALUATION_STOPPED, {"bar_start": bar_start.isoformat(),
                                                       "stage": "history", "symbol": symbol,
                                                       "error": error})
                    return
                self._evaluate(symbol, own, history, positions.get(symbol), now)
                cause = busy(self.engine)
                if cause is not None:  # an entry or exit just went out: protect it first
                    self._journal(EVALUATION_STOPPED, {"bar_start": bar_start.isoformat(),
                                                       "stage": "protect-first",
                                                       "symbol": symbol, "cause": cause})
                    return

    def _history(self, symbol: str, today: date) -> tuple[str | None, SessionHistory | None]:
        """(error type, prior sessions). History None when refused (recorded once a day); an
        error (budget, delayed data, a failed or malformed read) stops this minute: the next
        minute tries again."""
        try:
            return None, self.history.prior_sessions(symbol, today,
                                                     self.config.market_data.history_sessions)
        except HistoryUnavailable as exc:
            if (symbol, today) not in self._history_refused:
                self._history_refused.add((symbol, today))
                self._journal(HISTORY_REFUSED, {"symbol": symbol, "cause": exc.cause,
                                                "detail": exc.detail})
            return None, None
        except (MarketDataError, BrokerError) as exc:  # RateLimited, DelayedData included
            return type(exc).__name__, None

    def _evaluate(self, symbol: str, today_bars: list[Bar], history: SessionHistory | None,
                  position: PositionView | None, now: datetime) -> None:
        prior = () if history is None else tuple(history.bars_of(d) for d in history.sessions)
        bars = ([] if history is None else list(history.bars)) + today_bars
        quote = self.inbox.latest(symbol)
        context = SymbolContext(symbol, tuple(bars), prior, quote, position)
        try:
            decision = self.strategy.evaluate(context)
        except Exception as exc:  # noqa: BLE001 - a strategy bug never ends the loop
            self._journal(STRATEGY_ERROR, {"symbol": symbol, "error": type(exc).__name__,
                                           "detail": str(exc)})
            return
        if decision.verdict is Verdict.WATCH:
            self.watch.add(symbol)
        elif decision.verdict is Verdict.IGNORE:
            self.watch.discard(symbol)
        self.desk.on_decision(decision, bar_start=today_bars[-1].start, quote=quote, now=now)
        self.contexts.append(context)
