"""Exact fill prices on the trading path (runtime/account.py `record_exact_fills`): the loop
records a finished order's exact average fill price from the account's cash activity of the
fill, once, and the realized results (closed trades, the daily loss) count it. The broker's
order rows round sub-dollar prices to cents (probe 2026-11-16, CYAB)."""

from __future__ import annotations

import sqlite3
from dataclasses import replace
from decimal import Decimal as D

import pytest

from tickr.broker.base import BrokerFill, CashTrade
from tickr.broker.fake import FakeBroker
from tickr.engine.core import EXACT_FILL_PRICE, OrderRow
from tickr.runtime import entries
from tickr.runtime.account import AccountReads, Balances, exact_fill, record_exact_fills
from tickr.store import MIGRATIONS, Store
from tickr.types import OrderState, Side

from .test_account import FUNDS, closed_trade
from .test_service import CFG, Bot

# Test data: the entry (2 shares, order rows say 10) really filled at 10.004 and the exit at
# 9.995 after a 0.01 fee; exact gross result (9.995 - 10.004) x 2 = -0.018.
BUY_NET = D("-20.008")
SELL_NET = D("19.98")
SELL_FEE = D("0.01")


class Exact(FakeBroker):
    """Today's fills with a fill time and fees, as the order history gives them."""

    quantity: dict[str, D]

    def __init__(self, bot: Bot, cash: list[CashTrade]) -> None:
        super().__init__(funds=FUNDS, orders=bot.broker.orders, cash=cash)
        self.at = bot.now
        self.quantity = {}

    def fills_today(self, day):
        return [replace(f, filled_at=self.at,
                        fees=SELL_FEE if f.side == str(Side.SELL) else D(0),
                        filled_quantity=self.quantity.get(f.client_order_id, f.filled_quantity))
                for f in super().fills_today(day)]


def setup(tmp_path, cash: list[CashTrade] | None = None) -> tuple[Bot, str, Exact, AccountReads]:
    bot = Bot(tmp_path)
    t = closed_trade(bot)
    broker = Exact(bot, [] if cash is None else cash)
    reads = AccountReads(broker, float(CFG.orders.balance_read_seconds), lambda: bot.now)
    bot.service.account_reads = reads
    return bot, t, broker, reads


def filled(bot: Bot, t: str) -> tuple[OrderRow, OrderRow]:
    entry, sell = [o for o in bot.engine.orders(t) if o.filled_quantity > 0]
    return entry, sell


def both(bot: Bot) -> list[CashTrade]:
    return [CashTrade("ABC", "BUY", BUY_NET, bot.now), CashTrade("ABC", "SELL", SELL_NET, bot.now)]


def record(bot: Bot, reads: AccountReads) -> int:
    reads.read_once()
    return record_exact_fills(bot.engine, reads.snapshot())


def rows(bot: Bot) -> list[sqlite3.Row]:
    return bot.engine.store.query("SELECT key, body FROM journal WHERE kind = ? ORDER BY seq",
                                  (EXACT_FILL_PRICE,))


def test_the_loop_records_exact_prices_and_the_daily_loss_counts_them(tmp_path):
    bot, t, broker, reads = setup(tmp_path)
    assert entries.realized_loss_today(bot.engine, bot.now) == D(0)  # rounded: 10 -> 10
    broker.cash = both(bot)
    reads.read_once()
    assert bot.run(3)  # recorded on the loop's reconciliation pass, not on the read
    entry, sell = filled(bot, t)
    assert (entry.exact_fill_price, sell.exact_fill_price) == (D("10.004"), D("9.995"))
    assert (entry.average_fill_price, sell.average_fill_price) == (D(10), D(10))  # kept
    assert entries.realized_loss_today(bot.engine, bot.now) == D("0.018")  # fees left out
    (trade,) = bot.service.status().trades_today
    assert (trade.entry_price, trade.exit_price, trade.result_gross_usd) == (
        D("10.004"), D("9.995"), D("-0.018"))
    assert [r["key"] for r in rows(bot)] == [entry.client_order_id, sell.client_order_id]
    assert '"net_amount": "19.98"' in rows(bot)[1]["body"]
    assert '"fees": "0.01"' in rows(bot)[1]["body"]


