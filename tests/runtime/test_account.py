"""The bot writes account-v1 after each successful reconciliation (runtime/account.py), and
status carries the daily loss numbers and every tracked position."""

from __future__ import annotations

import json
import threading
import time
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal as D

from tickr.api import contract as api
from tickr.broker.base import AccountBalance, BrokerError, BrokerFill, CashTrade
from tickr.broker.fake import FakeBroker
from tickr.engine.core import BrokerView, Fence, TradeState
from tickr.runtime import entries, service
from tickr.runtime.account import (
    ACCOUNT_READS_FAILED,
    BALANCE_READ_FAILED,
    FILLS_READ_FAILED,
    NOT_AGREED,
    RECONCILE_CHECKED,
    AccountReads,
    exact_amount,
    exact_price,
    record_reconcile,
)
from tickr.types import OrderDetail, OrderState, OrderType, Position, Side

from .test_service import CFG, ROOT, Bot, opened


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
    assert (doc.equity_usd, doc.buying_power_usd) == (None, None)  # no balance scripted
    assert doc.balance_read_at is None and doc.cash_usd is None
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


def _checked(bot: Bot) -> list[tuple[bool, str | None]]:
    return [(r.body["matches_records"], r.body["mismatch"])
            for r in bot.store.journal(RECONCILE_CHECKED)]


def test_reconcile_rows_record_each_change_at_once_and_a_same_answer_once_per_interval(tmp_path):
    bot = Bot(tmp_path, broker=FakeBroker())
    interval = timedelta(seconds=float(CFG.orders.reconcile_proof_seconds))
    bot.engine.recover()
    assert record_reconcile(bot.engine, False) is False
    assert _checked(bot) == [(True, None)]
    bot.now += interval - timedelta(seconds=1)
    assert bot.engine.reconcile()
    record_reconcile(bot.engine, False)
    assert len(_checked(bot)) == 1  # same answer, too soon
    # A fence is a confirmed mismatch: recorded at once.
    with bot.store.transaction():
        bot.engine._fence_row(Fence.FOREIGN_ORDER, "m1", "open order not in our records")
    assert record_reconcile(bot.engine, False) is False
    assert _checked(bot)[-1] == (False, f"{Fence.FOREIGN_ORDER}:m1: open order not in our records")
    with bot.store.transaction():
        bot.store.execute("DELETE FROM fences")
    assert bot.engine.reconcile()
    record_reconcile(bot.engine, False)
    assert _checked(bot)[-1] == (True, None) and len(_checked(bot)) == 3
    bot.now += interval
    assert bot.engine.reconcile()
    record_reconcile(bot.engine, False)
    assert len(_checked(bot)) == 4  # same answer, the interval passed


def test_a_disagreement_without_a_fence_is_recorded_once_two_passes_in_a_row_show_it(tmp_path):
    bot = Bot(tmp_path, broker=FakeBroker())
    bot.engine.recover()
    disagreed = record_reconcile(bot.engine, False)
    bot.engine.broker_view = BrokerView((Position("ABC", D(1), D(5)),), ())
    disagreed = record_reconcile(bot.engine, disagreed)
    assert disagreed and _checked(bot) == [(True, None)]  # one pass alone: not recorded
    disagreed = record_reconcile(bot.engine, disagreed)  # still there on the next pass
    assert _checked(bot) == [(True, None), (False, NOT_AGREED)]


