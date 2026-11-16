"""Probe F7 (engine/session_probe.py): extended-hours acceptance, fill, trigger and close."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from tickr import config
from tickr.broker.base import BrokerError, OutcomeUnknown, Refused
from tickr.broker.fake import FakeBroker
from tickr.broker.webull import build_order_body, read_order
from tickr.calendar import SessionCalendar
from tickr.data.base import FakeMarketData
from tickr.engine import session_probe as sp
from tickr.engine.manual import ManualRefused
from tickr.types import (
    OrderDetail,
    OrderRequest,
    OrderType,
    Position,
    Quote,
    Side,
    TimeInForce,
    TradingSession,
)

ROOT = Path(__file__).resolve().parents[2]
CFG = config.load(ROOT / "config" / "bot.toml")
CALENDAR = SessionCalendar.load((ROOT / CFG.session.calendar_file).parent)
POST_TIME = datetime(2026, 11, 16, 22, 0, tzinfo=UTC)  # 17:00 ET, Monday
SYMBOL = "ABCD"

Hook = Callable[["World"], None]


class World:
    """A fake clock, broker and quote; `hook` runs on every sleep, as the market would."""

    def __init__(self, tmp_path: Path, hook: Hook | None = None) -> None:
        self.at = POST_TIME
        self.broker = FakeBroker()
        self.market = FakeMarketData()
        self.bid, self.ask = Decimal("10.00"), Decimal("10.02")
        self.hook = hook
        self.lines: list[str] = []
        self.folder = tmp_path / "evidence"
        self.folder.mkdir()
        self.history: list[tuple[datetime, list[Position]]] = []
        self._quote()

    def lag_positions(self, seconds: float) -> None:
        """The positions read shows the broker's holdings as they were `seconds` ago."""
        real = self.broker.positions

        def lagging() -> list[Position]:
            seen = [p for at, p in self.history if at <= self.at - timedelta(seconds=seconds)]
            return seen[-1] if seen else []

        self._real_positions = real
        self.broker.positions = lagging  # type: ignore[method-assign]

    def _quote(self) -> None:
        self.market.set_quote(Quote(SYMBOL, self.bid, self.ask, Decimal(100), Decimal(100),
                                    self.at, self.at))

    def now(self) -> datetime:
        return self.at

    def sleep(self, seconds: float) -> None:
        self.at += timedelta(seconds=seconds)
        if self.hook is not None:
            self.hook(self)
        real = getattr(self, "_real_positions", self.broker.positions)
        self.history.append((self.at, [replace(p) for p in real()]))
        self._quote()

    def working(self, side: Side, order_type: OrderType) -> list[str]:
        return [o.client_order_id for o in self.broker.open_orders()
                if o.side is side and o.order_type is order_type]

    def run(self) -> tuple[int, dict]:
        code = sp.run(CFG, self.broker, self.market, SYMBOL, self.folder, now=self.now,
                      sleep=self.sleep, out=self.lines.append, session=sp.POST,
                      session_end=POST_TIME + timedelta(hours=3))
        return code, json.loads((self.folder / "summary.json").read_text("utf-8"))

    def steps(self) -> list[dict]:
        return [json.loads(line) for line in
                (self.folder / "steps.jsonl").read_text("utf-8").splitlines()]


def fill_entry_and_close(world: World) -> None:
    for cid in world.working(Side.BUY, OrderType.LIMIT):
        world.broker.fill(cid, Decimal(1), world.ask)
    for cid in world.working(Side.SELL, OrderType.LIMIT):
        world.broker.fill(cid, Decimal(1), world.bid)


def test_check_time_only_in_extended_sessions_with_room_left() -> None:
    assert sp.check_time(CFG, CALENDAR, POST_TIME)[0] == sp.POST
    pre = datetime(2026, 11, 17, 10, 0, tzinfo=UTC)  # 05:00 ET
    assert sp.check_time(CFG, CALENDAR, pre) == (sp.PRE, CALENDAR.day_at(pre).regular_open)
    for refused in (datetime(2026, 11, 16, 16, 0, tzinfo=UTC),  # 11:00 ET, regular session
                    datetime(2026, 11, 17, 0, 50, tzinfo=UTC),  # 19:50 ET, 10 min left
                    datetime(2026, 11, 15, 22, 0, tzinfo=UTC)):  # Sunday
        with pytest.raises(ManualRefused):
            sp.check_time(CFG, CALENDAR, refused)