def test_a_recorded_price_is_never_overwritten(tmp_path):
    bot, t, broker, reads = setup(tmp_path)
    broker.cash = both(bot)
    assert record(bot, reads) == 2
    broker.cash = [CashTrade("ABC", "BUY", D("-20.01"), bot.now),
                   CashTrade("ABC", "SELL", D("19.97"), bot.now)]
    assert record(bot, reads) == 0
    entry, sell = filled(bot, t)
    assert (entry.exact_fill_price, sell.exact_fill_price) == (D("10.004"), D("9.995"))
    assert not bot.engine.record_exact_fill_price(entry.client_order_id, D(11), {})
    assert len(rows(bot)) == 2


def test_a_late_activity_is_recorded_when_it_arrives(tmp_path):
    bot, t, broker, reads = setup(tmp_path)
    broker.cash = both(bot)[:1]  # the exit's activity not listed yet
    assert record(bot, reads) == 1
    entry, sell = filled(bot, t)
    assert (entry.exact_fill_price, sell.exact_fill_price) == (D("10.004"), None)
    # Until then the exit counts at its rounded price: (10 - 10.004) x 2.
    assert entries.realized_loss_today(bot.engine, bot.now) == D("0.008")
    broker.cash = both(bot)
    assert record(bot, reads) == 1
    assert filled(bot, t)[1].exact_fill_price == D("9.995")
    assert entries.realized_loss_today(bot.engine, bot.now) == D("0.018")


def test_an_activity_that_never_arrives_keeps_the_order_rows_price(tmp_path):
    bot, t, _, reads = setup(tmp_path)
    for _ in range(3):
        assert record(bot, reads) == 0
    assert [o.exact_fill_price for o in filled(bot, t)] == [None, None]
    assert entries.realized_loss_today(bot.engine, bot.now) == D(0)
    assert rows(bot) == []


def test_two_executions_are_not_one_match(tmp_path):
    bot, t, broker, reads = setup(tmp_path)
    # The exit filled in two executions listed at the same time: no single row is the fill.
    broker.cash = [*both(bot)[:1], CashTrade("ABC", "SELL", D("9.995"), bot.now),
                   CashTrade("ABC", "SELL", D("9.985"), bot.now)]
    assert record(bot, reads) == 1
    assert filled(bot, t)[1].exact_fill_price is None


def test_one_execution_of_several_is_never_taken_for_the_whole_fill(tmp_path):
    bot, t, broker, reads = setup(tmp_path)
    # Only one of two executions shares the history row's fill time: its amount covers one
    # share, so the price it gives (about 5) is far from the row's 10 and is refused.
    broker.cash = [*both(bot)[:1], CashTrade("ABC", "SELL", D("9.995"), bot.now),
                   CashTrade("ABC", "SELL", D("9.985"), bot.now.replace(second=1))]
    assert record(bot, reads) == 1
    assert filled(bot, t)[1].exact_fill_price is None


def test_a_price_that_does_not_round_to_the_order_rows_is_refused(tmp_path):
    bot, t, broker, reads = setup(tmp_path)
    # (19.97 + 0.01) / 2 = 9.99: one cent off the row's 10, so not this whole fill.
    broker.cash = [*both(bot)[:1], CashTrade("ABC", "SELL", D("19.97"), bot.now)]
    assert record(bot, reads) == 1
    assert filled(bot, t)[1].exact_fill_price is None


