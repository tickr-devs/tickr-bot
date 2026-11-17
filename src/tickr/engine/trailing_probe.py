"""Probe T: does Webull's TRAILING_STOP_LOSS work as a broker-side stop for the bot.

`tickr probe-trailing SYMBOL`, run by the owner on the Mac in the regular session, inside the
`[probe] window_start`-`window_end` window (engine.manual.check_window), with the bot stopped
and the account flat (docs/manual-trade.md, "Trailing-stop probe"). Several positions
(backlog X1) want a trailing broker stop (rollout guide: 20 %); nothing in the bot sends one
until this probe answered (docs/webull-facts.md, T1-T6). The order body is the SDK v3
sample's: `trailing_type` PERCENTAGE, `trailing_stop_step`, `trigger_price_type` PRICE,
DAY, session CORE.

0. Listing of stops placed together (L2, before the entry, account flat):
   `[probe] listing_stops` BUY STOP_LOSS DAY CORE orders of `[probe] quantity` share(s), each
   `[probe] listing_far_percent` above the ask so none triggers (a buy stop needs no shares, so
   the probe's one share is not over-reserved), each one tick above the one before (Webull
   refuses an order identical to a working one, 417 OPENAPI_ORDER_RISK_RULE_DUPLICATE_ORDER_CHECK,
   2026-11-17), sent back to back in one poll. The open orders
   are read each poll for `[probe] listing_wait_seconds`: per stop, seconds from its place
   answer to the first read listing it. Then each is cancelled and must show CANCELLED; the
   bot's bulk stop check and its fills-together bound rely on this delay.
   D1 (same step): two more far BUY stops at one price, of `[probe] quantity` and twice that:
   does Webull accept a stop that differs from a working one only in quantity (the bot's stops
   for one trade share a price; equal quantities at one price are refused, see above). Both
   are then cancelled and proven over.
1. Entry: a BUY LIMIT DAY CORE for `[probe] quantity` shares at the ask. Unfilled within
   `[probe] entry_wait_seconds`, it is cancelled and the probe ends. While it waits, Detail
   and the positions are read each poll (P1: how long after Detail shows a fill the positions
   read shows it; the bot's Detail priority for a moved symbol relies on it).
   H1, how long after Detail shows a fill the order history shows it with the same filled
   quantity (one history read for all orders could replace a Detail read per fill when
   several entries fill together), is measured only after the trailing stop is placed, during
   its watch, so it never delays the stop.
2. Wide trailing stop (T1 accepted, T2 unit, T3 follows the price, T4 open orders, T5 cancel):
   a SELL TRAILING_STOP_LOSS for the held quantity with `trailing_stop_step` =
   `[probe] trailing_wide_percent` / 100 (the SDK sample's ratio form). Detail is read every
   poll for `[probe] trailing_observe_seconds` with a quote each time: the stop price Detail
   shows, compared with the bid, says which unit the step is in (T2: the distance is the
   ratio, or it is the percent, or Detail shows no stop price) and whether the stop rises
   with the price (T3). The open orders are read each poll until they list it (T4, and L1:
   seconds from the place answer to the first listing; the bot verifies new stops from that
   list). Then it is cancelled and must show CANCELLED (T5); a trigger meanwhile is recorded
   as such.
2b. Amount trailing stop (A1 accepted, A2 the stop lies `trailing_stop_step` USD under the
   price): a SELL TRAILING_STOP_LOSS with `trailing_type` AMOUNT and a step of the bid times
   `[probe] trailing_wide_percent` in USD, watched like the wide one, then cancelled.
3. Tight trailing stop (T6 triggers; T7 the smallest percent Webull accepts): only when T2
   found the unit, `[probe] trailing_tight_percents` in that unit, smallest first, until one
   is accepted (0.5 % sent as 0.005 was refused with OPENAPI_PARAM_ERR on 2026-11-17); that
   one is watched for `[probe] trailing_trigger_seconds`, and cancelled if still working.
3b. Smallest amount (A3): if a share is still held, `[probe] trailing_amount_steps_usd`, smallest
   first, until one is accepted; it is cancelled at once (a trigger meanwhile is recorded).
4. Close: whatever is still held is sold with a LIMIT DAY CORE `[probe] trailing_close_percent`
   below the bid, watched for `[orders] max_exit_seconds`; once Detail shows it filled, the
   history is read each poll for H1 on a sell. Not filled: the probe says so loudly and exits
   4; the owner closes it in the Webull app.

The history and open-orders reads only read: they never decide an order or a verdict above.

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

from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from tickr.broker.base import Broker, BrokerError
from tickr.calendar import ny_date_of
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
from tickr.types import (
    OrderDetail,
    OrderState,
    OrderType,
    Quote,
    Side,
    TradingSession,
    TrailingType,
)

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
CENT = Decimal("0.01")  # USD cents: the AMOUNT step's precision


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


def follows(seen: list[tuple[Decimal, Decimal]]) -> str:
    """T3 from (bid, stop price) per poll: did the stop rise when the bid rose above where it
    was at the first read."""
    bid, stop = seen[0]
    if max(s for _, s in seen) > stop:
        return FOLLOWED
    return NOT_FOLLOWED if max(b for b, _ in seen) > bid else PRICE_DID_NOT_RISE


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
    # H1: per filled order ("entry", "close"), seconds from Detail first showing the fill to
    # the history first showing the same filled quantity (poll-granular); None = never shown.
    history_lag: dict[str, float | None] | None = None
    # L1: seconds from the wide stop's place answer to the first open-orders read listing it;
    # None = not listed while it was watched.
    listed_after: float | None = None
    # L2: per stop placed together, seconds from its place answer to the first open-orders
    # read listing it; None = not listed within `[probe] listing_wait_seconds`.
    listed_together: list[float | None] | None = None
    # L2: the listing stops that filled (should be none: they lie far above the ask).
    listing_filled: list[str] | None = None
    # D1: the same-price stops placed, until each is finished.
    _same_open: list[tuple[str, str, OrderDetail | None, bool]] = field(default_factory=list)
    # H1 for the entry, until measured: (client order id, filled quantity, when Detail showed it).
    _entry_pending: tuple[str, Decimal, datetime] | None = None

    def _entry_history(self, *, final: bool = False) -> None:
        """One step of H1 for the entry, called each poll of the trailing stop's watch (after
        the stop is placed, so it never delays it): one history read while the entry's fill
        is not yet shown and `[probe] history_wait_seconds` since Detail showed it have not
        passed; past that wait, or not shown at the `final` call, it is recorded "not shown"."""
        if self._entry_pending is None:
            return
        cid, filled, detail_at = self._entry_pending
        waited = (self.now() - detail_at).total_seconds()
        if waited > self.config.probe.history_wait_seconds:
            self._record_history_lag("entry", detail_at, None)
        elif self._history_shows("entry", cid, filled):
            self._record_history_lag("entry", detail_at, self.now())
        elif final:
            self._record_history_lag("entry", detail_at, None)
        else:
            return
        self._entry_pending = None

    def listing(self, quote: Quote) -> str:
        """Step 0 (L2): several far BUY stops sent together, the open orders read each poll
        until all are listed, then every one cancelled and proven over."""
        probe = self.config.probe
        stop = floor_tick(quote.ask * (1 + probe.listing_far_percent / PERCENT))
        stop += tick(stop)
        placed: list[tuple[str, datetime]] = []
        for n in range(probe.listing_stops):
            cid = self._cid("listing")
            sent = self.place(f"listing-{n}", self.request(
                cid, Side.BUY, OrderType.STOP_LOSS, Decimal(probe.quantity), session=CORE,
                stop=stop + n * tick(stop)))
            if sent in (REFUSED, NOT_SENT):
                break
            placed.append((cid, self.now()))
        listed: dict[str, datetime] = {}
        poll = float(self.config.orders.detail_poll_seconds)
        for _ in range(max(1, int(probe.listing_wait_seconds // poll))):
            if len(listed) == len(placed):
                break
            self.sleep(poll)
            try:
                ids = {o.client_order_id for o in self.broker.open_orders()}
            except BrokerError as exc:
                self._log("open-orders-failed", case="listing", error=str(exc))
                continue
            for cid, _ in placed:
                if cid in ids and cid not in listed:
                    listed[cid] = self.now()
            self._log("open-orders", case="listing", listed=sorted(listed), of=len(placed))
        self.listed_together = [
            (listed[cid] - at).total_seconds() if cid in listed else None
            for cid, at in placed]
        self.listing_filled = []
        for n, (cid, _) in enumerate(placed):
            row, absent = self.settle(f"listing-{n}", cid, poll)
            row = self.finish(f"listing-{n}", cid, row, absent)
            if row is not None and row.filled_quantity > 0:
                self.listing_filled.append(f"listing stop {n} filled {row.filled_quantity}")
        verdict = (f"{len(placed)} of {probe.listing_stops} sent, listed after "
                   f"{self.listed_together} s")
        if self.listing_filled:
            verdict += f"; {', '.join(self.listing_filled)}"
        self.out(f"l2_listing: {verdict}")
        return verdict

    def same_price(self, quote: Quote) -> str:
        """D1: a far BUY stop, then a second at the same price for twice the quantity; the
        second's fate (accepted, or refused as a duplicate) is the answer. Both are then
        cancelled and proven over."""
        probe = self.config.probe
        stop = floor_tick(quote.ask * (1 + probe.listing_far_percent / PERCENT))
        stop += tick(stop)
        rows: list[OrderDetail | None] = []
        poll = float(self.config.orders.detail_poll_seconds)
        for n, quantity in enumerate((Decimal(probe.quantity), Decimal(probe.quantity) * 2)):
            name = f"same-price-{n}"
            cid = self._cid("same")
            sent = self.place(name, self.request(cid, Side.BUY, OrderType.STOP_LOSS, quantity,
                                                 session=CORE, stop=stop))
            if sent in (REFUSED, NOT_SENT):
                rows.append(None)
                if n == 0:
                    break
                continue
            row, absent = self.settle(name, cid, poll)
            rows.append(row)
            self._same_open.append((name, cid, row, absent))
        for name, cid, row, absent in self._same_open:
            self.finish(name, cid, row, absent)
        if len(rows) < 2 or rows[0] is None:
            verdict = f"{NOT_TESTED} (the first stop was not accepted)"
        elif rows[1] is None:
            verdict = "refused: a stop at the same price with another quantity is a duplicate"
        else:
            verdict = f"accepted ({rows[1].raw_status}): the quantity tells stops apart"
        self.out(f"d1_same_price: {verdict}")
        return verdict

    def _history_shows(self, name: str, cid: str, filled: Decimal) -> bool:
        """One order-history read (today's fills, every page): whether it shows `cid` with
        `filled` shares. A failed read counts as not shown and is logged."""
        try:
            fills = self.broker.fills_today(ny_date_of(self.now()))
        except BrokerError as exc:
            self._log("history-failed", case=name, error=str(exc))
            return False
        quantity = next((f.filled_quantity for f in fills if f.client_order_id == cid), None)
        self._log("history", case=name, client_order_id=cid, filled=quantity, rows=len(fills))
        return quantity == filled

    def _record_history_lag(self, name: str, detail_at: datetime,
                            history_at: datetime | None) -> None:
        lag = None if history_at is None else (history_at - detail_at).total_seconds()
        if self.history_lag is None:
            self.history_lag = {}
        self.history_lag[name] = lag
        self._log("history-lag", case=name, detail_fill_at=detail_at.isoformat(),
                  history_at=None if history_at is None else history_at.isoformat(),
                  seconds=lag)

    def history_after(self, name: str, cid: str, row: OrderDetail | None) -> None:
        """H1 for an order Detail just showed filled: the history each poll for
        `[probe] history_wait_seconds` until it shows the same filled quantity."""
        if row is None or row.filled_quantity <= 0:
            return
        detail_at = self.now()
        poll = float(self.config.orders.detail_poll_seconds)
        history_at = detail_at if self._history_shows(name, cid, row.filled_quantity) else None
        for _ in range(int(self.config.probe.history_wait_seconds // poll)):
            if history_at is not None:
                break
            self.sleep(poll)
            if self._history_shows(name, cid, row.filled_quantity):
                history_at = self.now()
        self._record_history_lag(name, detail_at, history_at)

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
        moved symbol relies on the positions read showing a fill. The time Detail showed the
        fill is kept for H1, measured later (`_entry_history`)."""
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
            if row is not None:
                self._entry_pending = (cid, row.filled_quantity, detail_at)
        return row, row is None and absent >= self.config.orders.unknown_absent_reads

    def _send_trailing(self, name: str, step: Decimal,
                       kind: TrailingType = TrailingType.PERCENTAGE) -> tuple[str, str | None]:
        held = self.held()
        if held <= 0:
            return NOT_TESTED, None
        cid = self._cid(name)
        sent = self.place(name, self.request(cid, Side.SELL, OrderType.TRAILING_STOP_LOSS, held,
                                             session=CORE, trailing_step=step,
                                             trailing_type=kind))
        return sent, (None if sent in (REFUSED, NOT_SENT) else cid)

    def _first_accepted(self, name: str, steps: list[Decimal], kind: TrailingType,
                        ) -> tuple[Decimal | None, str | None, list[str]]:
        """Each step in turn, until Webull accepts one: a refusal, an unknown answer that
        Detail proves created nothing (`[orders] unknown_absent_reads` "not present" answers),
        or a row that ended unfilled (rejected after its acknowledgement) moves on; a step
        neither shown nor proven absent is made sure over and ends the scan as unknown.
        Returns the accepted step and its client order id (None, None when none was
        accepted or no share is held) and one line per step refused."""
        refused: list[str] = []
        poll = float(self.config.orders.detail_poll_seconds)
        for step in steps:
            sent, cid = self._send_trailing(name, step, kind)
            if sent == NOT_TESTED:
                return None, None, refused
            if cid is None:
                refused.append(f"{step}: {sent}")
                continue
            row, absent = self.settle(name, cid, poll * self.config.orders.unknown_absent_reads)
            if row is None and absent:
                refused.append(f"{step}: not created")
                continue
            if row is None:
                # Neither shown nor proven absent: make sure it is over, and stop the scan.
                self.finish(name, cid, None, False)
                refused.append(f"{step}: {UNKNOWN}")
                return None, None, refused
            if row.state.terminal and row.filled_quantity == 0:
                self.finish(name, cid, row, False)
                refused.append(f"{step}: {row.raw_status}")
                continue
            return step, cid, refused
        return None, None, refused

    def _watch(self, name: str, cid: str, seconds: float, *, listing: bool = False,
               ) -> tuple[OrderDetail | None, int, list[tuple[Decimal, Decimal]]]:
        """Detail and a quote every poll for `seconds`, stopping at a terminal row; with
        `listing`, the open orders too until they list `cid` (L1, `listed_after`). Returns
        the last row, the run of "not present" answers at the end, and (bid, stop price) of
        every poll where both were read."""
        placed_at = self.now()
        self._entry_history()  # the stop is out: H1's first read
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
            self._entry_history()
            if listing and self.listed_after is None and self._listed(cid) == LISTED:
                self.listed_after = (self.now() - placed_at).total_seconds()
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
                                        float(self.config.probe.trailing_observe_seconds),
                                        listing=True)
        answers["t4_open_orders"] = (NOT_TESTED if row is None
                                     else LISTED if self.listed_after is not None
                                     else NOT_LISTED)
        if row is None:
            answers["t1_accepted"] = UNKNOWN if sent == UNKNOWN else "acknowledged-not-in-detail"
        elif row.state is OrderState.ACCEPTED or row.state in TRIGGER_STATES:
            answers["t1_accepted"] = f"{ACCEPTED} ({row.raw_status})"
        else:
            answers["t1_accepted"] = f"{REFUSED} ({row.raw_status})"
        if seen:
            bid, stop = seen[0]
            answers["t2_unit"] = unit_of(step, bid, stop, self.config.probe.trailing_unit_factor)
            answers["t3_follows"] = follows(seen)
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

    def tight(self, unit: str) -> dict[str, str]:
        """T6 and T7: with the unit known, the tight percents smallest first until one is
        accepted (T7), and that one watched for a trigger (T6)."""
        if unit not in (UNIT_RATIO, UNIT_PERCENT):
            return {"t6_triggers": NOT_TESTED, "t7_smallest_percent": NOT_TESTED}
        percents = list(self.config.probe.trailing_tight_percents)
        steps = [p / PERCENT if unit == UNIT_RATIO else p for p in percents]
        name = "trailing-tight"
        step, cid, refused = self._first_accepted(name, steps, TrailingType.PERCENTAGE)
        smallest = (NOT_TESTED if step is None and not refused
                    else f"none of {percents} %" if step is None
                    else f"{percents[steps.index(step)]} %")
        answers = {"t7_smallest_percent": f"{smallest}; refused: {refused}"}
        self.out(f"t7_smallest_percent: {answers['t7_smallest_percent']}")
        if cid is None:
            answers["t6_triggers"] = NOT_TESTED
            return answers
        row, absent, seen = self._watch(name, cid,
                                        float(self.config.probe.trailing_trigger_seconds))
        # T3 again: a tight stop moves a cent for about a cent of rise, so a stop price that
        # stays put while the bid rises says Detail does not show the trailing stop's level.
        answers["t3_tight_follows"] = follows(seen) if seen else UNIT_NOT_SHOWN
        self.out(f"t3_tight_follows: {answers['t3_tight_follows']}")
        row = self.finish(name, cid, row,
                          row is None and absent >= self.config.orders.unknown_absent_reads)
        if row is not None and row.filled_quantity > 0:
            verdict = f"{TRIGGERED} (fill {row.average_fill_price})"
        elif row is None:
            verdict = UNKNOWN
        else:
            verdict = NOT_TRIGGERED
        self.out(f"t6_triggers: {verdict}")
        answers["t6_triggers"] = verdict
        return answers

    def amount(self) -> dict[str, str]:
        """A1 and A2: a wide AMOUNT trailing stop, step = the bid times
        `[probe] trailing_wide_percent`, in USD; watched like the wide one, then cancelled."""
        name = "trailing-amount"
        bid = self._bid(name)
        if bid is None:
            return {"a1_accepted": NOT_TESTED, "a2_distance": NOT_TESTED}
        step = (bid * self.config.probe.trailing_wide_percent / PERCENT).quantize(CENT)
        sent, cid = self._send_trailing(name, step, TrailingType.AMOUNT)
        answers = {"a1_accepted": sent, "a2_distance": NOT_TESTED}
        if cid is not None:
            row, absent, seen = self._watch(
                name, cid, float(self.config.probe.trailing_observe_seconds))
            if row is None:
                answers["a1_accepted"] = UNKNOWN if sent == UNKNOWN else "not-in-detail"
            else:
                answers["a1_accepted"] = f"{row.raw_status} (step {step} USD)"
            if seen:
                first_bid, first_stop = seen[0]
                distance = first_bid - first_stop
                fits = (distance > 0 and max(distance, step) / min(distance, step)
                        <= self.config.probe.trailing_unit_factor)
                answers["a2_distance"] = (
                    f"{'usd' if fits else UNIT_UNCLEAR}: bid {first_bid}, stop {first_stop}")
            elif row is not None:
                answers["a2_distance"] = UNIT_NOT_SHOWN
            self.finish(name, cid, row,
                        row is None and absent >= self.config.orders.unknown_absent_reads)
        for key, value in answers.items():
            self.out(f"{key}: {value}")
        return answers

    def smallest_amount(self) -> str:
        """A3: the `[probe] trailing_amount_steps_usd` smallest first until Webull accepts
        one; it is cancelled at once (a trigger meanwhile is recorded)."""
        name = "trailing-amount-min"
        steps = list(self.config.probe.trailing_amount_steps_usd)
        step, cid, refused = self._first_accepted(name, steps, TrailingType.AMOUNT)
        if cid is None:
            verdict = (NOT_TESTED if not refused else f"none of {steps} USD") + \
                f"; refused: {refused}"
        else:
            row = self.finish(name, cid, None, False)
            filled = row is not None and row.filled_quantity > 0
            verdict = (f"{step} USD{' (triggered before its cancel)' if filled else ''}; "
                       f"refused: {refused}")
        self.out(f"a3_smallest_amount: {verdict}")
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
        row = self.finish("close", cid, row, absent)
        self.history_after("close", cid, row)
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
            summary["l2_listing"] = probe.listing(quote)
            summary["l2_listed_after_seconds"] = probe.listed_together
            summary["d1_same_price"] = probe.same_price(probe.quote())
            summary["l2_note"] = (
                "BUY stops (the bot's stops are SELLs; L1 is one SELL sample); the first "
                "open-orders read is one poll after the last place, so the smallest answer is "
                f"'within {config.orders.detail_poll_seconds} s'; null = not listed within "
                "[probe] listing_wait_seconds, or the stop filled (see l2_listing)")
            quote = probe.quote()
            summary["entry"] = probe.entry(quote)
            summary["p1_positions_lag_seconds"] = probe.lag
            summary["p1_note"] = (
                f"poll-granular ({config.orders.detail_poll_seconds} s): each poll reads "
                "Detail, then the positions, so 0.0 means within one poll; null = the "
                "positions never showed the fill")
            if summary["entry"] == FILLED:
                answers = probe.wide()
                summary.update(answers)
                summary["l1_listed_after_seconds"] = probe.listed_after
                summary.update(probe.amount())
                if "t6_triggers" not in answers:
                    summary.update(probe.tight(answers["t2_unit"]))
                summary["a3_smallest_amount"] = probe.smallest_amount()
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
        try:
            probe._entry_history(final=True)
        except Exception as exc:  # noqa: BLE001 - the summary below must still be written
            summary["h1_error"] = f"{type(exc).__name__}: {exc}"
        summary["h1_history_lag_seconds"] = probe.history_lag
        summary["h1_note"] = (
            "per filled order, seconds from Detail first showing the fill to the order history "
            "first showing the same filled quantity, poll-granular; null = not shown within "
            "[probe] history_wait_seconds")
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