def test_answers_every_question_and_records_each_order_before_sending(tmp_path: Path) -> None:
    def market(world: World) -> None:
        fill_entry_and_close(world)
        for cid in world.working(Side.SELL, OrderType.STOP_LOSS):
            world.broker.fill(cid, Decimal(1), world.bid - Decimal("0.02"))

    world = World(tmp_path, market)
    code, summary = world.run()
    assert code == sp.EXIT_ANSWERED
    assert summary["acceptance"] == {"stop-all": "accepted (SUBMITTED)",
                                     "stop-limit-all": "accepted (SUBMITTED)",
                                     "stop-core": "accepted (SUBMITTED)"}
    assert summary["entry"] == sp.FILLED
    assert summary["trigger-stop-all"].startswith(sp.TRIGGERED)
    assert summary["trigger-stop-limit-all"] == sp.NOT_TESTED  # nothing held any more
    assert summary["close"] == sp.CLOSED
    assert summary["broker_after"] == {"readable": True, "position": "0", "detail_says": "0",
                                       "working_probe_orders": []}
    intents = [s["client_order_id"] for s in world.steps() if s["step"] == "intent"]
    assert intents == [r.client_order_id for r in world.broker.sent]
    assert all(cid.startswith(CFG.orders.probe_client_order_id_prefix) for cid in intents)
    assert len(set(intents)) == len(intents)
    assert not world.broker.open_orders() and not world.broker.positions()
    far = world.broker.sent[0]
    assert far.stop_price == Decimal("12.02") and far.side is Side.BUY
    assert world.broker.sent[1].order_type is OrderType.STOP_LOSS_LIMIT
    assert world.broker.sent[2].session is TradingSession.CORE


def test_unfilled_entry_is_cancelled_and_nothing_is_triggered(tmp_path: Path) -> None:
    world = World(tmp_path)
    code, summary = world.run()
    assert code == sp.EXIT_ANSWERED
    assert summary["entry"] == sp.NOT_FILLED
    assert summary["trigger-stop-all"] == sp.NOT_TESTED
    assert summary["close"] == sp.CLOSED
    assert not world.broker.open_orders()


def test_refused_stop_type_is_not_trigger_tested(tmp_path: Path) -> None:
    def market(world: World) -> None:
        fill_entry_and_close(world)
        for cid in world.working(Side.SELL, OrderType.STOP_LOSS_LIMIT):
            world.broker.fill(cid, Decimal(1), world.bid - Decimal("0.02"))

    world = World(tmp_path, market)
    world.broker.script("refuse")  # the first order: BUY STOP_LOSS session ALL
    _, summary = world.run()
    assert summary["acceptance"]["stop-all"] == sp.REFUSED
    assert summary["trigger-stop-all"] == sp.NOT_TESTED
    assert summary["trigger-stop-limit-all"].startswith(sp.TRIGGERED)


def test_stop_that_keeps_working_below_its_price_is_reported(tmp_path: Path) -> None:
    def market(world: World) -> None:
        fill_entry_and_close(world)
        if world.working(Side.SELL, OrderType.STOP_LOSS):
            world.bid, world.ask = Decimal("9.80"), Decimal("9.82")

    world = World(tmp_path, market)
    _, summary = world.run()
    assert summary["trigger-stop-all"] == sp.NOT_TRIGGERED_PRICE_PASSED
    assert summary["close"] == sp.CLOSED


def test_close_that_does_not_fill_asks_the_operator(tmp_path: Path) -> None:
    def market(world: World) -> None:
        for cid in world.working(Side.BUY, OrderType.LIMIT):
            world.broker.fill(cid, Decimal(1), world.ask)

    world = World(tmp_path, market)
    code, summary = world.run()
    assert code == sp.EXIT_OPERATOR
    assert summary["close"] == sp.NOT_CLOSED
    assert any(line.startswith("OPERATOR") for line in world.lines)


def test_refuses_on_an_account_that_is_not_flat(tmp_path: Path) -> None:
    world = World(tmp_path)
    world.broker.holdings["XYZ"] = Position("XYZ", Decimal(5), Decimal(3))
    code, summary = world.run()
    assert code == sp.EXIT_ANSWERED
    assert "not flat" in summary["stopped"]
    assert world.broker.sent == []


def test_refuses_on_a_stale_quote(tmp_path: Path) -> None:
    world = World(tmp_path)
    world.at += timedelta(minutes=5)  # the quote stays at the old time
    _, summary = world.run()
    assert "not live" in summary["stopped"]
    assert world.broker.sent == []


def test_order_body_and_row_of_a_stop_limit() -> None:
    request = OrderRequest("probe-x1", SYMBOL, Side.SELL, OrderType.STOP_LOSS_LIMIT,
                           Decimal(1), TimeInForce.DAY, TradingSession.ALL,
                           limit_price=Decimal("9.80"), stop_price=Decimal("9.99"))
    body = build_order_body(request, CFG.orders.client_order_id_max_length)
    assert (body["order_type"], body["stop_price"], body["limit_price"],
            body["support_trading_session"]) == ("STOP_LOSS_LIMIT", "9.99", "9.80", "ALL")
    with pytest.raises(Refused):
        build_order_body(replace(request, limit_price=None),
                         CFG.orders.client_order_id_max_length)
    row = read_order({"client_order_id": "probe-x1", "symbol": SYMBOL, "side": "SELL",
                      "order_type": "STOP_LOSS_LIMIT", "status": "SUBMITTED",
                      "total_quantity": "1", "filled_quantity": "0", "stop_price": "9.99",
                      "limit_price": "9.80"}, lambda _: True, "test")
    assert (row.order_type, row.stop_price, row.limit_price) == (
        OrderType.STOP_LOSS_LIMIT, Decimal("9.99"), Decimal("9.80"))


