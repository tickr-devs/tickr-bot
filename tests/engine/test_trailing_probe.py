"""Probe T (engine/trailing_probe.py): Webull's trailing stop as the broker stop."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from tickr import config
from tickr.broker.base import Refused
from tickr.broker.fake import FakeBroker
from tickr.broker.webull import build_order_body, read_order
from tickr.data.base import FakeMarketData
from tickr.engine import trailing_probe as tp
from tickr.types import (
    OrderRequest,
    OrderState,
    OrderType,
    Quote,
    Side,
    TimeInForce,
    TradingSession,
)

ROOT = Path(__file__).resolve().parents[2]
CFG = config.load(ROOT / "config" / "bot.toml")
NOON = datetime(2026, 11, 16, 17, 0, tzinfo=UTC)  # 12:00 ET, Monday
SYMBOL = "ABCD"


class World:
    """Fake clock, broker and quote. `unit` is how the fake Webull reads trailing_stop_step
    (None: Detail shows no stop price); `drift` moves the bid each poll; a working trailing
    stop's stop price follows the highest bid since it was placed and fills when the bid
    reaches it."""

    def __init__(self, tmp_path: Path, *, unit: str | None = tp.UNIT_RATIO,
                 drift: Decimal = Decimal("0.01"), fill_entry: bool = True,
                 position_lag_polls: int = 0) -> None:
        self.at = NOON
        self.broker = FakeBroker()
        self.market = FakeMarketData()
        self.bid, self.ask = Decimal("10.00"), Decimal("10.02")
        self.unit, self.drift, self.fill_entry = unit, drift, fill_entry
        self.lines: list[str] = []
        self.folder = tmp_path / "evidence"
        self.folder.mkdir()
        self.high: dict[str, Decimal] = {}
        self.lag = position_lag_polls
        self.seen: list[list] = []
        real = self.broker.positions
        self._real = real
        self.broker.positions = self._lagged  # type: ignore[method-assign]
        self._quote()

    def _lagged(self):
        self.seen.append(self._real())
        return self.seen[max(0, len(self.seen) - 1 - self.lag)]

    def _quote(self) -> None:
        self.market.set_quote(Quote(SYMBOL, self.bid, self.ask, Decimal(100), Decimal(100),
                                    self.at, self.at))

    def sleep(self, seconds: float) -> None:
        self.at += timedelta(seconds=seconds)
        self.bid += self.drift
        self.ask += self.drift
        self._quote()
        b = self.broker
        for request in b.sent:
            cid = request.client_order_id
            order = b.orders.get(cid)
            if order is None or order.state.terminal:
                continue
            if order.order_type is OrderType.LIMIT and order.side is Side.BUY and self.fill_entry:
                b.fill(cid, order.quantity, self.ask)
            elif order.order_type is OrderType.LIMIT and order.side is Side.SELL:
                b.fill(cid, order.quantity, self.bid)
            elif order.order_type is OrderType.TRAILING_STOP_LOSS:
                step = request.trailing_stop_step
                assert step is not None
                distance = step if self.unit == tp.UNIT_RATIO else step / 100
                high = self.high[cid] = max(self.high.get(cid, self.bid), self.bid)
                stop = (high * (1 - distance)).quantize(Decimal("0.01"))
                if self.bid <= stop:
                    b.fill(cid, order.quantity, self.bid)
                elif self.unit is not None:
                    b.orders[cid] = replace(order, stop_price=stop, row_keys=(
                        "client_order_id", "status", "stop_price", "trailing_stop_step"))
                else:
                    b.orders[cid] = replace(order, row_keys=("client_order_id", "status"))

    def run(self) -> tuple[int, dict]:
        code = tp.run(CFG, self.broker, self.market, SYMBOL, self.folder, now=lambda: self.at,
                      sleep=self.sleep, out=self.lines.append)
        return code, json.loads((self.folder / "summary.json").read_text("utf-8"))


FACTOR = CFG.probe.trailing_unit_factor


@pytest.mark.parametrize("price, below_high, true_unit", [
    ("40", "0", tp.UNIT_RATIO), ("40", "0", tp.UNIT_PERCENT),
    ("40", "0.05", tp.UNIT_RATIO), ("40", "0.05", tp.UNIT_PERCENT),
    ("40", "0.12", tp.UNIT_RATIO), ("40", "0.12", tp.UNIT_PERCENT),
    ("12", "0.03", tp.UNIT_RATIO), ("12", "0.03", tp.UNIT_PERCENT),
    ("12", "0.06", tp.UNIT_RATIO), ("12", "0.06", tp.UNIT_PERCENT),
    ("5", "0.02", tp.UNIT_RATIO), ("5", "0.02", tp.UNIT_PERCENT)])
def test_unit_of_finds_the_unit_when_the_stop_follows_a_high_above_the_bid(
        price, below_high, true_unit) -> None:
    """Fable's rows: the stop trails the high, the bid read with it is a few cents lower."""
    step = Decimal("0.2")
    high = Decimal(price)
    bid = high - Decimal(below_high)
    distance = high * step if true_unit == tp.UNIT_RATIO else high * step / 100
    stop = (high - distance).quantize(Decimal("0.01"))
    if bid <= stop:
        # A 0.2 % trail is a few cents: a bid that far under the high is through the stop, so
        # the stop has triggered and that trigger is the answer (T6), not a unit to read.
        assert true_unit == tp.UNIT_PERCENT
        assert tp.unit_of(step, bid, stop, FACTOR) == tp.UNIT_UNCLEAR
        return
    assert tp.unit_of(step, bid, stop, FACTOR) == true_unit


