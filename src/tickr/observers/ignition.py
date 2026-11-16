"""Observer "early thin ignition": paper trades only, never an order.

Owner 2026-11-16. A thin stock that suddenly runs: on a discovery pass in the regular session,
between `first_signal_minutes_after_open` and `last_signal_minutes_before_close`, all of

- the price is the day's high (a new session high);
- the price is at least `min_rise_percent` above the lowest price of the last
  `low_window_minutes`;
- that low was set less than `low_max_age_minutes` ago;
- the session dollar volume so far is below `max_session_dollar_volume_usd`.

The signal is read from the screener rows discovery already reads each minute (`price`,
`volume`, `high`, `low`), so it costs no request: one price sample per minute instead of OHLC,
seen only while the symbol is on a list (owner: a few seconds later is fine). The low is the
lowest sampled price, or a new day low seen in a row. The dollar volume adds each pass's volume
growth times its price to the first row's volume times its price (an over-estimate for a stock
that already rose, so a thin signal is missed rather than invented).

Paper entry: at the ask of the same pass's snapshot quote (discovery reads it right after the
screeners: about the next bar's open). No spread limit and no dollar-volume floor. A signal
without a quote, or while the symbol's paper trade is open, is recorded and dies.

Paper exit, on the bid: once a symbol has an open paper trade it joins the L1 stream's observer
tier first (owner 2026-11-16: ticks, no bars quota; the tier's cap and its priority below the
position, WATCH symbols and candidates are `runtime/market.py`'s), and the snapshot quotes of
each later discovery pass are used too. Every quote newer than the last one used moves the
highest bid since entry; a bid `trail_percent` or more below it exits at that bid.
`variant_trail_percents` are recorded for the same entry. The first quote at or after
`flat_minutes_before_close` before the regular close ends every paper trade at its bid (a pass
`flat_grace_seconds` later without one: the last bid seen). A new signal may enter again once
the main trail has exited. Each exit records the bid, ask and spread there and the result in
percent and in USD on `paper_notional_usd`, and how many stream and snapshot quotes it saw. A
pause of `quote_gap_seconds` or more between two quotes is recorded as a gap (a halt, a quiet
book, or no stream slot: the snapshot's trade status is not read). The tier is not changed while
the bot holds a position or a working entry, so a paper trade begun then is followed by the
snapshots only until it gets a slot.

Records go to `ignition.jsonl` next to the decision records. The object is shared by the
discovery thread (rows, snapshot quotes, signals), the stream thread (quotes) and the loop (the
symbols it follows), so every method takes its lock. After a restart, open paper trades are taken
back from the file of the day.
"""

from __future__ import annotations

import json
import os
import threading
import tomllib
from collections import deque
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from tickr.calendar import MarketDay, ny_date_of
from tickr.config import Config
from tickr.data.base import ScreenerRow, StreamNotice
from tickr.types import Bar, Quote

FILE_NAME = "ignition.jsonl"
FILE_MODE = 0o600
PERCENT = Decimal(100)
CENTS = Decimal("0.01")
SECONDS_PER_MINUTE = 60

SIGNAL = "ignition-signal"
EXIT = "ignition-exit"
GAP = "ignition-gap"

ENTERED = "entered"
DIED_NO_QUOTE = "no valid quote"
DIED_OPEN = "a paper trade in the symbol is open"

TRAIL = "trail"
FLAT = "flat"
FLAT_LAST_BID = "flat-last-bid"
FLAT_NO_QUOTE = "flat-no-quote-since-restart"
REPLACED = "replaced-by-a-new-entry"

STREAM = "stream"
SNAPSHOT = "snapshot"

SECTIONS = {
    "strategy": {"name", "version"},
    "signal": {"first_signal_minutes_after_open", "last_signal_minutes_before_close",
               "min_rise_percent", "low_window_minutes", "low_max_age_minutes",
               "max_session_dollar_volume_usd"},
    "exit": {"trail_percent", "variant_trail_percents", "flat_minutes_before_close",
             "flat_grace_seconds", "paper_notional_usd"},
    "record": {"quote_gap_seconds"},
}


class IgnitionError(ValueError):
    """Unusable values in the observer's strategy.toml."""


def _whole(raw: Any) -> int:
    if isinstance(raw, bool) or not isinstance(raw, int) or raw < 1:
        raise IgnitionError(f"{raw!r} is not a whole number of at least 1")
    return raw


