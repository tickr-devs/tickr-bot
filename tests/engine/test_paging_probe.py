"""Probe H2c (engine/paging_probe.py): far limits placed and cancelled until the history pages."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from tickr import config
from tickr.broker.base import BrokerError
from tickr.broker.fake import FakeBroker
from tickr.data.base import FakeMarketData
from tickr.engine import paging_probe as pp
from tickr.engine import session_probe as sp
from tickr.types import OrderType, Quote, Side, TimeInForce

ROOT = Path(__file__).resolve().parents[2]
CFG = config.load(ROOT / "config" / "bot.toml")
POST_TIME = datetime(2026, 11, 16, 22, 0, tzinfo=UTC)  # 17:00 ET, Monday
SYMBOL = "ABCD"
PAGE = 44  # rows per page the fake history serves (one page held 44 rows, 2026-11-17)


class PagingBroker(FakeBroker):
    """A fake whose history lists every order sent, newest first, `PAGE` rows a page."""

    def __init__(self, repeat: bool = False) -> None:
        super().__init__()
        self.repeat = repeat
        self.asked: list[tuple[int, Any, str | None]] = []

    def history_page_shapes(self, max_pages: int, day: Any = None,
                            listed_prefix: str | None = None) -> list[dict[str, Any]]:
        self.asked.append((max_pages, day, listed_prefix))
        ids = [r.client_order_id for r in reversed(self.sent)]
        pages = []
        for start in range(0, len(ids), PAGE):
            chunk = ids[start:start + PAGE]
            if self.repeat and start:
                chunk = chunk + ids[:1]
            pages.append({"combos": len(chunk), "rows": len(chunk), "own_rows": len(chunk),
                          "place_times": [f"t{999 - start - n:03d}" for n in range(len(chunk))],
                          "next_page": start + PAGE < len(ids),
                          "listed": [c for c in chunk if c.startswith(listed_prefix or "")]})
        return pages[:max_pages]


class World:
    def __init__(self, tmp_path: Path, broker: FakeBroker) -> None:
        self.at = POST_TIME
        self.broker = broker
        self.market = FakeMarketData()
        self.lines: list[str] = []
        self.folder = tmp_path / "evidence"
        self.folder.mkdir()
        self._quote()

    def _quote(self) -> None:
        self.market.set_quote(Quote(SYMBOL, Decimal("10.00"), Decimal("10.02"), Decimal(100),
                                    Decimal(100), self.at, self.at))

    def now(self) -> datetime:
        return self.at

    def sleep(self, seconds: float) -> None:
        self.at += timedelta(seconds=seconds)
        self._quote()

    def run(self) -> tuple[int, dict]:
        code = pp.run(CFG, self.broker, self.market, SYMBOL, self.folder, now=self.now,
                      sleep=self.sleep, out=self.lines.append, session=sp.POST,
                      session_end=POST_TIME + timedelta(hours=3))
        return code, json.loads((self.folder / "summary.json").read_text("utf-8"))

    def steps(self) -> list[dict]:
        return [json.loads(line) for line in
                (self.folder / "steps.jsonl").read_text("utf-8").splitlines()]


def test_places_far_limits_one_by_one_cancels_each_and_reads_the_pages(tmp_path: Path) -> None:
    broker = PagingBroker()
    world = World(tmp_path, broker)
    code, summary = world.run()
    assert code == sp.EXIT_ANSWERED
    sent = broker.sent
    assert len(sent) == CFG.probe.paging_orders
    assert all(r.side is Side.BUY and r.order_type is OrderType.LIMIT
               and r.time_in_force is TimeInForce.DAY for r in sent)
    assert sent[0].limit_price == Decimal("8.00")  # 20 % below the 10.00 bid
    assert len({r.limit_price for r in sent}) == len(sent)  # one tick apart: never a duplicate
    assert not broker.open_orders() and not broker.positions()
    intents = [s["client_order_id"] for s in world.steps() if s["step"] == "intent"]
    assert intents == [r.client_order_id for r in sent]  # each recorded before its send
    assert summary["verdict"] == pp.PASSED
    assert summary["rows_per_page"] == [PAGE, CFG.probe.paging_orders - PAGE]
    assert summary["page_size"] == PAGE and summary["listed_of_placed"] == len(sent)
    assert summary["newest_first_in_each_page"] is True
    assert broker.asked == [(CFG.probe.history_max_pages, POST_TIME.date(),
                             CFG.orders.probe_client_order_id_prefix)]
    assert "h2c_paging: passed" in world.lines[-1]


def test_a_row_on_two_pages_leaves_it_unfinished(tmp_path: Path) -> None:
    code, summary = World(tmp_path, PagingBroker(repeat=True)).run()
    assert code == sp.EXIT_ANSWERED
    assert summary["verdict"] == pp.UNFINISHED and len(summary["listed_twice"]) == 1


def test_one_page_is_unfinished() -> None:
    pages = [{"rows": 3, "next_page": False, "place_times": ["c", "b", "a"],
              "listed": ["x1", "x2"]}]
    answers = pp.page_answers(pages, ["x1", "x2"])
    assert answers["verdict"] == pp.UNFINISHED and answers["page_size"] is None


def test_a_broker_without_page_shapes_sends_nothing(tmp_path: Path) -> None:
    world = World(tmp_path, FakeBroker())
    code, summary = world.run()
    assert code == sp.EXIT_ANSWERED and "cannot read history page shapes" in summary["stopped"]
    assert world.broker.sent == []


def test_a_failed_history_read_is_recorded_and_the_account_left_clean(tmp_path: Path) -> None:
    class Down(PagingBroker):
        def history_page_shapes(self, *args: Any, **kwargs: Any) -> list[dict[str, Any]]:
            raise BrokerError("history route paused")

    world = World(tmp_path, Down())
    code, summary = world.run()
    assert code == sp.EXIT_ANSWERED and "history route paused" in summary["error"]
    assert summary["broker_after"]["working_probe_orders"] == []


def test_a_fill_stops_the_run_and_names_the_order_and_the_share(tmp_path: Path) -> None:
    class FillsThird(PagingBroker):
        def place(self, request: Any) -> Any:
            ack = super().place(request)
            if len(self.sent) == 3:
                self.fill(request.client_order_id, Decimal(1), request.limit_price)
            return ack

    world = World(tmp_path, FillsThird())
    code, summary = world.run()
    third = world.broker.sent[2].client_order_id
    assert code == sp.EXIT_OPERATOR and len(world.broker.sent) == 3  # nothing more sent
    assert third in summary["unresolved"] and "filled 1" in summary["unresolved"]
    assert summary["broker_after"]["position"] == "1"
    assert world.lines[-1].startswith("OPERATOR:") and third in world.lines[-1]


def test_a_cancel_that_never_takes_stops_the_run_with_the_order_left(tmp_path: Path) -> None:
    class Sticky(PagingBroker):
        def cancel(self, client_order_id: str) -> None:
            if len(self.sent) < 2 or client_order_id != self.sent[1].client_order_id:
                super().cancel(client_order_id)

    world = World(tmp_path, Sticky())
    code, summary = world.run()
    second = world.broker.sent[1].client_order_id
    assert code == sp.EXIT_OPERATOR and len(world.broker.sent) == 2  # nothing more sent
    assert second in summary["unresolved"]
    assert summary["broker_after"]["working_probe_orders"] == [second]
    assert "pages" not in summary  # the history is not read with an order left working