def test_unit_of_keeps_unclear_for_a_stop_near_neither_and_says_when_none_is_shown() -> None:
    step, bid = Decimal("0.2"), Decimal(10)
    assert tp.unit_of(step, bid, Decimal("9.80"), FACTOR) == tp.UNIT_UNCLEAR  # 2 %: neither
    assert tp.unit_of(step, bid, Decimal("10.50"), FACTOR) == tp.UNIT_UNCLEAR  # above the bid
    assert tp.unit_of(step, bid, None, FACTOR) == tp.UNIT_NOT_SHOWN


def test_rising_price_answers_every_question_and_ends_flat(tmp_path: Path) -> None:
    world = World(tmp_path)
    code, summary = world.run()
    assert code == tp.EXIT_ANSWERED
    assert summary["entry"] == "filled"
    assert summary["t1_accepted"].startswith("accepted")
    assert summary["t2_unit"] == tp.UNIT_RATIO
    assert summary["t3_follows"] == tp.FOLLOWED
    assert summary["t4_open_orders"] == tp.LISTED
    assert summary["t5_cancel"] == tp.CANCELLED
    assert summary["p1_positions_lag_seconds"] == 0 and "poll-granular" in summary["p1_note"]
    fields = [json.loads(line) for line in
              (world.folder / "steps.jsonl").read_text("utf-8").splitlines()]
    (logged,) = [row for row in fields if row["step"] == "trailing-row-fields"]
    assert "stop_price" in logged["fields"]
    assert summary["close"] == "closed" and Decimal(summary["broker_after"]["position"]) == 0
    wide, tight = [r for r in world.broker.sent if r.order_type is OrderType.TRAILING_STOP_LOSS]
    assert (wide.trailing_stop_step, tight.trailing_stop_step) == (Decimal("0.2"),
                                                                    Decimal("0.005"))
    assert all(r.session is TradingSession.CORE for r in world.broker.sent)


def test_a_wide_stop_that_triggers_answers_t6(tmp_path: Path) -> None:
    world = World(tmp_path, unit=tp.UNIT_PERCENT, drift=Decimal("-0.01"))
    code, summary = world.run()
    assert summary["t6_triggers"].startswith(f"{tp.TRIGGERED} (the wide stop itself")
    assert code == tp.EXIT_ANSWERED and summary["close"] == "closed"