def test_a_fill_seen_inside_a_reconcile_pass_never_records_a_mismatch(tmp_path):
    """Reconcile reads open orders, then its step sees the entry fill and places the stop: the
    stop is live in the records but not in that read, for one pass only."""
    from tickr.api import cleanday
    from tickr.calendar import SessionCalendar

    bot = Bot(tmp_path, broker=FakeBroker())
    bot.engine.recover()
    disagreed = record_reconcile(bot.engine, False)
    t = bot.engine.open_trade("ABC", D(2), D(10), D("9.80"), D(1))
    bot.broker.fill(bot.engine.orders(t)[0].client_order_id, D(2), D(10))
    seen = []
    for _ in range(4):
        bot.now += timedelta(seconds=float(CFG.orders.reconcile_proof_seconds))
        assert bot.engine.reconcile()
        disagreed = record_reconcile(bot.engine, disagreed)
        seen.append(disagreed)
    assert seen[0] is True and not any(seen[1:])  # the transient pass happened
    assert {matches for matches, _ in _checked(bot)} == {True}
    with bot.store.transaction():  # the engine's run, as the bot's start would record it
        bot.store.execute(
            "INSERT INTO runs(run_id, started_at, environment, config_digest, strategy, "
            "strategy_version, code_version) VALUES (?, ?, ?, 'd', 's', '1', 'c')",
            (bot.engine.run_id, bot.store.stamp(), CFG.environment_name))
    day = bot.now.date()
    calendar = SessionCalendar.load((ROOT / CFG.session.calendar_file).parent)
    verdict = cleanday.clean_days(bot.store, CFG.environment_name, [day], day, calendar,
                                  CFG.orders.reconcile_max_gap_seconds)[day]
    # The startup row and three of the four passes: the transient one wrote nothing.
    assert verdict.causes == () and verdict.reconcile_rows == 4


def test_the_loop_records_reconcile_rows_but_not_after_a_failed_read(tmp_path):
    bot = Bot(tmp_path, broker=FakeBroker())
    assert bot.run(3)
    assert _checked(bot) == [(True, None)]  # at startup; the next ones are within the interval
    broker = FakeBroker()

    def fail():
        raise BrokerError("down")

    broker.open_orders = fail  # type: ignore[method-assign]
    failing = Bot(tmp_path / "failing", broker=broker)
    failing.run(3)
    assert _checked(failing) == []


FUNDS = AccountBalance(net_liquidation=D("1000.50"), cash=D("980.50"), market_value=D("20"),
                       unrealized_profit_loss=D("0.40"), day_profit_loss=D("-1.10"),
                       day_buying_power=D("4002"), overnight_buying_power=D("2001"),
                       maintenance_margin=D("5"), margin_calls=0, day_trades_left=None)


def with_reads(bot: Bot, broker: FakeBroker) -> AccountReads:
    reads = AccountReads(broker, float(CFG.orders.balance_read_seconds), lambda: bot.now)
    bot.service.account_reads = reads
    return reads


def failure_rows(bot: Bot, kind: str) -> list:
    return bot.engine.store.query("SELECT body FROM journal WHERE kind = ?", (kind,))


def test_the_balance_and_account_kind_reach_the_document(tmp_path):
    broker = FakeBroker(funds=FUNDS, account_label="Individual Margin")
    bot = with_account(Bot(tmp_path, broker=broker), tmp_path)
    with_reads(bot, broker).read_once()
    bot.engine.recover()
    bot.service.write_account()
    doc = read(bot)
    assert (doc.equity_usd, doc.buying_power_usd) == (D("1000.50"), D("4002"))
    assert (doc.cash_usd, doc.positions_value_usd) == (D("980.50"), D("20"))
    assert (doc.unrealized_pl_usd, doc.day_pl_usd) == (D("0.40"), D("-1.10"))
    assert (doc.overnight_buying_power_usd, doc.maintenance_margin_usd) == (D("2001"), D("5"))
    assert (doc.margin_calls, doc.day_trades_left) == (0, None)
    assert doc.account_label == "Individual Margin"
    assert doc.balance_read_at == bot.now


def test_without_display_reads_the_balance_is_null(tmp_path):
    bot = with_account(Bot(tmp_path, broker=FakeBroker(funds=FUNDS)), tmp_path)
    bot.engine.recover()
    bot.service.write_account()
    doc = read(bot)
    assert doc.equity_usd is None and doc.fills == () and doc.account_label is None


def test_a_failed_balance_read_keeps_the_last_one_and_is_journaled_once(tmp_path):
    broker = FakeBroker(funds=FUNDS)
    bot = with_account(Bot(tmp_path, broker=broker), tmp_path)
    reads = with_reads(bot, broker)
    bot.engine.recover()
    reads.read_once()
    good = bot.now
    broker.funds = None  # every read fails from now on
    for _ in range(3):
        bot.now += timedelta(seconds=float(CFG.orders.balance_read_seconds))
        reads.read_once()
        bot.service.write_account()
    doc = read(bot)
    assert doc.equity_usd == D("1000.50") and doc.balance_read_at == good
    assert doc.read_at == bot.now  # positions and orders stay current
    assert len(failure_rows(bot, BALANCE_READ_FAILED)) == 1
    assert not bot.engine.fences()  # display only: nothing blocks trading
    broker.funds = FUNDS
    reads.read_once()
    broker.funds = None
    reads.read_once()
    bot.service.write_account()
    assert len(failure_rows(bot, BALANCE_READ_FAILED)) == 2  # a new stretch of failures


