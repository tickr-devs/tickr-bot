"""Probe T: does Webull's TRAILING_STOP_LOSS work as a broker-side stop for the bot.

`tickr probe-trailing SYMBOL`, run by the owner on the Mac in the regular session, inside the
`[probe] window_start`-`window_end` window (engine.manual.check_window), with the bot stopped
and the account flat (docs/manual-trade.md, "Trailing-stop probe"). Several positions
(backlog X1) want a trailing broker stop (rollout guide: 20 %); nothing in the bot sends one
until this probe answered (docs/webull-facts.md, T1-T6). The order body is the SDK v3
sample's: `trailing_type` PERCENTAGE, `trailing_stop_step`, `trigger_price_type` PRICE,
DAY, session CORE.

1. Entry: a BUY LIMIT DAY CORE for `[probe] quantity` shares at the ask. Unfilled within
   `[probe] entry_wait_seconds`, it is cancelled and the probe ends. While it waits, Detail
   and the positions are read each poll (P1: how long after Detail shows a fill the positions
   read shows it; the bot's Detail priority for a moved symbol relies on it).
2. Wide trailing stop (T1 accepted, T2 unit, T3 follows the price, T4 open orders, T5 cancel):
   a SELL TRAILING_STOP_LOSS for the held quantity with `trailing_stop_step` =
   `[probe] trailing_wide_percent` / 100 (the SDK sample's ratio form). Detail is read every
   poll for `[probe] trailing_observe_seconds` with a quote each time: the stop price Detail
   shows, compared with the bid, says which unit the step is in (T2: the distance is the
   ratio, or it is the percent, or Detail shows no stop price) and whether the stop rises
   with the price (T3). One open-orders read says whether it is listed (T4). Then it is
   cancelled and must show CANCELLED (T5); a trigger meanwhile is recorded as such.
3. Tight trailing stop (T6 triggers): only when T2 found the unit, a SELL TRAILING_STOP_LOSS
   with `[probe] trailing_tight_percent` in that unit, watched for
   `[probe] trailing_trigger_seconds`; still working afterwards, it is cancelled.
4. Close: whatever is still held is sold with a LIMIT DAY CORE `[probe] trailing_close_percent`
   below the bid, watched for `[orders] max_exit_seconds`. Not filled: the probe says so loudly
   and exits 4; the owner closes it in the Webull app.

Hard rule 2: the shares are protected by the trailing stop under test, and between the fill and
that stop, between the two stops, and after a stop that did not trigger, the probe holds
`[probe] quantity` share(s) without a broker stop for at most a few minutes; proving whether a
trailing stop protects is what the probe is for. It is a sender besides the engine
(tests/test_invariants.py names it). Hard rule 3: every order is written to the evidence file
before its one request, never resent; an unknown outcome is read back by Detail (the plumbing
of engine/session_probe.py). The probe holds the bot's lock and uses the probe order prefix.
Evidence: `<data_dir>/<[probe] evidence_dir>/<NY date>/trailing-<UTC time>/steps.jsonl` and a
`summary.json` with one verdict per question.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from tickr.broker.base import Broker, BrokerError
from tickr.config import Config
from tickr.data.base import MarketData, MarketDataError
from tickr.engine.manual import ManualRefused
from tickr.engine.replace_probe import read_quote, stale_cause
from tickr.engine.session_probe import (
    ACCEPTED,
    CLOSED,
    EXIT_ANSWERED,
    EXIT_OPERATOR,
    FILLED,
    NOT_CLOSED,
    NOT_FILLED,
    NOT_SENT,
    NOT_SENT_UNRESOLVED,
    NOT_TESTED,
    PERCENT,
    REFUSED,
    UNKNOWN,
    Clock,
    Evidence,
    Out,
    Probe,
    Sleep,
    Unresolved,
    floor_tick,
    quote_row,
    require_flat,
    tick,
)
from tickr.types import OrderDetail, OrderState, OrderType, Quote, Side, TradingSession

CORE = TradingSession.CORE

# T2: which unit Webull reads `trailing_stop_step` in, from the stop price Detail shows.
UNIT_RATIO = "ratio"  # 0.2 = 20 %
UNIT_PERCENT = "percent"  # 0.2 = 0.2 %
UNIT_NOT_SHOWN = "detail-shows-no-stop-price"
UNIT_UNCLEAR = "stop-price-fits-neither-unit"
# T3: whether the stop price rose with the price.
FOLLOWED = "stop-rose-with-the-price"
NOT_FOLLOWED = "price-rose-but-stop-did-not"
PRICE_DID_NOT_RISE = "price-did-not-rise-not-answered"
# T4.
LISTED = "listed"
NOT_LISTED = "not-listed"
# T5 / T6.
CANCELLED = "cancelled"
TRIGGERED = "triggered"
NOT_TRIGGERED = "not-triggered"

TRIGGER_STATES = (OrderState.FILLED, OrderState.PARTIALLY_FILLED)


def unit_of(step: Decimal, bid: Decimal, stop: Decimal | None, factor: Decimal) -> str:
    """Which unit puts `stop` where Detail shows it below `bid`. The two candidate distances
    (step x price, or step % of price) differ a hundredfold, and a trailing stop follows the
    high since it was placed rather than this poll's bid, so cents do not decide: the
    candidate whose distance is within `factor` of the measured one, nearer in ratio terms;
    neither within it = unclear."""
    if stop is None:
        return UNIT_NOT_SHOWN
    measured = bid - stop
    if measured <= 0:
        return UNIT_UNCLEAR
    fits = []
    for unit, candidate in ((UNIT_RATIO, bid * step), (UNIT_PERCENT, bid * step / PERCENT)):
        ratio = max(measured, candidate) / min(measured, candidate)
        if ratio <= factor:
            fits.append((ratio, unit))
    return min(fits)[1] if fits else UNIT_UNCLEAR


@dataclass
class TrailingProbe(Probe):
    """The plumbing of the extended-hours probe (record, send once, Detail, cancel and
    prove), in the regular session: orders go to session CORE and quotes must be as fresh
    as the bot's own entry rule."""

    order_session: TradingSession = CORE

    # P1: seconds from Detail first showing the entry fill to the positions read first showing
    # it (poll-granular, may be negative); None when the positions read never showed it.
    lag: float | None = None
    # The field names (never values) of the first trailing row Detail showed, logged once.
    _keys_logged: bool = False

    def quote(self) -> Quote:
        quote = read_quote(self.market, self.symbol)
        age, stale = stale_cause(self.config, quote, self.now())
        self._log("quote", age_seconds=age, stale=stale, **quote_row(quote))
        if stale is not None:
            raise ManualRefused(stale)
        if not 0 < quote.bid <= quote.ask:
            raise ManualRefused(f"no valid quote: bid {quote.bid} ask {quote.ask}")
        return quote

    def _bid(self, name: str) -> Decimal | None:
        try:
            quote = read_quote(self.market, self.symbol)
        except (ManualRefused, BrokerError, MarketDataError) as exc:
            self._log("quote-failed", case=name, error=str(exc))
            return None
        self._log("quote", case=name, **quote_row(quote))
        return quote.bid

    def entry(self, quote: Quote) -> str:
        cid = self._cid("entry")
        price = floor_tick(quote.ask) if quote.ask == floor_tick(quote.ask) \
            else floor_tick(quote.ask) + tick(quote.ask)
        if price > self.config.probe.max_price_usd:
            raise ManualRefused(f"ask {quote.ask} is above [probe] max_price_usd")
        sent = self.place("entry", self.request(cid, Side.BUY, OrderType.LIMIT,
                                                Decimal(self.config.probe.quantity),
                                                session=CORE, limit=price))
        if sent in (REFUSED, NOT_SENT):
            return sent
        row, absent = self._entry_and_lag(cid)
        row = self.finish("entry", cid, row, absent)
        verdict = FILLED if row is not None and row.filled_quantity > 0 else NOT_FILLED
        self.out(f"entry (BUY LIMIT CORE at {price}): {verdict}")
        return verdict

    def _entry_and_lag(self, cid: str) -> tuple[OrderDetail | None, bool]:
        """`settle` for the entry, plus a positions read each poll (P1): when Detail first
        showed the fill and when the positions read first did, so `positions_lag_seconds`
        (positions minus Detail, poll-granular) is measured; the bot's Detail priority for a
        moved symbol relies on the positions read showing a fill."""
        poll = float(self.config.orders.detail_poll_seconds)
        row: OrderDetail | None = None
        absent = 0
        detail_at: datetime | None = None
        position_at: datetime | None = None
        polls = max(1, int(self.config.probe.entry_wait_seconds // poll))
        for n in range(polls + int(self.config.probe.extended_position_agree_seconds // poll)):
            self.sleep(poll)
            if detail_at is None:
                read = self.detail("entry", cid)
                absent = absent + 1 if read is False else 0
                if isinstance(read, OrderDetail):
                    row = read
                    if row.filled_quantity > 0:
                        detail_at = self.now()
            if position_at is None:
                try:
                    held = self._position()
                except Unresolved:
                    held = Decimal(0)
                if held > 0:
                    position_at = self.now()
            if detail_at is not None and position_at is not None:
                break
            if detail_at is None and n + 1 >= polls:
                break  # not filled in time: finish cancels it
            if row is not None and row.state.terminal and detail_at is None:
                break
        if detail_at is not None:
            self.lag = (None if position_at is None
                        else (position_at - detail_at).total_seconds())
            self._log("positions-lag", detail_fill_at=detail_at.isoformat(),
                      position_at=None if position_at is None else position_at.isoformat(),
                      seconds=self.lag)
        return row, row is None and absent >= self.config.orders.unknown_absent_reads

    def _send_trailing(self, name: str, step: Decimal) -> tuple[str, str | None]:
        held = self.held()
        if held <= 0:
            return NOT_TESTED, None
        cid = self._cid(name)
        sent = self.place(name, self.request(cid, Side.SELL, OrderType.TRAILING_STOP_LOSS, held,
                                             session=CORE, trailing_step=step))
        return sent, (None if sent in (REFUSED, NOT_SENT) else cid)

    def _watch(self, name: str, cid: str, seconds: float,
               ) -> tuple[OrderDetail | None, int, list[tuple[Decimal, Decimal]]]:
        """Detail and a quote every poll for `seconds`, stopping at a terminal row. Returns
        the last row, the run of "not present" answers at the end, and (bid, stop price) of
        every poll where both were read."""
        row: OrderDetail | None = None
        absent = 0
        seen: list[tuple[Decimal, Decimal]] = []
        polls = max(1, int(seconds // float(self.config.orders.detail_poll_seconds)))
        for _ in range(polls):
            self.sleep(float(self.config.orders.detail_poll_seconds))
            read = self.detail(name, cid)
            absent = absent + 1 if read is False else 0
            if isinstance(read, OrderDetail):
                row = read
                if not self._keys_logged and read.row_keys is not None:
                    self._keys_logged = True
                    self._log("trailing-row-fields", case=name, fields=list(read.row_keys))
            bid = self._bid(name)
            if bid is not None and row is not None and row.stop_price is not None:
                seen.append((bid, row.stop_price))
            if row is not None and row.state.terminal:
                break
        return row, absent, seen

    def _listed(self, cid: str) -> str:
        try:
            ids = [o.client_order_id for o in self.broker.open_orders()]
        except BrokerError as exc:
            self._log("open-orders-failed", error=str(exc))
            return UNKNOWN
        verdict = LISTED if cid in ids else NOT_LISTED
        self._log("open-orders", client_order_id=cid, verdict=verdict)
        return verdict

    def wide(self) -> dict[str, str]:
        """Steps T1-T5 with the wide stop; the answers keyed by question."""
        name = "trailing-wide"
        step = self.config.probe.trailing_wide_percent / PERCENT
        sent, cid = self._send_trailing(name, step)
        answers = {"t1_accepted": sent, "t2_unit": NOT_TESTED, "t3_follows": NOT_TESTED,
                   "t4_open_orders": NOT_TESTED, "t5_cancel": NOT_TESTED}
        if cid is None:
            return answers
        row, absent, seen = self._watch(name, cid,
                                        float(self.config.probe.trailing_observe_seconds))
        answers["t4_open_orders"] = self._listed(cid) if row is not None else NOT_TESTED
        if row is None:
            answers["t1_accepted"] = UNKNOWN if sent == UNKNOWN else "acknowledged-not-in-detail"
        elif row.state is OrderState.ACCEPTED or row.state in TRIGGER_STATES:
            answers["t1_accepted"] = f"{ACCEPTED} ({row.raw_status})"
        else:
            answers["t1_accepted"] = f"{REFUSED} ({row.raw_status})"
        if seen:
            bid, stop = seen[0]
            answers["t2_unit"] = unit_of(step, bid, stop, self.config.probe.trailing_unit_factor)
            rose = max(b for b, _ in seen) > bid
            answers["t3_follows"] = (FOLLOWED if max(s for _, s in seen) > stop
                                     else NOT_FOLLOWED if rose else PRICE_DID_NOT_RISE)
        elif row is not None:
            answers["t2_unit"] = UNIT_NOT_SHOWN
        triggered = row is not None and row.state in TRIGGER_STATES
        row = self.finish(name, cid, row,
                          row is None and absent >= self.config.orders.unknown_absent_reads)
        if triggered or (row is not None and row.filled_quantity > 0):
            answers["t5_cancel"] = f"{NOT_TESTED} ({TRIGGERED} first)"
            fill = None if row is None else row.average_fill_price
            answers["t6_triggers"] = f"{TRIGGERED} (the wide stop itself, fill {fill})"
        elif row is not None and row.state is OrderState.CANCELLED:
            answers["t5_cancel"] = CANCELLED
        else:
            answers["t5_cancel"] = UNKNOWN
        for key, value in answers.items():
            self.out(f"{key}: {value}")
        return answers

    def tight(self, unit: str) -> str:
        """T6: with the unit known, a tight trailing stop is watched for a trigger."""
        if unit not in (UNIT_RATIO, UNIT_PERCENT):
            return NOT_TESTED
        percent = self.config.probe.trailing_tight_percent
        step = percent / PERCENT if unit == UNIT_RATIO else percent
        name = "trailing-tight"
        sent, cid = self._send_trailing(name, step)
        if cid is None:
            return sent
        row, absent, _ = self._watch(name, cid,
                                     float(self.config.probe.trailing_trigger_seconds))
        row = self.finish(name, cid, row,
                          row is None and absent >= self.config.orders.unknown_absent_reads)
        if row is not None and row.filled_quantity > 0:
            verdict = f"{TRIGGERED} (fill {row.average_fill_price})"
        elif row is None:
            verdict = UNKNOWN
        else:
            verdict = NOT_TRIGGERED
        self.out(f"t6_triggers: {verdict}")
        return verdict

    def close(self) -> str:
        held = self.held()
        if held <= 0:
            return CLOSED
        try:
            quote = self.quote()
        except (ManualRefused, BrokerError, MarketDataError) as exc:
            self._log("close-not-sent", error=str(exc))
            return NOT_CLOSED
        price = floor_tick(quote.bid * (1 - self.config.probe.trailing_close_percent / PERCENT))
        cid = self._cid("close")
        request = self.request(cid, Side.SELL, OrderType.LIMIT, held, session=CORE,
                               limit=price)
        if self.place("close", request) in (REFUSED, NOT_SENT):
            return NOT_CLOSED
        row, absent = self.settle("close", cid, float(self.config.orders.max_exit_seconds))
        self.finish("close", cid, row, absent)
        return CLOSED if self.held() <= 0 else NOT_CLOSED


def run(config: Config, broker: Broker, market: MarketData, symbol: str, folder: Path, *,
        now: Clock, sleep: Sleep, out: Out) -> int:
    """The four steps; returns the exit code. The caller holds the bot's lock. An order not
    proven finished stops the run before any further order, the close included; the final
    broker read always runs and decides the exit code."""
    started = now()
    probe = TrailingProbe(config, broker, market, symbol, Evidence(folder), now, sleep, out,
                          run_tag=f"{started.astimezone(UTC):%H%M%S}")
    summary: dict[str, Any] = {"probe": "trailing", "environment": config.environment_name,
                               "symbol": symbol, "started_at": started.isoformat()}
    unresolved: str | None = None
    try:
        try:
            require_flat(broker)
            quote = probe.quote()
            if quote.ask > config.probe.max_price_usd:
                raise ManualRefused(f"ask {quote.ask} is above [probe] max_price_usd")
            summary["entry"] = probe.entry(quote)
            summary["p1_positions_lag_seconds"] = probe.lag
            summary["p1_note"] = (
                f"poll-granular ({config.orders.detail_poll_seconds} s): each poll reads "
                "Detail, then the positions, so 0.0 means within one poll; null = the "
                "positions never showed the fill")
            if summary["entry"] == FILLED:
                answers = probe.wide()
                summary.update(answers)
                if "t6_triggers" not in answers:
                    summary["t6_triggers"] = probe.tight(answers["t2_unit"])
        except ManualRefused as exc:
            summary["stopped"] = str(exc)
            out(f"stopped: {exc}")
        summary["close"] = probe.close()
    except Unresolved as exc:
        unresolved = str(exc)
        summary["unresolved"] = unresolved
        summary.setdefault("close", NOT_SENT_UNRESOLVED)
        out(f"stopped, nothing more sent: {exc}")
    except BrokerError as exc:
        summary["error"] = f"{type(exc).__name__}: {exc}"
        summary.setdefault("close", NOT_CLOSED)
    finally:
        summary["broker_after"] = after = probe.leftovers()
        summary["finished_at"] = now().isoformat()
        probe.evidence.summary(summary)
    clean = (after["readable"] and after["position"] == 0 and not after["working_probe_orders"])
    if unresolved is not None or summary.get("close") != CLOSED or not clean:
        first = f"{unresolved}; " if unresolved is not None else ""
        out(f"OPERATOR: {first}the broker shows {after}: cancel the probe orders first, then "
            f"sell what is held in the Webull app now")
        return EXIT_OPERATOR
    return EXIT_ANSWERED
