"""Limit-exit chase before a market exit (backlog X3): the prices and the end rule.

A close is sent as a SELL LIMIT `[exit_chase] ticks_through_bid` ticks under the bid. While it
works it is re-priced every `reprice_seconds`: cancelled, and only once a Detail read shows
the cancel took (or the order finished) is the next LIMIT placed at the new bid for what is
still held. Webull's replace (F5, docs/webull-facts.md) is unproven, so it is never used here.

The chase ends when the next price would be below the floor (the bid when the chase started
less `max_worse_atr` x ATR), after `1 + max_reprices` orders, or without a fresh quote. Then
the regular session gets one MARKET for the rest (the close as before); outside it no MARKET
is ever sent: the operator is alerted and the stop rules stay (`ChaseEnd.market` False).

Resting stops: Webull's behaviour for a closing SELL while a stop reserves the shares is
unproven (docs/webull-facts.md, probe list). The chase never meets it: it starts only when the
trade has no live stop (the teardown cancels a stop and waits for Detail before its shares are
sold) and no other close working; otherwise it refuses (`STOP_RESTING`, `CLOSE_WORKING`) and
the trade closes by MARKET as before. The probe that would lift this: in the regular session,
with a STOP_LOSS SELL for N shares working on a position of N, place a SELL LIMIT for N
(refused, which code, or accepted), then for a position of N + M a SELL LIMIT for M.

This module only prices and decides; the engine records and sends (tickr.engine.core)."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_CEILING, Decimal
from enum import StrEnum

from tickr.config import ExitChaseConfig
from tickr.risk.ticks import tick

# Journal rows, keyed by trade id: the chase started (reference bid, ATR, floor), one per
# chase order (its price and when it was decided, engine clock), refused before it started,
# and how it ended.
CHASE_STARTED = "exit-chase-started"
CHASE_PRICE = "exit-chase-price"
CHASE_REFUSED = "exit-chase-refused"
CHASE_ENDED = "exit-chase-ended"


@dataclass(frozen=True)
class ExitQuote:
    """What the chase prices from: the best bid, the symbol's ATR (USD per share) and how
    old the quote is, in seconds."""

    bid: Decimal
    atr: Decimal
    age_seconds: Decimal


class Refusal(StrEnum):
    """Why a close goes out as MARKET instead of starting a chase (recorded once per trade)."""

    STOP_RESTING = "stop-resting"  # a stop of the trade still works: unproven at Webull
    CLOSE_WORKING = "close-working"  # another close of the trade is already working
    NO_QUOTE = "no-quote"  # no quote or ATR for the symbol
    STALE_QUOTE = "stale-quote"  # older than [risk] max_quote_age_seconds
    BAD_QUOTE = "bad-quote"  # bid or ATR not above 0


class EndCause(StrEnum):
    CAP_REACHED = "cap-reached"  # 1 + max_reprices orders were sent
    FLOOR = "floor"  # the next price would be below the floor
    NO_QUOTE = "no-quote"
    STALE_QUOTE = "stale-quote"
    BAD_QUOTE = "bad-quote"


@dataclass(frozen=True)
class Start:
    reference: Decimal  # the bid when the chase started
    atr: Decimal
    floor: Decimal  # the worst price the chase may send


@dataclass(frozen=True)
class Price:
    limit: Decimal
    bid: Decimal


@dataclass(frozen=True)
class End:
    cause: EndCause
    market: bool  # True: one MARKET for the rest; False: alert, no MARKET (outside the session)


def _quote_refusal(quote: ExitQuote | None, max_age: Decimal) -> Refusal | None:
    if quote is None:
        return Refusal.NO_QUOTE
    if quote.bid <= 0 or quote.atr <= 0 or quote.age_seconds < 0:
        return Refusal.BAD_QUOTE
    if quote.age_seconds > max_age:
        return Refusal.STALE_QUOTE
    return None


def limit_price(bid: Decimal, ticks_through: int) -> Decimal:
    """`ticks_through` ticks under `bid`, on the tick grid (a SELL limit rounds up)."""
    price = bid - tick(bid) * ticks_through
    return price.quantize(tick(price), rounding=ROUND_CEILING)


def start(quote: ExitQuote | None, *, stops_live: bool, closes_live: bool,
          max_quote_age: Decimal, settings: ExitChaseConfig) -> Start | Refusal:
    """Whether the chase may start for a close now, and its floor."""
    if stops_live:
        return Refusal.STOP_RESTING
    if closes_live:
        return Refusal.CLOSE_WORKING
    refusal = _quote_refusal(quote, max_quote_age)
    if refusal is not None or quote is None:
        return refusal or Refusal.NO_QUOTE
    return Start(quote.bid, quote.atr, quote.bid - quote.atr * settings.max_worse_atr)


def next_order(begun: Start, sent: int, quote: ExitQuote | None, *, max_quote_age: Decimal,
               market_allowed: bool, settings: ExitChaseConfig) -> Price | End:
    """The next chase order after `sent` orders, or how the chase ends. `market_allowed`:
    a MARKET close may be sent now (the regular session); never a MARKET otherwise."""
    if sent >= 1 + settings.max_reprices:
        return End(EndCause.CAP_REACHED, market_allowed)
    refusal = _quote_refusal(quote, max_quote_age)
    if refusal is not None or quote is None:
        return End(EndCause((refusal or Refusal.NO_QUOTE).value), market_allowed)
    price = limit_price(quote.bid, settings.ticks_through_bid)
    if price < begun.floor or price <= 0:
        return End(EndCause.FLOOR, market_allowed)
    return Price(price, quote.bid)