def _positive(raw: Any) -> Decimal:
    if isinstance(raw, bool) or not isinstance(raw, int | float):
        raise IgnitionError(f"{raw!r} is not a number")
    value = Decimal(str(raw))
    if not value.is_finite() or value <= 0:
        raise IgnitionError(f"{raw!r} is not positive")
    return value


def _ratio(raw: Any) -> Decimal:
    return _positive(raw) / PERCENT


@dataclass(frozen=True)
class Rules:
    name: str
    version: str
    first_signal_minutes_after_open: int
    last_signal_minutes_before_close: int
    min_rise: Decimal  # ratio
    low_window: timedelta
    low_max_age: timedelta
    max_dollar_volume: Decimal
    trail: Decimal  # ratio
    variants: tuple[Decimal, ...]  # ratios
    flat_minutes_before_close: int
    flat_grace: timedelta
    notional: Decimal
    quote_gap: timedelta

    @classmethod
    def from_values(cls, values: Mapping[str, Any]) -> Rules:
        if set(values) != set(SECTIONS):
            raise IgnitionError(f"sections must be exactly {sorted(SECTIONS)}")
        for section, keys in SECTIONS.items():
            if not isinstance(values[section], Mapping) or set(values[section]) != keys:
                raise IgnitionError(f"[{section}] keys must be exactly {sorted(keys)}")
        strategy, signal, exit_ = values["strategy"], values["signal"], values["exit"]
        variants = exit_["variant_trail_percents"]
        if not isinstance(variants, list):
            raise IgnitionError("variant_trail_percents must be a list")
        rules = cls(
            name=str(strategy["name"]), version=str(strategy["version"]),
            first_signal_minutes_after_open=_whole(signal["first_signal_minutes_after_open"]),
            last_signal_minutes_before_close=_whole(signal["last_signal_minutes_before_close"]),
            min_rise=_ratio(signal["min_rise_percent"]),
            low_window=timedelta(minutes=_whole(signal["low_window_minutes"])),
            low_max_age=timedelta(minutes=_whole(signal["low_max_age_minutes"])),
            max_dollar_volume=_positive(signal["max_session_dollar_volume_usd"]),
            trail=_ratio(exit_["trail_percent"]),
            variants=tuple(_ratio(v) for v in variants),
            flat_minutes_before_close=_whole(exit_["flat_minutes_before_close"]),
            flat_grace=timedelta(seconds=_whole(exit_["flat_grace_seconds"])),
            notional=_positive(exit_["paper_notional_usd"]),
            quote_gap=timedelta(seconds=_whole(values["record"]["quote_gap_seconds"])))
        if any(t >= 1 for t in rules.trails):
            raise IgnitionError("trail percents must be below 100")
        if len(set(rules.trails)) != len(rules.trails):
            raise IgnitionError("each trail percent once")
        if rules.low_max_age > rules.low_window:
            raise IgnitionError("low_max_age_minutes above low_window_minutes")
        if rules.last_signal_minutes_before_close <= rules.flat_minutes_before_close:
            raise IgnitionError("the last signal must come before the flat time")
        return rules

    @property
    def trails(self) -> tuple[Decimal, ...]:
        """The main trail first."""
        return (self.trail, *self.variants)


def _field(fields: Mapping[str, str], key: str) -> Decimal | None:
    try:
        value = Decimal(fields[key])
    except (KeyError, InvalidOperation, TypeError):
        return None
    return value if value.is_finite() and value > 0 else None


def _valid(quote: Quote | None) -> bool:
    return quote is not None and 0 < quote.bid <= quote.ask


def percent_text(ratio: Decimal) -> str:
    """A ratio as a plain percent: 0.1 -> "10", 0.125 -> "12.5"."""
    value = ratio * PERCENT
    return str(value.to_integral_value()) if value == value.to_integral_value() else str(value)


def spread_percent(quote: Quote) -> Decimal:
    """(ask - bid) / mid, percent."""
    return (quote.ask - quote.bid) * 2 / (quote.ask + quote.bid) * PERCENT


@dataclass
class Tape:
    """One symbol's price samples within the low window, and its dollar volume estimate."""

    samples: deque[tuple[datetime, Decimal]] = field(default_factory=deque)
    volume: Decimal = Decimal(0)
    dollar_volume: Decimal = Decimal(0)
    day_low: Decimal | None = None

    def add(self, at: datetime, price: Decimal, volume: Decimal, low: Decimal | None,
            window: timedelta) -> None:
        self.dollar_volume += max(volume - self.volume, Decimal(0)) * price
        self.volume = max(volume, self.volume)
        if low is not None and self.day_low is not None and low < self.day_low:
            self.samples.append((at, low))  # a new day low was set since the last pass
        if low is not None:
            self.day_low = low if self.day_low is None else min(low, self.day_low)
        self.samples.append((at, price))
        while self.samples and self.samples[0][0] < at - window:
            self.samples.popleft()

    def low(self) -> tuple[datetime, Decimal]:
        """The lowest sample, the latest of equal ones."""
        at, price = self.samples[0]
        for when, value in self.samples:
            if value <= price:
                at, price = when, value
        return at, price