def test_an_open_limit_order_shows_its_limit_price(tmp_path):
    broker = FakeBroker()
    broker.orders["manual-2"] = OrderDetail("manual-2", "XYZ", Side.BUY, OrderType.LIMIT, D(5),
                                            D(0), None, OrderState.ACCEPTED, "SUBMITTED",
                                            limit_price=D("4.25"))
    bot = with_account(Bot(tmp_path, broker=broker), tmp_path)
    bot.engine.recover()
    bot.service.write_account()
    (order,) = read(bot).orders
    assert order.limit_price == D("4.25")


def test_an_older_bots_file_reads_as_no_balance(tmp_path):
    broker = FakeBroker(funds=FUNDS)
    bot = with_account(Bot(tmp_path, broker=broker), tmp_path)
    with_reads(bot, broker).read_once()
    bot.engine.recover()
    bot.service.write_account()
    raw = json.loads(bot.paths.account.read_text("utf-8"))
    for key in ("balance_read_at", "cash_usd", "positions_value_usd", "unrealized_pl_usd",
                "day_pl_usd", "overnight_buying_power_usd", "maintenance_margin_usd",
                "margin_calls", "day_trades_left", "account_label", "fills_read_at", "fills"):
        del raw[key]
    raw["equity_usd"] = raw["buying_power_usd"] = None
    bot.paths.account.write_text(json.dumps(raw), "utf-8")
    doc = read(bot)
    assert doc.balance_read_at is None and doc.cash_usd is None and doc.fills == ()


def test_a_failed_accounts_read_never_blocks_the_balance(tmp_path):
    class NoAccounts(FakeBroker):
        def account(self):
            raise BrokerError("accounts route paused")

    broker = NoAccounts(funds=FUNDS)
    bot = with_account(Bot(tmp_path, broker=broker), tmp_path)
    with_reads(bot, broker).read_once()
    bot.engine.recover()
    bot.service.write_account()
    doc = read(bot)
    assert doc.equity_usd == D("1000.50") and doc.account_label is None
    assert failure_rows(bot, BALANCE_READ_FAILED) == []


def test_positions_values_and_todays_fills_reach_the_document(tmp_path):
    broker = FakeBroker(funds=FUNDS)
    broker.holdings["XYZ"] = Position("XYZ", D(5), D(3), last_price=D("3.10"),
                                      market_value=D("15.50"), unrealized_profit_loss=D("0.50"),
                                      day_profit_loss=D("0.50"))
    broker.orders["manual-3"] = OrderDetail("manual-3", "XYZ", Side.BUY, OrderType.LIMIT, D(5),
                                            D(5), D(3), OrderState.FILLED, "FILLED")
    bot = with_account(Bot(tmp_path, broker=broker), tmp_path)
    with_reads(bot, broker).read_once()
    bot.engine.recover()
    bot.service.write_account()
    doc = read(bot)
    (position,) = doc.positions
    assert (position.last_price, position.market_value_usd) == (D("3.10"), D("15.50"))
    assert (position.unrealized_pl_usd, position.day_pl_usd) == (D("0.50"), D("0.50"))
    (fill,) = doc.fills
    assert (fill.client_order_id, fill.symbol, fill.quantity, fill.price, fill.ours) == (
        "manual-3", "XYZ", D(5), D(3), False)
    assert doc.fills_read_at == bot.now


def test_a_failed_fills_read_never_blocks_the_balance(tmp_path):
    class NoHistory(FakeBroker):
        def fills_today(self, day):
            raise BrokerError("history route paused")

    broker = NoHistory(funds=FUNDS)
    bot = with_account(Bot(tmp_path, broker=broker), tmp_path)
    with_reads(bot, broker).read_once()
    bot.engine.recover()
    bot.service.write_account()
    doc = read(bot)
    assert doc.equity_usd == D("1000.50") and doc.fills == () and doc.fills_read_at is None
    assert len(failure_rows(bot, FILLS_READ_FAILED)) == 1


