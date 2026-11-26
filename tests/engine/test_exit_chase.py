"""Limit-exit chase before a market exit (backlog X3): cancel-then-place re-pricing on the fake
broker, the cap and the floor, races and unknown outcomes, and the switch off."""

from __future__ import annotations

from collections import Counter
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal as D
from pathlib import Path

import pytest

from tickr import config
from tickr.broker.base import OutcomeUnknown
from tickr.broker.fake import FakeBroker
from tickr.config import ConfigError, GateResult
from tickr.engine import chase
from tickr.engine.core import Engine, Fence, Leg, SendState, TradeState
from tickr.store import Store
from tickr.types import OrderState, OrderType, Side

ROOT = Path(__file__).resolve().parents[2]
CFG = config.load(ROOT / "config" / "bot.toml")
POLL = float(CFG.orders.detail_poll_seconds)
CHASE = replace(CFG.exit_chase, enabled=True)
REPRICE = float(CHASE.reprice_seconds)
ORDERS = 1 + CHASE.max_reprices
ATR = D("0.40")  # floor 0.5 ATR = 0.20 under the first bid


class Rig:
    """Engine and store on one clock, a quote the test moves, a session the test closes."""

    def __init__(self, tmp_path: Path, *, enabled: bool = True,
                 quote: bool = True) -> None:
        self.broker = FakeBroker()
        self.epoch = 1_000_000.0
        self.bid: D | None = D("10.00")
        self.refusal: str | None = None
        self.store = Store.open(tmp_path / "db.sqlite3", busy_timeout_seconds=0,
                                now=self.now)
        cfg = replace(CFG, root=tmp_path,
                      exit_chase=replace(CFG.exit_chase, enabled=enabled))
        self.engine = Engine(self.store, self.broker, cfg, "chase001",
                             lambda: GateResult(True, "t"), clock=lambda: self.epoch,
                             close_refusal=lambda: self.refusal,
                             exit_quote=self.quote if quote else (lambda _s: None))

    def now(self) -> datetime:
        return datetime.fromtimestamp(self.epoch, UTC)

    def quote(self, symbol: str) -> chase.ExitQuote | None:  # noqa: ARG002 - one symbol
        return None if self.bid is None else chase.ExitQuote(self.bid, ATR, D(1))

    def run(self, steps: int = 1) -> None:
        for _ in range(steps):
            self.epoch += POLL
            self.engine.step()

    def position(self, qty: str = "2") -> str:
        """`qty` shares filled under a verified stop, then an exit asked."""
        self.engine.recover()
        t = self.engine.open_trade("ABC", D(qty), D(10), D("9.80"), D(1))
        self.broker.fill(self.closes(t, Leg.ENTRY)[0], D(qty), D(10))
        self.run(3)
        self.engine.request_exit(t, "test")
        return t

    def closes(self, trade_id: str, leg: Leg = Leg.EXIT) -> list[str]:
        return [o.client_order_id for o in self.engine.orders(trade_id) if o.leg is leg]

    def sent(self, order_type: OrderType) -> list:
        """SELL orders of that type sent (the entry is a LIMIT BUY)."""
        return [r for r in self.broker.sent
                if r.order_type is order_type and r.side is Side.SELL]

    def working(self, order_type: OrderType) -> list:
        return [o for o in self.broker.open_orders()
                if o.order_type is order_type and o.side is Side.SELL]

    def until(self, check, limit: int = 60) -> None:
        for _ in range(limit):
            if check():
                return
            self.run(1)
        raise AssertionError("condition never met")


def single_sends(broker: FakeBroker) -> None:
    counts = Counter(r.client_order_id for r in broker.sent)
    assert all(n == 1 for n in counts.values()), counts


def kinds(rig: Rig, trade_id: str) -> list[str]:
    return [r.kind for r in rig.store.journal(key=trade_id) if r.kind.startswith("exit-chase")]


# --- pure rules --------------------------------------------------------------------------


@pytest.mark.parametrize(("bid", "price"), [
    (D("10.00"), D("9.99")), (D("1.00"), D("0.99")), (D("0.5000"), D("0.4999")),
    (D("10.005"), D("10.00")),
])
def test_one_tick_through_the_bid_on_the_tick_grid(bid, price):
    assert chase.limit_price(bid, 1) == price


