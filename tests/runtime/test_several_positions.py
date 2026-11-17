"""Several positions at once (backlog X1), built but off by default (`[risk]
max_open_positions = 1`): each open trade takes one slot, at most one trade per symbol, every
trade has its own broker stop sized on its own fills, and all trades share the daily loss
ceiling through their reservations."""

from __future__ import annotations

import re
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal as D

import pytest

from tickr import config
from tickr.broker.base import BrokerError
from tickr.broker.fake import FakeBroker
from tickr.config import GateResult
from tickr.engine.core import Engine, EntryRefused, Fence, Leg, TradeState
from tickr.observers.log import DecisionLog
from tickr.runtime import launchd
from tickr.runtime.entries import EntryDesk, slots_in_use
from tickr.store import Store
from tickr.types import OrderState, OrderType, Quote, Side

from .test_entries import BAR, CALENDAR, CFG, NOW, ROOT, decision

pytestmark = pytest.mark.skipif(not config.trading_gate(CFG).open,
                                reason="the configured trading gate is closed")


def wide(slots: int, **risk: object) -> config.Config:
    return replace(CFG, risk=replace(CFG.risk, max_open_positions=slots, **risk))


def quote(symbol: str, ask: str = "10.05") -> Quote:
    return Quote(symbol, D("10.04"), D(ask), D(100), D(100), NOW - timedelta(seconds=1), NOW)


class Desk:
    def __init__(self, tmp_path, cfg: config.Config) -> None:
        self.broker = FakeBroker()
        self.store = Store.open(tmp_path / "db.sqlite3", busy_timeout_seconds=0)
        self.clock = [1_000_000.0]
        self.engine = Engine(self.store, self.broker, cfg, "desk0001",
                             lambda: GateResult(True, "t"), clock=lambda: self.clock[0])
        self.engine.recover()
        self.desk = EntryDesk(self.engine, cfg, calendar=CALENDAR,
                              last_broker_read_at=lambda: NOW,
                              log=DecisionLog(tmp_path / "decisions"))

    def enter(self, symbol: str, **overrides: object):
        return self.desk.on_decision(decision(symbol=symbol, **overrides), bar_start=BAR,
                                     quote=quote(symbol), now=NOW)

    def step(self, steps: int = 1) -> None:
        """Advance one Detail poll interval per step, like the loop does."""
        for _ in range(steps):
            self.clock[0] += float(CFG.orders.detail_poll_seconds)
            self.engine.step()

    def trade_of(self, symbol: str):
        return next(t for t in self.engine.trades() if t.symbol == symbol)

    def fill(self, symbol: str, quantity: D = D(10)) -> None:
        entry = self.engine.orders(self.trade_of(symbol).trade_id)[0]
        self.broker.fill(entry.client_order_id, quantity, D("10.05"))
        self.step(3)

    def stops(self, symbol: str):
        return [o for o in self.engine.orders(self.trade_of(symbol).trade_id)
                if o.leg is Leg.STOP]


def test_each_open_trade_takes_one_slot_until_the_limit(tmp_path):
    desk = Desk(tmp_path, wide(3))
    assert [desk.enter(s).admission for s in ("AAA", "BBB", "CCC")] == ["taken"] * 3
    record = desk.enter("DDD")
    assert record.admission == "refused"
    assert record.refusal_cause.startswith("POSITION_LIMIT_REACHED: 3 open position(s), limit 3")
    assert slots_in_use(desk.engine) == 3


def test_a_working_entry_takes_its_slot_and_blocks_only_its_own_symbol(tmp_path):
    desk = Desk(tmp_path, wide(3))
    assert desk.enter("AAA").admission == "taken"  # entry working, nothing filled
    again = desk.enter("AAA")
    assert again.refusal_cause.startswith("OWN_ORDER_WORKING")
    desk.fill("AAA")
    assert desk.enter("AAA").refusal_cause.startswith("OWN_POSITION_OPEN")
    assert desk.enter("BBB").admission == "taken"


def test_the_engine_refuses_a_second_trade_in_a_symbol_whatever_admission_says(tmp_path):
    desk = Desk(tmp_path, wide(3))
    desk.engine.open_trade("AAA", D(2), D("10.00"), D("9.80"), D(1))
    with pytest.raises(EntryRefused, match="a trade in AAA is already open"):
        desk.engine.open_trade("AAA", D(2), D("10.00"), D("9.80"), D(1))
    assert len(desk.engine.trades()) == 1