def test_the_loop_never_waits_on_a_slow_display_read(tmp_path):
    release = threading.Event()
    entered = threading.Event()

    class SlowBalance(FakeBroker):
        def balance(self):
            entered.set()
            release.wait()  # a broker route that hangs
            return FUNDS

    broker = SlowBalance()
    bot = with_account(Bot(tmp_path, broker=broker), tmp_path)
    reads = with_reads(bot, broker)
    reads.start()
    try:
        assert entered.wait(timeout=WAIT_SECONDS)
        bot.engine.recover()
        bot.service.write_account()  # returns while the read still hangs
        assert read(bot).equity_usd is None
        release.set()
    finally:
        release.set()
        reads.stop(WAIT_SECONDS)


def test_an_unexpected_error_in_the_display_thread_is_journaled_and_it_goes_on(tmp_path):
    class Broken(FakeBroker):
        calls = 0

        def balance(self):
            Broken.calls += 1
            raise ValueError("bug")

    broker = Broken()
    bot = with_account(Bot(tmp_path, broker=broker), tmp_path)
    reads = AccountReads(broker, 0.0, lambda: bot.now)  # no pause between rounds
    bot.service.account_reads = reads
    reads.start()
    try:
        deadline = time.monotonic() + WAIT_SECONDS
        while Broken.calls < 2 and time.monotonic() < deadline:
            time.sleep(0.01)
    finally:
        reads.stop(WAIT_SECONDS)
    assert Broken.calls >= 2  # the thread survived the first error
    bot.engine.recover()
    bot.service.write_account()
    assert len(failure_rows(bot, ACCOUNT_READS_FAILED)) == 1


# Test data: the most a test waits for the display thread, seconds.
WAIT_SECONDS = 5


def closed_trade(bot: Bot) -> str:
    t = opened(bot)
    bot.service.state = api.BotState.RUNNING
    bot.command(api.CommandKind.FLATTEN)
    assert bot.run(10)
    return t


def test_status_result_after_the_brokers_fees_on_todays_fills(tmp_path):
    bot = Bot(tmp_path)
    t = closed_trade(bot)
    filled = [o for o in bot.engine.orders(t) if o.filled_quantity > 0]
    fees = {o.client_order_id: D("0.07") if o.side is Side.SELL else D(0) for o in filled}

    class WithFees(FakeBroker):
        def fills_today(self, day):
            return [replace(f, fees=fees.get(f.client_order_id)) for f in super().fills_today(day)]

    reads = AccountReads(WithFees(funds=FUNDS, orders=bot.broker.orders),
                         float(CFG.orders.balance_read_seconds), lambda: bot.now)
    bot.service.account_reads = reads
    status = bot.service.status()
    assert (status.fees_today_usd, status.result_today_net_usd) == (None, None)  # not read yet
    reads.read_once()
    status = bot.service.status()
    assert status.fees_today_usd == D("0.07")
    assert status.result_today_net_usd == status.result_today_gross_usd - D("0.07")


def test_fees_stay_unknown_while_a_fill_is_missing_from_the_list(tmp_path):
    bot = Bot(tmp_path)
    t = closed_trade(bot)
    exit_id = [o for o in bot.engine.orders(t) if o.filled_quantity > 0][-1].client_order_id

    class Lagging(FakeBroker):
        def fills_today(self, day):
            return [replace(f, fees=D(0)) for f in super().fills_today(day)
                    if f.client_order_id != exit_id]

    reads = AccountReads(Lagging(funds=FUNDS, orders=bot.broker.orders),
                         float(CFG.orders.balance_read_seconds), lambda: bot.now)
    bot.service.account_reads = reads
    reads.read_once()
    status = bot.service.status()
    assert (status.fees_today_usd, status.result_today_net_usd) == (None, None)


def test_status_carries_the_brokers_own_result_once_read_after_the_last_close(tmp_path):
    bot = Bot(tmp_path)
    closed_trade(bot)
    reads = AccountReads(FakeBroker(funds=FUNDS), float(CFG.orders.balance_read_seconds),
                         lambda: bot.now)
    bot.service.account_reads = reads
    assert bot.service.status().result_today_broker_usd is None  # not read yet
    reads.read_once()
    # Day P&L -1.10 less the unrealized 0.40: what the broker realized today.
    assert bot.service.status().result_today_broker_usd == D("-1.50")