def test_start_refuses_while_a_stop_rests_or_a_close_works_or_without_a_fresh_quote():
    q = chase.ExitQuote(D(10), ATR, D(1))
    age = CFG.risk.max_quote_age_seconds

    def start(quote, stops=False, closes=False):
        return chase.start(quote, stops_live=stops, closes_live=closes, max_quote_age=age,
                           settings=CHASE)

    assert start(q, stops=True) is chase.Refusal.STOP_RESTING
    assert start(q, closes=True) is chase.Refusal.CLOSE_WORKING
    assert start(None) is chase.Refusal.NO_QUOTE
    assert start(replace(q, age_seconds=age + 1)) is chase.Refusal.STALE_QUOTE
    assert start(replace(q, atr=D(0))) is chase.Refusal.BAD_QUOTE
    assert start(q) == chase.Start(D(10), ATR, D(10) - ATR * CHASE.max_worse_atr)


@pytest.mark.parametrize("market", [True, False])
def test_the_end_sends_market_only_where_the_session_allows_it(market):
    begun = chase.Start(D(10), ATR, D("9.80"))
    q = chase.ExitQuote(D(10), ATR, D(1))
    age = CFG.risk.max_quote_age_seconds

    def step(sent, quote):
        return chase.next_order(begun, sent, quote, max_quote_age=age, market_allowed=market,
                                settings=CHASE)

    assert step(ORDERS - 1, q) == chase.Price(D("9.99"), D(10))
    assert step(ORDERS, q) == chase.End(chase.EndCause.CAP_REACHED, market)
    assert step(1, replace(q, bid=D("9.80"))) == chase.End(chase.EndCause.FLOOR, market)
    assert step(1, replace(q, bid=D("9.81"))) == chase.Price(D("9.80"), D("9.81"))
    assert step(1, None) == chase.End(chase.EndCause.NO_QUOTE, market)


def test_config_load_refuses_the_chase_on_until_it_is_wired(tmp_path):
    text = (ROOT / "config" / "bot.toml").read_text("utf-8")
    on = text.replace("[exit_chase]\n", "[exit_chase]\n", 1).replace(
        "\nenabled = false\n# Ticks below the bid", "\nenabled = true\n# Ticks below the bid", 1)
    assert on != text
    (tmp_path / "bot.toml").write_text(on, "utf-8")
    with pytest.raises(ConfigError, match="exit_chase"):
        config.load(tmp_path / "bot.toml", root=ROOT)


# --- engine ------------------------------------------------------------------------------


def test_the_chase_sells_by_limit_through_the_bid_once_the_stop_is_gone(tmp_path):
    rig = Rig(tmp_path)
    t = rig.position()
    rig.until(lambda: rig.sent(OrderType.LIMIT))
    (order,) = rig.sent(OrderType.LIMIT)
    assert order.limit_price == D("9.99") and order.quantity == D(2)
    assert not rig.working(OrderType.STOP_LOSS)  # never a SELL while the stop reserves shares
    assert rig.store.journal(f"order-{SendState.INTENT}", order.client_order_id)
    rig.broker.fill(order.client_order_id, D(2), D("9.99"))
    rig.run(2)
    assert rig.engine.trade(t).state is TradeState.CLOSED
    assert rig.sent(OrderType.MARKET) == [] and rig.engine.fences() == {}
    assert kinds(rig, t) == [chase.CHASE_STARTED, chase.CHASE_PRICE]
    single_sends(rig.broker)


def test_reprice_cancels_waits_for_detail_then_places_at_the_new_bid(tmp_path):
    rig = Rig(tmp_path)
    rig.position()
    rig.until(lambda: rig.sent(OrderType.LIMIT))
    first = rig.sent(OrderType.LIMIT)[0].client_order_id
    rig.bid = D("9.95")
    while len(rig.sent(OrderType.LIMIT)) < 2:
        assert len(rig.working(OrderType.LIMIT)) <= 1  # never two chase orders at once
        rig.run(1)
    assert rig.broker.orders[first].state is OrderState.CANCELLED
    (cancelled_at,) = rig.store.journal("cancel-intent", first)
    second = rig.sent(OrderType.LIMIT)[1]
    assert second.limit_price == D("9.94") and second.quantity == D(2)
    # The new order's intent was recorded after a Detail read showed the cancel took.
    observed = [r for r in rig.store.journal("order-observed", first)
                if r.body["state"] == OrderState.CANCELLED]
    (intent,) = rig.store.journal("order-intent", second.client_order_id)
    assert cancelled_at.seq < observed[0].seq < intent.seq
    single_sends(rig.broker)