def test_tight_stop_triggers_on_a_falling_price(tmp_path: Path) -> None:
    world = World(tmp_path, drift=Decimal("-0.01"))
    code, summary = world.run()
    assert code == tp.EXIT_ANSWERED
    assert summary["t3_follows"] == tp.PRICE_DID_NOT_RISE
    assert summary["t6_triggers"].startswith(tp.TRIGGERED)
    assert summary["close"] == "closed"


def test_percent_unit_is_found_and_the_tight_step_sent_in_it(tmp_path: Path) -> None:
    world = World(tmp_path, unit=tp.UNIT_PERCENT)
    _, summary = world.run()
    assert summary["t2_unit"] == tp.UNIT_PERCENT
    tight = [r for r in world.broker.sent if r.order_type is OrderType.TRAILING_STOP_LOSS][-1]
    assert tight.trailing_stop_step == CFG.probe.trailing_tight_percent


def test_no_stop_price_in_detail_skips_the_trigger_test(tmp_path: Path) -> None:
    world = World(tmp_path, unit=None)
    code, summary = world.run()
    assert summary["t2_unit"] == tp.UNIT_NOT_SHOWN and summary["t6_triggers"] == "not-tested"
    assert code == tp.EXIT_ANSWERED and summary["close"] == "closed"


def test_positions_lag_is_measured(tmp_path: Path) -> None:
    world = World(tmp_path, position_lag_polls=2)
    _, summary = world.run()
    assert summary["p1_positions_lag_seconds"] == 2 * float(CFG.orders.detail_poll_seconds)


def test_unfilled_entry_ends_without_a_stop(tmp_path: Path) -> None:
    world = World(tmp_path, fill_entry=False)
    code, summary = world.run()
    assert summary["entry"] == "not-filled" and "t1_accepted" not in summary
    assert code == tp.EXIT_ANSWERED
    assert not [r for r in world.broker.sent if r.order_type is OrderType.TRAILING_STOP_LOSS]


def test_refused_on_a_busy_account(tmp_path: Path) -> None:
    world = World(tmp_path)
    world.broker.add_foreign_position("XYZ", Decimal(1), Decimal(5))
    code, summary = world.run()
    assert "not flat" in summary["stopped"] and not world.broker.sent


def test_order_body_matches_the_sdk_sample_and_refuses_misfits() -> None:
    request = OrderRequest("probe-x1", SYMBOL, Side.SELL, OrderType.TRAILING_STOP_LOSS,
                           Decimal(1), TimeInForce.DAY, TradingSession.CORE,
                           trailing_stop_step=Decimal("0.2"))
    body = build_order_body(request, CFG.orders.client_order_id_max_length)
    assert (body["order_type"], body["trailing_type"], body["trailing_stop_step"],
            body["trigger_price_type"]) == ("TRAILING_STOP_LOSS", "PERCENTAGE", "0.2", "PRICE")
    assert "stop_price" not in body and "limit_price" not in body
    for bad in (replace(request, trailing_stop_step=None),
                replace(request, order_type=OrderType.STOP_LOSS, stop_price=Decimal(9)),
                replace(request, trailing_stop_step=Decimal(0))):
        with pytest.raises(Refused):
            build_order_body(bad, CFG.orders.client_order_id_max_length)


def test_a_trailing_row_is_read_with_its_step() -> None:
    row = {"client_order_id": "probe-x1", "symbol": SYMBOL, "side": "SELL",
           "order_type": "TRAILING_STOP_LOSS", "status": "SUBMITTED", "total_quantity": "1",
           "filled_quantity": "0", "stop_price": "8.00", "trailing_stop_step": "0.2"}
    detail = read_order(row, lambda _: True, "test")
    assert (detail.order_type, detail.state, detail.stop_price, detail.trailing_stop_step) == (
        OrderType.TRAILING_STOP_LOSS, OrderState.ACCEPTED, Decimal("8.00"), Decimal("0.2"))