def test_a_probe_order_left_working_stops_everything_after_it(tmp_path: Path) -> None:
    world = World(tmp_path)

    def cancel_lost(client_order_id: str) -> None:
        raise OutcomeUnknown("scripted: the cancel never arrived")

    world.broker.cancel = cancel_lost  # type: ignore[method-assign]
    code, summary = world.run()
    assert code == sp.EXIT_OPERATOR
    assert "still SUBMITTED" in summary["unresolved"]
    assert summary["close"] == sp.NOT_SENT_UNRESOLVED
    assert summary["broker_after"]["working_probe_orders"] == ["probe-x2200001"]
    assert len(world.broker.sent) == 1  # nothing after the unresolved order


def _sells(world: World) -> list[OrderRequest]:
    return [r for r in world.broker.sent if r.side is Side.SELL]


def test_a_sell_stop_whose_cancel_does_not_take_blocks_every_later_sell(tmp_path: Path) -> None:
    world = World(tmp_path, fill_entry_and_close)
    real_cancel = world.broker.cancel

    def cancel(client_order_id: str) -> None:
        order = world.broker.orders[client_order_id]
        if order.side is Side.SELL and order.order_type is not OrderType.LIMIT:
            return  # the broker says nothing and the stop keeps working
        real_cancel(client_order_id)

    world.broker.cancel = cancel  # type: ignore[method-assign]
    code, summary = world.run()
    assert code == sp.EXIT_OPERATOR
    assert len(_sells(world)) == 1  # one stop; no second stop and no close for the same share
    assert summary["close"] == sp.NOT_SENT_UNRESOLVED
    assert summary["broker_after"]["position"] == "1"


def test_a_sell_stop_detail_never_shows_blocks_every_later_sell(tmp_path: Path) -> None:
    world = World(tmp_path, fill_entry_and_close)
    real_detail = world.broker.order_detail

    def detail(client_order_id: str) -> OrderDetail:
        if world.broker.orders[client_order_id].side is Side.SELL:
            raise BrokerError("scripted: Detail read failed")
        return real_detail(client_order_id)

    world.broker.order_detail = detail  # type: ignore[method-assign]
    code, summary = world.run()
    assert code == sp.EXIT_OPERATOR
    assert "never shown" in summary["unresolved"]
    assert len(_sells(world)) == 1


def test_a_fill_detail_never_showed_stops_before_any_sell(tmp_path: Path) -> None:
    def market(world: World) -> None:
        # The broker holds a share that no Detail row of this run explains.
        if world.working(Side.BUY, OrderType.LIMIT) and SYMBOL not in world.broker.holdings:
            world.broker.add_foreign_position(SYMBOL, Decimal(1), world.ask)

    world = World(tmp_path, market)
    code, summary = world.run()
    assert code == sp.EXIT_OPERATOR
    assert "holds 1 ABCD, Detail of this run's orders says 0" in summary["unresolved"]
    assert _sells(world) == []


def test_positions_lagging_the_entry_fill_are_waited_for(tmp_path: Path) -> None:
    world = World(tmp_path, fill_entry_and_close)
    world.lag_positions(6)
    world.broker.script("refuse", "refuse", "refuse")  # no stop type: straight to the close
    code, summary = world.run()
    assert summary["entry"] == sp.FILLED
    assert [(r.order_type, r.quantity) for r in _sells(world)] == [(OrderType.LIMIT, 1)]
    assert summary["close"] == sp.CLOSED and code == sp.EXIT_ANSWERED


def test_positions_lagging_too_long_never_count_as_flat(tmp_path: Path) -> None:
    world = World(tmp_path, fill_entry_and_close)
    world.lag_positions(CFG.probe.extended_position_agree_seconds + 30)
    world.broker.script("refuse", "refuse", "refuse")
    code, summary = world.run()
    assert code == sp.EXIT_OPERATOR
    assert "holds 0 ABCD, Detail of this run's orders says 1" in summary["unresolved"]
    assert _sells(world) == []  # never a SELL on a disagreement


def test_positions_lagging_a_triggered_stop_send_no_second_sell(tmp_path: Path) -> None:
    def market(world: World) -> None:
        fill_entry_and_close(world)
        for cid in world.working(Side.SELL, OrderType.STOP_LOSS):
            world.broker.fill(cid, Decimal(1), world.bid - Decimal("0.02"))

    world = World(tmp_path, market)
    world.lag_positions(6)
    code, summary = world.run()
    assert summary["trigger-stop-all"].startswith(sp.TRIGGERED)
    assert summary["trigger-stop-limit-all"] == sp.NOT_TESTED
    assert len(_sells(world)) == 1
    assert code == sp.EXIT_ANSWERED