def test_a_partial_fill_during_the_chase_leaves_only_the_rest_to_sell(tmp_path):
    rig = Rig(tmp_path)
    t = rig.position("3")
    rig.until(lambda: rig.sent(OrderType.LIMIT))
    first = rig.sent(OrderType.LIMIT)[0].client_order_id
    rig.broker.fill(first, D(1), D("9.99"))
    rig.until(lambda: len(rig.sent(OrderType.LIMIT)) == 2)
    assert rig.sent(OrderType.LIMIT)[1].quantity == D(2)
    rig.broker.fill(rig.sent(OrderType.LIMIT)[1].client_order_id, D(2), D("9.99"))
    rig.run(2)
    assert rig.engine.trade(t).state is TradeState.CLOSED and rig.broker.positions() == []
    single_sends(rig.broker)


def test_a_fill_racing_the_reprice_cancel_sends_nothing_more(tmp_path):
    rig = Rig(tmp_path)
    t = rig.position()
    rig.until(lambda: rig.sent(OrderType.LIMIT))
    first = rig.sent(OrderType.LIMIT)[0].client_order_id
    cancel = rig.broker.cancel

    def fill_then_cancel(cid: str) -> None:
        rig.broker.fill(cid, D(2), D("9.99"))  # the order fills as the cancel goes out
        cancel(cid)  # refused: already terminal

    rig.broker.cancel = fill_then_cancel
    rig.until(lambda: rig.store.journal("cancel-intent", first))
    rig.run(3)
    assert rig.engine.trade(t).state is TradeState.CLOSED
    assert len(rig.sent(OrderType.LIMIT)) == 1 and rig.sent(OrderType.MARKET) == []
    assert rig.broker.positions() == []
    single_sends(rig.broker)


def test_a_partial_fill_racing_the_reprice_cancel_sells_the_rest_once(tmp_path):
    rig = Rig(tmp_path)
    t = rig.position()
    rig.until(lambda: rig.sent(OrderType.LIMIT))
    cancel = rig.broker.cancel

    def fill_then_cancel(cid: str) -> None:
        rig.broker.fill(cid, D(1), D("9.99"))
        cancel(cid)

    rig.broker.cancel = fill_then_cancel
    rig.until(lambda: len(rig.sent(OrderType.LIMIT)) == 2)
    rig.broker.cancel = cancel
    second = rig.sent(OrderType.LIMIT)[1]
    assert second.quantity == D(1)
    rig.broker.fill(second.client_order_id, D(1), D("9.99"))
    rig.run(2)
    assert rig.engine.trade(t).state is TradeState.CLOSED
    single_sends(rig.broker)


def test_an_unknown_place_outcome_is_read_never_resent(tmp_path):
    rig = Rig(tmp_path)
    t = rig.position()
    rig.broker.script("unknown")  # the chase order reached Webull, the reply was lost
    rig.until(lambda: rig.sent(OrderType.LIMIT))
    first = rig.sent(OrderType.LIMIT)[0].client_order_id
    assert rig.engine.order(first).send_state is SendState.UNKNOWN
    rig.run(1)
    # Detail found it working: it is now the chase order, re-priced in its time.
    assert rig.engine.order(first).send_state is SendState.ACKNOWLEDGED
    rig.until(lambda: len(rig.sent(OrderType.LIMIT)) == 2)
    assert rig.broker.orders[first].state is OrderState.CANCELLED
    single_sends(rig.broker)
    assert rig.engine.trade(t).state is TradeState.EXITING


def test_an_unknown_cancel_outcome_waits_for_detail_before_the_next_order(tmp_path):
    rig = Rig(tmp_path)
    rig.position()
    rig.until(lambda: rig.sent(OrderType.LIMIT))
    first = rig.sent(OrderType.LIMIT)[0].client_order_id
    cancel = rig.broker.cancel
    lost: list[str] = []

    def cancel_reply_lost(cid: str) -> None:
        cancel(cid)
        lost.append(cid)
        raise OutcomeUnknown("scripted: cancel reply lost")

    rig.broker.cancel = cancel_reply_lost
    rig.until(lambda: lost)
    assert rig.engine.order(first).cancel_state == "unknown"
    assert len(rig.sent(OrderType.LIMIT)) == 1  # nothing placed on an unknown cancel
    rig.until(lambda: len(rig.sent(OrderType.LIMIT)) == 2)
    assert rig.store.journal("order-observed", first)[-1].body["state"] == OrderState.CANCELLED
    single_sends(rig.broker)