@dataclass
class Leg:
    """One trail of a paper trade: the highest bid since entry and, once out, the exit."""

    trail: Decimal
    high: Decimal
    exited: bool = False


@dataclass
class PaperTrade:
    trade_id: str
    symbol: str
    entry_at: datetime
    entry_price: Decimal  # the ask
    legs: list[Leg]
    last_quote: Quote | None = None
    counts: dict[str, int] = field(default_factory=lambda: {STREAM: 0, SNAPSHOT: 0})

    @property
    def main_open(self) -> bool:
        return not self.legs[0].exited

    @property
    def open(self) -> bool:
        return not all(leg.exited for leg in self.legs)


class IgnitionLog:
    """`<decisions_dir>/<environment>/<NY date>/ignition.jsonl`, one append per record."""

    def __init__(self, directory: Path) -> None:
        self.directory = directory

    def path(self, at: datetime) -> Path:
        return self.directory / ny_date_of(at).isoformat() / FILE_NAME

    def write(self, at: datetime, record: dict[str, Any]) -> None:
        path = self.path(at)
        path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(record, sort_keys=True, default=str) + "\n"
        fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, FILE_MODE)
        try:
            os.write(fd, line.encode("utf-8"))
        finally:
            os.close(fd)

    def read(self, at: datetime) -> list[dict[str, Any]]:
        path = self.path(at)
        if not path.exists():
            return []
        rows = []
        for line in path.read_text("utf-8").splitlines():
            try:
                rows.append(json.loads(line))
            except ValueError:
                continue  # a torn last line after a crash
        return rows