def test_every_position_has_its_own_stop_sized_on_its_own_fills(tmp_path):
    desk = Desk(tmp_path, wide(3))
    for symbol in ("AAA", "BBB"):
        desk.enter(symbol)
    desk.fill("AAA", D(4))
    desk.fill("BBB", D(10))
    for symbol, quantity in (("AAA", D(4)), ("BBB", D(10))):
        (stop,) = desk.stops(symbol)
        assert (stop.symbol, stop.order_type, stop.quantity) == (symbol, OrderType.STOP_LOSS,
                                                                 quantity)
        assert stop.verified
    assert not any(n.startswith(Fence.UNPROTECTED) for n in desk.engine.fences())


def test_a_stop_out_closes_only_its_trade_and_frees_its_slot(tmp_path):
    desk = Desk(tmp_path, wide(2))
    for symbol in ("AAA", "BBB"):
        desk.enter(symbol)
        desk.fill(symbol)
    assert desk.enter("CCC").refusal_cause.startswith("POSITION_LIMIT_REACHED")
    (stop,) = desk.stops("AAA")
    aaa = desk.trade_of("AAA").trade_id
    desk.broker.fill(stop.client_order_id, D(10), D("9.70"))
    desk.step(3)
    assert desk.engine.trade(aaa).state is TradeState.CLOSED
    (bbb,) = desk.engine.trades()
    assert bbb.symbol == "BBB" and bbb.state is TradeState.OPEN
    assert desk.stops("BBB")[0].live
    assert desk.engine.fences() == {}
    assert desk.enter("CCC").admission == "taken"


def test_reconcile_with_several_positions_agrees_and_flags_only_real_mismatches(tmp_path):
    desk = Desk(tmp_path, wide(3))
    for symbol in ("AAA", "BBB"):
        desk.enter(symbol)
        desk.fill(symbol)
    assert desk.engine.reconcile() and desk.engine.fences() == {}
    desk.broker.add_foreign_position("BBB", D(5), D(10))  # shares bought by hand in BBB
    assert desk.engine.reconcile()
    bbb = desk.trade_of("BBB").trade_id
    assert desk.engine.fences() == {f"{Fence.QUANTITY_MISMATCH}:{bbb}":
                                    "broker holds 15, Detail says 10"}
    # Hard rule 1: a conflict blocks every new entry; both stops stay on the Detail quantity.
    assert desk.enter("CCC").refusal_cause.startswith("UNRESOLVED_EXPOSURE")
    assert [(s.live, s.quantity) for sym in ("AAA", "BBB") for s in desk.stops(sym)] == [
        (True, D(10)), (True, D(10))]


def test_all_trades_share_the_daily_loss_ceiling(tmp_path):
    # 10 x (10.10 - 9.75) = 3.50 reserved per trade; a ceiling of 9 leaves room for two.
    desk = Desk(tmp_path, wide(5, max_daily_loss_usd=D(9), min_daily_room_usd=D(1)))
    assert [desk.enter(s).admission for s in ("AAA", "BBB")] == ["taken", "taken"]
    record = desk.enter("CCC")
    assert record.admission == "refused" and record.refusal_cause.startswith("DAILY_CEILING")


def test_shutdown_and_flatten_cover_every_trade(tmp_path):
    desk = Desk(tmp_path, wide(3))
    for symbol in ("AAA", "BBB"):
        desk.enter(symbol)
        desk.fill(symbol)
    for trade in desk.engine.trades():
        desk.engine.request_exit(trade.trade_id, "session-flatten")
    desk.step(10)  # one Detail read per step: the teardowns take turns
    exits = [o for o in desk.engine.orders() if o.leg is Leg.EXIT]
    assert sorted((o.symbol, o.quantity) for o in exits) == [("AAA", D(10)), ("BBB", D(10))]


@pytest.mark.parametrize("value", [0, CFG.market_data.stream_reserved_symbols + 1])
def test_config_refuses_a_slot_count_outside_1_to_the_reserved_stream_slots(tmp_path, value):
    text = (ROOT / "config" / "bot.toml").read_text("utf-8")
    text = re.sub(r"(?m)^max_open_positions = \d+", f"max_open_positions = {value}", text)
    (tmp_path / "config").mkdir()
    path = tmp_path / "config" / "bot.toml"
    path.write_text(text, "utf-8")
    with pytest.raises(config.ConfigError, match="max_open_positions"):
        config.load(path, ROOT)


def test_the_shipped_config_keeps_one_slot():
    assert CFG.risk.max_open_positions == 1


MAX = CFG.market_data.stream_reserved_symbols  # the most slots config allows
POLL = float(CFG.orders.detail_poll_seconds)
SYMBOLS = [f"S{n:02d}" for n in range(MAX)]


