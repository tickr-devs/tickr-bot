"""The bot writes account-v1 after each successful reconciliation (runtime/account.py), and
status carries the daily loss numbers and every tracked position."""

from __future__ import annotations

from dataclasses import replace
from decimal import Decimal as D

from aperture.api import contract as api
from aperture.broker.base import BrokerError
from aperture.broker.fake import FakeBroker
from aperture.engine.core import BrokerView, Fence, TradeState
from aperture.runtime import entries, service
from aperture.runtime.account import NOT_AGREED
from aperture.types import OrderDetail, OrderState, OrderType, Position, Side

from .test_service import CFG, Bot, opened


def with_account(bot: Bot, tmp_path) -> Bot:
    bot.paths = replace(bot.paths, account=tmp_path / "account.json")
    bot.service.paths = bot.paths
    return bot


def read(bot: Bot) -> api.AccountV1:
    return api.read_account(bot.paths.account)


def test_bot_writes_what_reconciliation_read(tmp_path):
    bot = with_account(Bot(tmp_path, broker=FakeBroker()), tmp_path)
    opened(bot)
    assert bot.run(2)
    doc = read(bot)
    assert doc.matches_records and doc.mismatch is None
    assert (doc.equity_usd, doc.buying_power_usd) == (None, None)  # the adapter reads no balance
    assert doc.positions == (api.AccountPosition("ABC", D(2), D(10)),)
    (stop,) = doc.orders
    assert (stop.symbol, stop.side, stop.type, stop.status, stop.quantity, stop.ours) == (
        "ABC", "SELL", "STOP_LOSS", "ACCEPTED", D(2), True)
    assert stop.client_order_id == bot.engine.orders()[-1].client_order_id
    assert doc.read_at <= bot.now
    assert "broker_order_id" not in bot.paths.account.read_text("utf-8")


def test_a_foreign_position_and_order_are_a_mismatch(tmp_path):
    broker = FakeBroker()
    broker.add_foreign_position("XYZ", D(5), D(3))
    broker.orders["manual-1"] = OrderDetail("manual-1", "XYZ", Side.SELL, OrderType.LIMIT, D(5),
                                            D(0), None, OrderState.ACCEPTED, "SUBMITTED")
    bot = with_account(Bot(tmp_path, broker=broker), tmp_path)
    bot.engine.recover()
    bot.service.write_account()
    doc = read(bot)
    assert not doc.matches_records
    assert "foreign-position:XYZ" in doc.mismatch and "foreign-order:manual-1" in doc.mismatch
    (order,) = doc.orders
    assert (order.client_order_id, order.ours) == ("manual-1", False)
    assert doc.positions == (api.AccountPosition("XYZ", D(5), D(3)),)


def test_status_unknown_or_over_protected_never_matches(tmp_path):
    for fence in (Fence.STATUS_UNKNOWN, Fence.OVER_PROTECTED):
        bot = with_account(Bot(tmp_path / fence, broker=FakeBroker()), tmp_path / fence)
        opened(bot)
        bot.engine.recover()
        with bot.store.transaction():
            bot.engine._fence_row(fence, "x1", "test cause")
        bot.service.write_account()
        doc = read(bot)
        assert not doc.matches_records
        assert doc.mismatch == f"{fence}:x1: test cause"


def test_records_disagreeing_without_a_fence_is_a_mismatch_in_plain_words(tmp_path):
    bot = with_account(Bot(tmp_path, broker=FakeBroker()), tmp_path)
    bot.engine.recover()
    bot.engine.broker_view = BrokerView((Position("ABC", D(1), D(5)),), ())
    bot.service.write_account()
    doc = read(bot)
    assert (doc.matches_records, doc.mismatch) == (False, NOT_AGREED)


def test_a_failing_account_write_is_journaled_once_and_the_loop_goes_on(tmp_path):
    bot = with_account(Bot(tmp_path, broker=FakeBroker()), tmp_path)
    bot.paths.account.mkdir()  # the rename onto a directory fails with an OSError
    t = opened(bot)
    assert bot.run(20)  # several reconciles, each write failing
    (row,) = bot.store.journal(service.ACCOUNT_WRITE_FAILED)
    assert row.body["error"]
    assert bot.engine.trade(t).state is TradeState.OPEN
    assert bot.status().bot_state is api.BotState.STOPPED
    bot.paths.account.rmdir()
    bot.service.write_account()
    assert read(bot).matches_records
    bot.paths.account.unlink()
    bot.paths.account.mkdir()
    bot.service.write_account()  # a new stretch of failures: journaled again
    assert len(bot.store.journal(service.ACCOUNT_WRITE_FAILED)) == 2


def test_a_failed_read_writes_nothing(tmp_path):
    broker = FakeBroker()

    def fail():
        raise BrokerError("down")

    broker.open_orders = fail  # type: ignore[method-assign]
    bot = with_account(Bot(tmp_path, broker=broker), tmp_path)
    bot.run(2)
    assert not bot.paths.account.exists()


def test_status_has_the_daily_money_and_every_position(tmp_path):
    bot = Bot(tmp_path, broker=FakeBroker())
    status = bot.service.status()
    assert (status.realized_loss_today_usd, status.open_reservations_usd,
            status.daily_room_usd, status.positions) == (
        D(0), D(0), CFG.risk.max_daily_loss_usd, ())
    opened(bot)
    status = bot.service.status()
    assert status.open_reservations_usd == D(1)
    assert status.daily_room_usd == CFG.risk.max_daily_loss_usd - D(1)
    assert status.positions == (status.position,) and status.position is not None
    assert api.loads(api.StatusV1, api.dumps(status)) == status


def test_unknown_realized_loss_is_null_never_zero(tmp_path, monkeypatch):
    bot = Bot(tmp_path, broker=FakeBroker())
    monkeypatch.setattr(entries, "realized_loss_today", lambda engine, now: None)
    status = bot.service.status()
    assert (status.realized_loss_today_usd, status.daily_room_usd) == (None, None)
    assert status.open_reservations_usd == D(0)