class Ignition:
    """The observer. Capture-sink calls (`screener`, `quotes`) and `after_pass` come from the
    discovery thread, `on_quote` from the stream thread, `watching` from the loop."""

    def __init__(self, rules: Rules, log: IgnitionLog) -> None:
        self.rules = rules
        self.log = log
        self._lock = threading.Lock()
        self._tapes: dict[str, Tape] = {}
        self._trades: dict[str, PaperTrade] = {}  # open (any leg), by symbol
        self._session: datetime | None = None  # the regular open the state belongs to
        self._flat_at: datetime | None = None
        self._rows: dict[str, ScreenerRow] = {}
        self._kinds: dict[str, set[str]] = {}
        self._row_time: datetime | None = None
        self._quotes: dict[str, Quote] = {}
        self._count = 0
        # Records made under the lock, written after it is released, so the stream thread
        # never waits on a file append (the writer lock keeps their order).
        self._out: list[tuple[datetime, dict[str, Any]]] = []
        self._write_lock = threading.Lock()

    def _flush(self) -> None:
        """Write the records queued under the state lock (called after releasing it)."""
        with self._write_lock:
            with self._lock:
                out, self._out = self._out, []
            for at, record in out:
                self.log.write(at, record)

    # -- the loop ----------------------------------------------------------------------------

    def watching(self) -> set[str]:
        """Symbols with an open paper trade: first in the stream's observer tier."""
        with self._lock:
            return set(self._trades)

    # -- the stream (QuoteListener) ----------------------------------------------------------

    def on_quote(self, quote: Quote) -> None:
        with self._lock:
            trade = self._trades.get(quote.symbol)
            if trade is None:
                return
            self._tick(trade, quote, STREAM)
            if not self._out:
                return
        self._flush()

    def on_notice(self, notice: StreamNotice) -> None:
        """Gaps between quotes are what the records keep."""

    def on_transport(self, up: bool, at: datetime, detail: str) -> None:
        """Gaps between quotes are what the records keep."""

    # -- discovery (capture sink, then the pass) ---------------------------------------------

    def screener(self, rows: Sequence[ScreenerRow], provider: str,  # noqa: ARG002
                 receipt_time: datetime) -> None:
        with self._lock:
            for row in rows:
                self._rows.setdefault(row.symbol, row)
                self._kinds.setdefault(row.symbol, set()).add(str(row.kind))
            self._row_time = receipt_time

    def bars(self, bars: Sequence[Bar], provider: str, receipt_time: datetime) -> None:
        """Bars are not used: the signal comes from the screener rows, the exit from quotes."""

    def quotes(self, quotes: Sequence[Quote], provider: str, source: str) -> None:  # noqa: ARG002
        with self._lock:
            for quote in quotes:
                self._quotes[quote.symbol] = quote

    def after_pass(self, day: MarketDay, universe: set[str], now: datetime) -> None:
        """This pass's signals (universe symbols only), its snapshot quotes for the open paper
        trades, and the flat time. Clears the pass's rows and quotes."""
        with self._lock:
            rows, quotes, at = self._rows, self._quotes, self._row_time or now
            kinds = self._kinds
            self._rows, self._quotes, self._row_time, self._kinds = {}, {}, None, {}
            if self._session != day.regular_open:
                self._new_day(day, now)
            for symbol, trade in list(self._trades.items()):
                quote = quotes.get(symbol)
                if quote is not None:
                    self._tick(trade, quote, SNAPSHOT)
            first = day.regular_open + timedelta(
                minutes=self.rules.first_signal_minutes_after_open)
            last = day.before_close(self.rules.last_signal_minutes_before_close)
            for symbol, row in rows.items():
                if symbol in universe:
                    self._sample(symbol, row, at, quotes.get(symbol), first <= at <= last,
                                 sorted(kinds.get(symbol, ())))
            assert self._flat_at is not None
            if now >= self._flat_at + self.rules.flat_grace:
                for trade in list(self._trades.values()):
                    self._flat_without_quote(trade, now)
        self._flush()

    # -- inside the lock ---------------------------------------------------------------------

    def _new_day(self, day: MarketDay, now: datetime) -> None:
        self._session = day.regular_open
        self._flat_at = day.before_close(self.rules.flat_minutes_before_close)
        self._tapes.clear()
        self._trades.clear()
        self._resume(now)

    def _sample(self, symbol: str, row: ScreenerRow, at: datetime, quote: Quote | None,
                in_window: bool, lists: list[str]) -> None:
        price = _field(row.fields, "price")
        volume = _field(row.fields, "volume")
        high = _field(row.fields, "high")
        if price is None or volume is None or high is None:
            return
        tape = self._tapes.setdefault(symbol, Tape())
        tape.add(at, price, volume, _field(row.fields, "low"), self.rules.low_window)
        low_at, low = tape.low()
        rise = price / low - 1
        if not (in_window and price >= high and rise >= self.rules.min_rise
                and at - low_at < self.rules.low_max_age
                and tape.dollar_volume < self.rules.max_dollar_volume):
            return
        record: dict[str, Any] = {
            "record": SIGNAL, "strategy": self.rules.name, "version": self.rules.version,
            "symbol": symbol, "at": at.isoformat(), "price": str(price), "high": str(high),
            "low": str(low), "low_at": low_at.isoformat(),
            "minutes_since_low": int((at - low_at).total_seconds()) // SECONDS_PER_MINUTE,
            "rise_percent": str((rise * PERCENT).quantize(CENTS)),
            "dollar_volume_usd": str(tape.dollar_volume.quantize(Decimal(1))),
            "volume": str(volume), "lists": lists,
            "bid": None if quote is None else str(quote.bid),
            "ask": None if quote is None else str(quote.ask),
            "bid_size": None if quote is None else str(quote.bid_size),
            "ask_size": None if quote is None else str(quote.ask_size)}
        earlier = self._trades.get(symbol)
        if earlier is not None and earlier.main_open:
            record["outcome"] = DIED_OPEN
        elif quote is None or not _valid(quote):
            record["outcome"] = DIED_NO_QUOTE
        else:
            self._count += 1
            trade = PaperTrade(f"{ny_date_of(at).isoformat()}-{symbol}-{self._count}", symbol,
                               quote.provider_time, quote.ask,
                               [Leg(t, quote.bid) for t in self.rules.trails], quote)
            trade.counts[SNAPSHOT] += 1
            if earlier is not None:  # its variants stop here; the new trade takes the slot
                for leg in earlier.legs:
                    if not leg.exited:
                        self._exit(earlier, leg, quote, at, REPLACED)
            self._trades[symbol] = trade
            # Slot caps are judged later from this: open paper trades counting this one (any
            # trail still out), and those whose main trail is still out.
            record.update(open_paper_trades=len(self._trades),
                          open_main_trails=sum(t.main_open for t in self._trades.values()))
            record.update(outcome=ENTERED, trade_id=trade.trade_id, entry_price=str(quote.ask),
                          entry_at=quote.provider_time.isoformat(),
                          spread_percent=str(spread_percent(quote).quantize(CENTS)))
        self._out.append((at, record))

    def _tick(self, trade: PaperTrade, quote: Quote, source: str) -> None:
        last = trade.last_quote
        if not _valid(quote) or (last is not None and quote.provider_time <= last.provider_time):
            return
        trade.counts[source] += 1
        if last is not None and quote.provider_time - last.provider_time >= self.rules.quote_gap:
            self._out.append((quote.provider_time, {
                "record": GAP, "trade_id": trade.trade_id, "symbol": trade.symbol,
                "after": last.provider_time.isoformat(), "next": quote.provider_time.isoformat(),
                "seconds": int((quote.provider_time - last.provider_time).total_seconds()),
                "source": source}))
        trade.last_quote = quote
        assert self._flat_at is not None
        for leg in trade.legs:
            if leg.exited:
                continue
            if quote.provider_time >= self._flat_at:
                self._exit(trade, leg, quote, quote.provider_time, FLAT)
            elif quote.bid <= leg.high * (1 - leg.trail):
                self._exit(trade, leg, quote, quote.provider_time, TRAIL)
            else:
                leg.high = max(leg.high, quote.bid)
        if not trade.open:
            self._trades.pop(trade.symbol, None)

    def _flat_without_quote(self, trade: PaperTrade, now: datetime) -> None:
        """No quote at or after the flat time: the last bid seen, or no result at all when
        none was seen since a restart (refuse, never guess)."""
        for leg in trade.legs:
            if not leg.exited:
                self._exit(trade, leg, trade.last_quote, now,
                           FLAT_LAST_BID if trade.last_quote is not None else FLAT_NO_QUOTE)
        self._trades.pop(trade.symbol, None)

    def _exit(self, trade: PaperTrade, leg: Leg, quote: Quote | None, at: datetime,
              cause: str) -> None:
        leg.exited = True
        record: dict[str, Any] = {
            "record": EXIT, "strategy": self.rules.name, "version": self.rules.version,
            "trade_id": trade.trade_id, "symbol": trade.symbol,
            "trail_percent": percent_text(leg.trail), "main": leg is trade.legs[0],
            "cause": cause, "entry_price": str(trade.entry_price),
            "entry_at": trade.entry_at.isoformat(), "exit_at": at.isoformat(),
            "high_bid": str(leg.high), "quotes": dict(trade.counts)}
        if quote is not None:
            result = quote.bid / trade.entry_price - 1
            record.update(
                exit_bid=str(quote.bid), exit_ask=str(quote.ask),
                quote_at=quote.provider_time.isoformat(),
                spread_percent=str(spread_percent(quote).quantize(CENTS)),
                result_percent=str((result * PERCENT).quantize(CENTS)),
                result_usd=str((result * self.rules.notional).quantize(CENTS)))
        self._out.append((at, record))

    def _resume(self, now: datetime) -> None:
        """Open paper trades of today's file (entered, a trail not exited yet). The highest
        bid starts again from the entry quote's bid."""
        entered: dict[str, dict[str, Any]] = {}
        exited: dict[str, set[Decimal]] = {}
        for row in self.log.read(now):
            if row.get("record") == SIGNAL and row.get("outcome") == ENTERED:
                entered[row["trade_id"]] = row
                self._count += 1
            elif row.get("record") == EXIT:
                exited.setdefault(row["trade_id"], set()).add(
                    Decimal(row["trail_percent"]) / PERCENT)
        for trade_id, row in entered.items():
            done = exited.get(trade_id, set())
            bid = Decimal(row["bid"])
            trade = PaperTrade(trade_id, row["symbol"], datetime.fromisoformat(row["entry_at"]),
                               Decimal(row["entry_price"]),
                               [Leg(t, bid, t in done) for t in self.rules.trails])
            if trade.open:
                self._trades[trade.symbol] = trade


def from_config(config: Config) -> Ignition | None:
    """The observer `[observers] ignition` names (None when ""), or IgnitionError."""
    folder = config.observers.ignition
    if not folder:
        return None
    path = config.root / "strategies" / folder / "strategy.toml"
    try:
        with path.open("rb") as handle:
            values = tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise IgnitionError(f"{path}: {exc}") from None
    rules = Rules.from_values(values)
    if rules.name != folder:
        raise IgnitionError(f"[strategy] name {rules.name!r} is not the folder {folder!r}")
    base = config.root / config.run.data_dir / config.observers.decisions_dir
    return Ignition(rules, IgnitionLog(base / config.environment_name))
