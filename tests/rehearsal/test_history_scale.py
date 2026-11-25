"""The bot's reads scale with today's work, not with history (backlog T17, review 2026-11-24
section 7).

The same trading morning (`test_trading_day.scenario`, AAA bought and stopped out) runs once on
an empty database and once on one holding a long closed history. Every row and query the bot
reads through the store during the run is counted: the history must add none.
"""

from __future__ import annotations

from collections import Counter
from datetime import UTC, datetime, timedelta

import pytest

from rehearsal.harness import Rehearsal
from rehearsal.test_trading_day import scenario
from tickr.runtime.protection import PROTECTION_LATENCY
from tickr.store import Store

STORE_QUERY = Store.query  # the real ones, counted per run
STORE_JOURNAL = Store.journal
HISTORY_TRADES = 2000  # closed trades before the day, three orders and a journal each
STARTUP_LATEST = 1  # startup reads of the single newest row of a kind
FIRST_CLOSE = datetime(2026, 1, 5, 15, tzinfo=UTC)
# (leg, side, type, broker state, filled) of each historical trade's orders.
LEGS = (("entry", "BUY", "LIMIT", "FILLED", "2"),
        ("stop", "SELL", "STOP_LOSS", "FILLED", "2"),
        ("stop", "SELL", "STOP_LOSS", "CANCELLED", "0"))


def seed_history(database, environment: str, count: int) -> None:
    store = Store.open(database, busy_timeout_seconds=0)
    run = store.start_run(environment=environment, config_digest="d", strategy="s",
                          strategy_version="1", code_version="x")
    with store.transaction():
        for n in range(count):
            at = (FIRST_CLOSE + timedelta(hours=n)).isoformat()
            trade, symbol = f"h{n}", f"H{n % 300}"
            store.execute(
                "INSERT INTO trades (trade_id, run_id, symbol, state, entry_quantity, "
                "entry_limit_price, stop_price, planned_loss_usd, opened_at, closed_at, "
                "close_cause) VALUES (?, ?, ?, 'closed', '2', '10', '9.8', '1', ?, ?, "
                "'stopped-out')", (trade, run, symbol, at, at))
            for i, (leg, side, kind, state, filled) in enumerate(LEGS):
                store.execute(
                    "INSERT INTO orders (client_order_id, trade_id, leg, symbol, side, "
                    "order_type, quantity, send_state, broker_state, filled_quantity, "
                    "average_fill_price, fees_usd, net_cash_usd, updated_at) VALUES "
                    "(?, ?, ?, ?, ?, ?, '2', 'acknowledged', ?, ?, '10', '0.01', '0', ?)",
                    (f"{trade}-{i}", trade, leg, symbol, side, kind, state, filled, at))
            store.append(PROTECTION_LATENCY, {"symbol": symbol}, key=trade, run_id=run)
            journal_history(store, run, n, at)
    store.close()


def journal_history(store: Store, run: str, n: int, at: str) -> None:
    """The journal a past trading hour leaves: an operator command, a protect-first stop and
    its finished remainder, and an evaluation stop of another stage."""
    store.append("command", {
        "kind": "entry-stop-off", "requested_at": at.replace("+00:00", "Z"), "source": "app",
        "outcome": "done", "cause": None}, key=f"c{n}", run_id=run)
    store.append("evaluation-stopped", {"bar_start": at, "stage": "protect-first",
                                        "symbol": "H1", "cause": "busy", "remaining": ["H2"],
                                        "quotes": {}, "loss_budget_usd": None},
                 key=f"b{n}", run_id=run)
    store.append("evaluation-remainder", {"symbols": ["H2"]}, key=f"b{n}", run_id=run)
    store.append("evaluation-stopped", {"bar_start": at, "stage": "candidates-late"},
                 key=f"b{n}", run_id=run)


def morning_reads(tmp_path, monkeypatch, history: int) -> tuple[Counter, Counter]:
    r = Rehearsal(tmp_path, monkeypatch)
    scenario(r)
    seed_history(r.database, r.config.environment_name, history)
    rows: Counter[str] = Counter()
    calls: Counter[str] = Counter()
    def counted(self, sql, params=()):
        found = STORE_QUERY(self, sql, params)
        rows[sql] += len(found)
        calls[sql] += 1
        return found

    monkeypatch.setattr(Store, "query", counted)
    assert r.run_until("10:40") == 0
    monkeypatch.setattr(Store, "query", STORE_QUERY)
    assert [t["close_cause"] for t in r.trades() if t["symbol"] == "AAA"] == ["stopped-out"]
    return rows, calls


@pytest.mark.parametrize("history", [HISTORY_TRADES])
def test_a_long_history_adds_no_reads_to_a_trading_morning(tmp_path, monkeypatch, history):
    empty_rows, empty_calls = morning_reads(tmp_path / "empty", monkeypatch, 0)
    rows, calls = morning_reads(tmp_path / "history", monkeypatch, history)
    grown = {sql: (empty_rows[sql], n) for sql, n in rows.items() if n > empty_rows[sql]}
    # At most the newest history row a startup read looks for (the pending protect-first
    # remainder), never rows that grow with the history.
    assert sum(n - before for before, n in grown.values()) <= len(grown) <= STARTUP_LATEST, grown
    assert sum(calls.values()) == sum(empty_calls.values())