def full(tmp_path, *, filled: int, **overrides: object) -> Desk:
    """Every slot taken: the first `filled` trades hold a protected position, the others have
    their entry working; then long enough that every order has been read in turn."""
    desk = Desk(tmp_path, wide(MAX, max_daily_loss_usd=D(MAX * 10)))
    for n, symbol in enumerate(SYMBOLS):
        assert desk.enter(symbol, **overrides).admission == "taken"
        if n < filled:
            desk.fill(symbol)
    desk.step(2 * MAX)
    return desk


def steps_until(desk: Desk, done) -> int:
    steps = 0
    while not done():
        desk.step()
        steps += 1
        assert steps <= MAX, "did not happen within one step per slot"
    return steps


@pytest.mark.parametrize("filled", [0, MAX - 1])
def test_at_the_most_slots_a_fill_gets_a_verified_stop_in_two_polls(tmp_path, filled):
    """The delay from a fill to its verified stop does not grow with the slots: the positions
    read shows the symbol moved, so its entry's Detail is read first, and the new stop is
    the first in the common queue. Pinned at the highest allowed count, with every other
    slot an entry still working (the longest queue) or a protected position."""
    desk = full(tmp_path, filled=filled)
    symbol = SYMBOLS[-1]
    entry = desk.engine.orders(desk.trade_of(symbol).trade_id)[0]
    desk.broker.fill(entry.client_order_id, D(10), D("10.05"))
    steps = steps_until(desk, lambda: any(s.verified for s in desk.stops(symbol)))
    assert steps <= 2
    assert steps * POLL < float(CFG.orders.max_unprotected_seconds) / 2