def test_a_balance_read_before_the_last_close_is_not_todays_result(tmp_path):
    bot = Bot(tmp_path)
    reads = AccountReads(FakeBroker(funds=FUNDS), float(CFG.orders.balance_read_seconds),
                         lambda: bot.now - timedelta(hours=1))
    bot.service.account_reads = reads
    reads.read_once()
    closed_trade(bot)
    assert bot.service.status().result_today_broker_usd is None


def test_exact_amounts_and_trade_result_from_the_cash_activities(tmp_path):
    bot = Bot(tmp_path)
    t = closed_trade(bot)
    filled = [o for o in bot.engine.orders(t) if o.filled_quantity > 0]
    at = bot.now
    fees = {o.client_order_id: D(0) for o in filled}

    class Exact(FakeBroker):
        def fills_today(self, day):
            return [replace(f, fees=fees.get(f.client_order_id), filled_at=at)
                    for f in super().fills_today(day)]

    cash = [CashTrade(o.symbol, str(o.side), D("-20.0004") if o.side is Side.BUY
                      else D("19.9901"), at) for o in filled]
    broker = Exact(funds=FUNDS, orders=bot.broker.orders, cash=cash)
    reads = AccountReads(broker, float(CFG.orders.balance_read_seconds), lambda: bot.now)
    bot.service.account_reads = reads
    assert bot.service.status().trades_today[0].result_exact_usd is None  # nothing read
    reads.read_once()
    (trade,) = bot.service.status().trades_today
    assert trade.result_exact_usd == D("-0.01")  # -0.0103 to the cent
    broker.cash = cash[:1]  # the exit's activity not listed yet: back to the order prices
    reads.read_once()
    assert bot.service.status().trades_today[0].result_exact_usd is None


def test_exact_price_from_the_amount_and_fees():
    buy = BrokerFill("e", "CYAB", "BUY", "LIMIT", D(918), D("0.23"), None, D(0))
    sell = BrokerFill("s", "CYAB", "SELL", "STOP_LOSS", D(918), D("0.18"), None, D("0.19"))
    assert exact_price(buy, D("-208.5696")) == D("0.2272")
    assert exact_price(sell, D("166.3352")) == D("0.1814")
    assert exact_price(sell, None) is None


def test_a_cash_row_for_only_part_of_a_fill_is_not_the_fills_amount():
    # An order of 918 filled in two executions, one cash row each (the shape is unproven):
    # the row at the order's fill time covers 418 shares only.
    fill = BrokerFill("e", "CYAB", "BUY", "LIMIT", D(918), D("0.23"),
                      datetime(2026, 11, 16, 17, 36, 4, tzinfo=UTC), D(0))
    rows = (CashTrade("CYAB", "BUY", D("-113.6000"), datetime(2026, 11, 16, 17, 36, 3, tzinfo=UTC)),
            CashTrade("CYAB", "BUY", D("-94.9696"), datetime(2026, 11, 16, 17, 36, 4, tzinfo=UTC)))
    assert exact_amount(fill, rows, (fill,)) is None  # two rows for one filled order
    assert exact_amount(fill, rows[1:], (fill,)) is None  # 94.9696 / 918 = 0.10, not 0.23
    whole = (CashTrade("CYAB", "BUY", D("-208.5696"), fill.filled_at),)
    assert exact_amount(fill, whole, (fill,)) == D("-208.5696")


def test_a_cash_part_too_small_to_move_the_price_is_caught_by_the_row_count():
    at = datetime(2026, 11, 16, 17, 36, 4, tzinfo=UTC)
    fill = BrokerFill("e", "CYAB", "BUY", "LIMIT", D(918), D("0.23"), at, D(0))
    # 900 of 918 shares at the fill time: the price it implies still rounds to 0.23.
    rows = (CashTrade("CYAB", "BUY", D("-4.1994"), at - timedelta(seconds=1)),
            CashTrade("CYAB", "BUY", D("-209.9700"), at))
    assert exact_amount(fill, rows[1:], (fill,)) == D("-209.9700")  # alone: undetectable
    assert exact_amount(fill, rows, (fill,)) is None  # the day lists two rows for one order