def test_the_cap_reached_ends_with_one_market_in_the_regular_session(tmp_path):
    rig = Rig(tmp_path)
    t = rig.position()
    rig.until(lambda: rig.sent(OrderType.MARKET), limit=ORDERS * 10)
    assert len(rig.sent(OrderType.LIMIT)) == ORDERS
    assert all(r.limit_price == D("9.99") for r in rig.sent(OrderType.LIMIT))
    (market,) = rig.sent(OrderType.MARKET)
    assert market.quantity == D(2) and not rig.working(OrderType.LIMIT)
    ended = rig.store.journal(chase.CHASE_ENDED, t)[0].body
    assert ended["cause"] == chase.EndCause.CAP_REACHED and ended["market"] is True
    rig.broker.fill(market.client_order_id, D(2), D("9.90"))
    rig.run(2)
    assert rig.engine.trade(t).state is TradeState.CLOSED
    single_sends(rig.broker)


def test_a_bid_below_the_floor_ends_the_chase_early(tmp_path):
    rig = Rig(tmp_path)
    t = rig.position()
    rig.until(lambda: rig.sent(OrderType.LIMIT))
    rig.bid = D("9.80")  # 9.79 would be past 0.5 ATR (0.20) under the first bid
    rig.until(lambda: rig.sent(OrderType.MARKET))
    assert len(rig.sent(OrderType.LIMIT)) == 1
    assert rig.store.journal(chase.CHASE_ENDED, t)[0].body["cause"] == chase.EndCause.FLOOR
    single_sends(rig.broker)


def test_no_quote_refuses_the_chase_and_closes_by_market_as_before(tmp_path):
    rig = Rig(tmp_path, quote=False)
    t = rig.position()
    rig.until(lambda: rig.sent(OrderType.MARKET))
    assert rig.sent(OrderType.LIMIT) == []
    (refused,) = rig.store.journal(chase.CHASE_REFUSED, t)
    assert refused.body["cause"] == chase.Refusal.NO_QUOTE


def test_after_the_close_cutoff_no_market_is_sent_during_a_chase(tmp_path):
    rig = Rig(tmp_path)
    t = rig.position()
    rig.until(lambda: rig.sent(OrderType.LIMIT))
    rig.refusal = "at or after 15:59:00 ET"
    rig.run(ORDERS * 10)
    assert rig.sent(OrderType.MARKET) == []
    assert any(n.startswith(f"{Fence.MARKET_CLOSED}:") for n in rig.engine.fences())
    assert len(rig.working(OrderType.LIMIT)) <= 1
    assert rig.engine.trade(t).state is TradeState.EXITING
    single_sends(rig.broker)


def _scenario(tmp_path: Path, **rig_args) -> tuple[list, list[str]]:
    tmp_path.mkdir()
    rig = Rig(tmp_path, **rig_args)
    t = rig.position()
    rig.until(lambda: rig.sent(OrderType.MARKET))
    rig.broker.fill(rig.sent(OrderType.MARKET)[0].client_order_id, D(2), D("9.90"))
    rig.run(2)
    assert rig.engine.trade(t).state is TradeState.CLOSED
    sent = [(r.client_order_id, r.order_type, r.side, r.quantity, r.limit_price, r.stop_price)
            for r in rig.broker.sent]
    return sent, [r.kind for r in rig.store.journal()]


def test_the_switch_off_behaves_exactly_as_without_the_chase(tmp_path):
    off, off_kinds = _scenario(tmp_path / "off", enabled=False)
    none, none_kinds = _scenario(tmp_path / "none", enabled=False, quote=False)
    assert off == none and off_kinds == none_kinds
    assert [s[1] for s in off] == [OrderType.LIMIT, OrderType.STOP_LOSS, OrderType.MARKET]
    assert not any(k.startswith("exit-chase") for k in off_kinds)