def test_at_the_most_slots_a_stop_out_is_seen_in_one_poll(tmp_path):
    desk = full(tmp_path, filled=MAX)
    symbol = SYMBOLS[MAX // 2]
    trade_id = desk.trade_of(symbol).trade_id
    (stop,) = desk.stops(symbol)
    desk.broker.fill(stop.client_order_id, D(10), D("9.70"))
    assert steps_until(desk, lambda: desk.engine.trade(trade_id).state
                       is TradeState.CLOSED) <= 1


def test_at_the_most_slots_the_flatten_takes_one_position_at_a_time(tmp_path):
    """Session flatten of every slot: each position keeps its stop until the one before it
    has its close sent, so none is without a stop for more than one poll, and the whole
    flatten fits well inside the time from the flatten to the close-order cutoff."""
    desk = full(tmp_path, filled=MAX)
    for trade in desk.engine.trades():
        desk.engine.request_exit(trade.trade_id, "session-flatten")
    stopless: dict[str, int] = {}
    sold: set[str] = set()
    steps = 0
    while len([o for o in desk.engine.orders() if o.leg is Leg.EXIT]) < MAX:
        desk.step()
        steps += 1
        for o in desk.engine.orders():  # closes fill at once, like liquid market sells
            if o.leg is Leg.EXIT and o.client_order_id not in sold:
                desk.broker.fill(o.client_order_id, o.quantity, D("10.00"))
                sold.add(o.client_order_id)
        for symbol in SYMBOLS:
            orders = desk.engine.orders(desk.trade_of(symbol).trade_id) if any(
                t.symbol == symbol for t in desk.engine.trades()) else []
            if any(o.leg is Leg.STOP and o.cancelling for o in orders) and not any(
                    o.leg is Leg.EXIT for o in orders):
                stopless[symbol] = stopless.get(symbol, 0) + 1
        assert steps <= 2 * MAX
    assert max(stopless.values()) <= 1
    window = (CFG.session.flatten_minutes_before_close * 60
              - CFG.session.close_order_cutoff_seconds)
    assert (steps + 1) * POLL <= window / 2


WINDOW = float(CFG.session.flatten_minutes_before_close * 60
               - CFG.session.close_order_cutoff_seconds)
STOPS = CFG.orders.max_stops_per_trade
# The most slots the start-time check lets through with the shipped poll and stop cap.
ALLOWED = int(WINDOW / (STOPS * 2 * POLL))


def bare_shares(broker: FakeBroker) -> dict[str, D]:
    """Per symbol, shares the broker holds with no working stop and no working sell."""
    covered: dict[str, D] = {}
    for o in broker.open_orders():
        if o.side is Side.SELL:
            covered[o.symbol] = covered.get(o.symbol, D(0)) + o.quantity - o.filled_quantity
    return {p.symbol: p.quantity - covered.get(p.symbol, D(0)) for p in broker.positions()}


def flatten(desk: Desk, fill_delay: int, symbols: list[str] = SYMBOLS) -> tuple[int, int]:
    """Request the session flatten of every trade; closes fill `fill_delay` polls after they
    are sent. Returns the polls until the broker is flat and the most consecutive polls any
    symbol had bare shares."""
    for trade in desk.engine.trades():
        desk.engine.request_exit(trade.trade_id, "session-flatten")
    sent: dict[str, int] = {}
    streak: dict[str, int] = {}
    worst = 0
    steps = 0
    while desk.broker.positions():
        desk.step()
        steps += 1
        for o in desk.engine.orders():
            if o.leg is Leg.EXIT and o.client_order_id not in sent:
                sent[o.client_order_id] = steps
        for cid, at in sent.items():
            order = desk.broker.orders.get(cid)
            if order is not None and not order.state.terminal and steps - at >= fill_delay:
                desk.broker.fill(cid, order.quantity - order.filled_quantity, D("10.00"))
        bare = bare_shares(desk.broker)
        for symbol in symbols:
            streak[symbol] = streak.get(symbol, 0) + 1 if bare.get(symbol, D(0)) > 0 else 0
            worst = max(worst, streak[symbol])
        assert steps <= 4 * MAX * STOPS, "the flatten did not finish"
    return steps, worst


@pytest.mark.parametrize("fill_delay", [0, 1, 2, 4])
@pytest.mark.parametrize("slots", [1, ALLOWED])
def test_the_flatten_of_split_stops_leaves_shares_bare_for_one_cancel_read(tmp_path, slots,
                                                                           fill_delay):
    """One stop is cancelled at a time across every trade, and the shares of a stop whose
    cancel Detail confirms are sold at once, never after another close's fill: each stop's
    shares are without a working stop or sell for one poll (its cancel's read), so a
    position has bare shares for at most one poll per stop, whatever the slots and the
    closes' fill delay, and the flatten fits the start-time bound."""
    desk = Desk(tmp_path, wide(slots, max_daily_loss_usd=D(MAX * 10)))
    for symbol in SYMBOLS[:slots]:
        assert desk.enter(symbol, scale_out=D(4)).admission == "taken"
        desk.fill(symbol)
    desk.step(2 * slots)
    assert {len(desk.stops(s)) for s in SYMBOLS[:slots]} == {2}
    steps, worst = flatten(desk, fill_delay)
    assert worst <= 2  # two stops, one poll each, back to back
    assert STOPS * POLL < float(CFG.orders.max_unprotected_seconds)
    assert steps * POLL <= slots * STOPS * 2 * POLL <= WINDOW


@pytest.mark.parametrize("fill_delay", [0, 2])
def test_three_stops_per_position_flatten_as_fast_when_closes_fill_at_once(tmp_path,
                                                                          fill_delay):
    """Entries filled 3 + 7 with a scale-out hold three stops each. A close that fills at
    once moves the position, but that move is our own close: the symbol's other stops are
    not read ahead of its cancel, so instant fills are no slower than late ones."""
    desk = Desk(tmp_path, wide(MAX, max_daily_loss_usd=D(MAX * 10)))
    for symbol in SYMBOLS:
        assert desk.enter(symbol, scale_out=D(4)).admission == "taken"
        entry = desk.engine.orders(desk.trade_of(symbol).trade_id)[0]
        for piece in (D(3), D(7)):
            desk.broker.fill(entry.client_order_id, piece, D("10.05"))
            desk.step(3)
    desk.step(2 * MAX)
    assert {len([s for s in desk.stops(x) if s.live]) for x in SYMBOLS} == {3}
    steps, worst = flatten(desk, fill_delay)
    assert worst <= 3  # three stops, one poll each
    assert steps <= MAX * 3 + 3


@pytest.mark.parametrize("fill_delay", [0, 1, 2])
@pytest.mark.parametrize("pieces", [8, 10])
def test_at_one_slot_a_position_with_many_stops_flattens_in_two_polls_per_stop(tmp_path,
                                                                              pieces,
                                                                              fill_delay):
    """One slot (no stop cap): an entry filled one share per observation holds a stop per
    piece. Each close of ours moves the position, which must not put the other stops' reads
    ahead of the waiting cancel: the flatten takes at most two polls per stop, and the
    shares are bare for one poll per stop at most, whatever the closes' fill delay."""
    desk = Desk(tmp_path, wide(1))
    assert desk.enter("AAA", quantity=D(pieces)).admission == "taken"
    entry = desk.engine.orders(desk.trade_of("AAA").trade_id)[0]
    for _ in range(pieces):
        desk.broker.fill(entry.client_order_id, D(1), D("10.05"))
        desk.step(3)
    desk.step(6)
    stops = len([s for s in desk.stops("AAA") if s.live])
    assert stops >= 7
    steps, worst = flatten(desk, fill_delay, ["AAA"])
    assert steps <= 2 * stops
    assert worst <= stops


def test_with_one_slot_an_entry_filling_in_pieces_fills_to_its_size(tmp_path):
    """Owner 2026-11-17: the stop cap applies only with several slots. With one, an entry
    filling 1 + 2 + 3 + 4 ends holding all 10, each piece under its stop."""
    desk = Desk(tmp_path, wide(1))
    assert desk.enter("AAA").admission == "taken"
    entry = desk.engine.orders(desk.trade_of("AAA").trade_id)[0]
    for piece in (1, 2, 3, 4):
        desk.broker.fill(entry.client_order_id, D(piece), D("10.05"))
        desk.step(4)
    assert desk.broker.holdings["AAA"].quantity == 10
    live = [s for s in desk.stops("AAA") if s.live]
    assert len(live) == 4 and all(s.verified for s in live)
    assert sum((s.quantity for s in live), D(0)) == 10
    assert not desk.store.journal("entry-capped", desk.trade_of("AAA").trade_id)


def test_an_entry_filling_in_pieces_is_capped_at_the_stop_limit(tmp_path):
    """With several slots, each fill observation adds a stop (two across the scale-out
    boundary); once the trade holds the limit minus what one more observation adds, the
    entry's rest is cancelled, so the flatten bound's stop count holds."""
    desk = Desk(tmp_path, wide(2))
    assert desk.enter("AAA", scale_out=D(6)).admission == "taken"
    entry = desk.engine.orders(desk.trade_of("AAA").trade_id)[0]
    for _ in range(4):
        if desk.broker.orders[entry.client_order_id].state.terminal:
            break
        desk.broker.fill(entry.client_order_id, D(1), D("10.05"))
        desk.step(4)
    assert desk.broker.orders[entry.client_order_id].state is OrderState.CANCELLED
    live = [s for s in desk.stops("AAA") if s.live]
    assert len(live) <= STOPS
    assert sum((s.quantity for s in live), D(0)) == desk.broker.holdings["AAA"].quantity
    assert desk.store.journal("entry-capped", desk.trade_of("AAA").trade_id)


def test_a_close_never_waits_on_a_stop_whose_detail_cannot_be_read(tmp_path):
    """One position at a time loses its stop, but a stop whose cancel cannot be confirmed
    (its Detail reads keep failing after the cancel) holds the other closes back only until
    the detail-unreadable fence, which alerts; it is never sold blind."""
    desk = Desk(tmp_path, wide(3))
    for symbol in ("AAA", "BBB"):
        desk.enter(symbol)
        desk.fill(symbol)
    desk.step(5)
    for trade in desk.engine.trades():
        desk.engine.request_exit(trade.trade_id, "session-flatten")
    steps_until(desk, lambda: desk.stops("AAA")[0].cancelling)
    stuck = desk.stops("AAA")[0].client_order_id
    real = desk.broker.order_detail

    def detail(cid):
        if cid == stuck:
            raise BrokerError("down")
        return real(cid)

    desk.broker.order_detail = detail  # type: ignore[method-assign]
    # The fence comes `detail_unreadable_seconds` after the first failed read; then BBB's
    # teardown takes its two reads (its stop's cancel, then the close goes out).
    limit = int(float(CFG.orders.detail_unreadable_seconds) / POLL) + 3
    steps = steps_until(desk, lambda: any(o.leg is Leg.EXIT and o.symbol == "BBB"
                                          for o in desk.engine.orders()))
    assert steps <= limit
    assert f"{Fence.DETAIL_UNREADABLE}:{stuck}" in desk.engine.fences()
    assert not any(o.leg is Leg.EXIT and o.symbol == "AAA" for o in desk.engine.orders())


def test_config_refuses_a_poll_too_slow_for_the_flatten_of_every_slot(tmp_path):
    text = (ROOT / "config" / "bot.toml").read_text("utf-8")
    window = (CFG.session.flatten_minutes_before_close * 60
              - CFG.session.close_order_cutoff_seconds)
    poll = int(window / (2 * STOPS * MAX)) + 1
    text = re.sub(r"(?m)^max_open_positions = \d+", f"max_open_positions = {MAX}", text)
    text = re.sub(r"(?m)^detail_poll_seconds = \d+", f"detail_poll_seconds = {poll}", text)
    (tmp_path / "config").mkdir()
    path = tmp_path / "config" / "bot.toml"
    path.write_text(text, "utf-8")
    with pytest.raises(config.ConfigError, match="close_order_cutoff_seconds"):
        launchd.check_timings(config.load(path, ROOT))