def test_900_of_918_shares_is_not_the_whole_fill(tmp_path):
    bot, t, _, _ = setup(tmp_path)
    entry = replace(filled(bot, t)[0], quantity=D(918), filled_quantity=D(918),
                    average_fill_price=D("0.24"))
    fill = BrokerFill(entry.client_order_id, "ABC", "BUY", "LIMIT", D(918), D("0.24"),
                      bot.now, D(0))
    # The fill (918 at 0.2449) listed as two executions: 900 at the fill time, 18 a second
    # later. The 900 row alone implies 220.41 / 918 = 0.2401, still 0.24 to the cent, so only
    # the row count (two rows, one filled order) refuses it.
    part = CashTrade("ABC", "BUY", D("-220.41"), bot.now)
    rest = CashTrade("ABC", "BUY", D("-4.4082"), bot.now.replace(second=1))
    assert exact_fill(entry, fill, Balances(fills=(fill,), cash=(part, rest))) is None
    assert exact_fill(entry, fill, Balances(fills=(fill,), cash=(part,))) is not None
    whole = CashTrade("ABC", "BUY", D("-224.8182"), bot.now)
    assert exact_fill(entry, fill, Balances(fills=(fill,), cash=(whole,)))[0] == D("0.2449")


def test_a_history_row_with_another_quantity_is_ignored(tmp_path):
    bot, t, broker, reads = setup(tmp_path)
    broker.cash = both(bot)
    broker.quantity[filled(bot, t)[1].client_order_id] = D(1)
    assert record(bot, reads) == 1
    assert filled(bot, t)[1].exact_fill_price is None


def test_a_wrong_sign_or_unknown_fees_prove_nothing(tmp_path):
    bot, t, broker, reads = setup(tmp_path)
    broker.cash = [CashTrade("ABC", "BUY", -BUY_NET, bot.now)]  # a buy that brought cash in
    assert record(bot, reads) == 0

    class NoFees(Exact):
        def fills_today(self, day):
            return [replace(f, fees=None) for f in super().fills_today(day)]

    reads = AccountReads(NoFees(bot, both(bot)), float(CFG.orders.balance_read_seconds),
                         lambda: bot.now)
    assert record(bot, reads) == 0
    assert [o.exact_fill_price for o in filled(bot, t)] == [None, None]


def test_an_order_still_working_is_not_recorded(tmp_path):
    bot, t, broker, reads = setup(tmp_path)
    broker.cash = both(bot)
    reads.read_once()
    entry, _ = filled(bot, t)
    fill = BrokerFill(entry.client_order_id, "ABC", "BUY", "LIMIT", D(2), D(10), bot.now, D(0))
    for state in (None, OrderState.ACCEPTED, OrderState.PARTIALLY_FILLED):
        working = replace(entry, broker_state=state)
        assert exact_fill(working, fill, reads.snapshot()) is None
    assert exact_fill(entry, fill, reads.snapshot()) is not None


def test_a_non_positive_price_is_refused(tmp_path):
    bot, t, _, _ = setup(tmp_path)
    with pytest.raises(ValueError, match="not positive"):
        bot.engine.record_exact_fill_price(filled(bot, t)[0].client_order_id, D(0), {})


def test_a_v4_database_gains_the_exact_fill_price_column(tmp_path):
    path = tmp_path / "a.sqlite3"
    db = sqlite3.connect(path, isolation_level=None)
    for migration in MIGRATIONS[:4]:
        migration(db)
    db.execute("PRAGMA user_version = 4")
    db.execute("INSERT INTO orders(client_order_id, trade_id, leg, symbol, side, order_type, "
               "quantity, send_state, updated_at) VALUES ('o1', 't1', 'entry', 'ABC', 'BUY', "
               "'LIMIT', '1', 'acknowledged', 'x')")
    db.close()
    s = Store.open(path, busy_timeout_seconds=0)
    (row,) = s.query("SELECT client_order_id, exact_fill_price FROM orders")
    assert (row["client_order_id"], row["exact_fill_price"]) == ("o1", None)
    assert (tmp_path / "a.pre-v5.sqlite3").exists()
    s.close()
