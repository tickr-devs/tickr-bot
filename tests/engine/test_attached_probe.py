"""Probe F8 (engine/attached_probe.py): an entry with an attached stop, sent as one combo."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from tickr import config
from tickr.broker.base import Acknowledgement, Refused
from tickr.broker.fake import FakeBroker
from tickr.data.base import FakeMarketData
from tickr.engine import attached_probe as ap
from tickr.engine import session_probe as sp
from tickr.types import ComboRole, OrderType, Quote, Side

ROOT = Path(__file__).resolve().parents[2]
CFG = config.load(ROOT / "config" / "bot.toml")
NOON = datetime(2026, 11, 16, 17, 0, tzinfo=UTC)  # 12:00 ET, Monday
SYMBOL = "ABCD"

Hook = Callable[["World"], None]


class World:
    """A fake clock, broker and quote; `hook` runs on every sleep, as the market would."""

    def __init__(self, tmp_path: Path, hook: Hook | None = None) -> None:
        self.at = NOON
        self.broker = FakeBroker()
        self.market = FakeMarketData()
        self.bid, self.ask = Decimal("10.00"), Decimal("10.02")
        self.hook = hook
        self.lines: list[str] = []
        self.folder = tmp_path / "evidence"
        self.folder.mkdir()
        self._quote()

    def _quote(self) -> None:
        self.market.set_quote(Quote(SYMBOL, self.bid, self.ask, Decimal(100), Decimal(100),
                                    self.at, self.at))

    def now(self) -> datetime:
        return self.at

    def sleep(self, seconds: float) -> None:
        self.at += timedelta(seconds=seconds)
        if self.hook is not None:
            self.hook(self)
        self._quote()

    def run(self) -> tuple[int, dict]:
        code = ap.run(CFG, self.broker, self.market, SYMBOL, self.folder, now=self.now,
                      sleep=self.sleep, out=self.lines.append)
        return code, json.loads((self.folder / "summary.json").read_text("utf-8"))

    def steps(self) -> list[dict]:
        return [json.loads(line) for line in
                (self.folder / "steps.jsonl").read_text("utf-8").splitlines()]


def marketable(world: World) -> None:
    """Fill what the quote reaches: BUY LIMITs at or above the ask, SELL LIMITs at or below
    the bid. Stops never trigger."""
    for o in world.broker.open_orders():
        if o.order_type is not OrderType.LIMIT or o.limit_price is None:
            continue
        if (o.side is Side.BUY and o.limit_price >= world.ask) or (
                o.side is Side.SELL and o.limit_price <= world.bid):
            price = world.ask if o.side is Side.BUY else world.bid
            world.broker.fill(o.client_order_id, o.quantity - o.filled_quantity, price)


def test_the_attached_stop_works_at_the_fill_and_every_leg_is_recorded_first(tmp_path):
    world = World(tmp_path, marketable)
    code, summary = world.run()
    assert code == sp.EXIT_ANSWERED
    for name in ("stop-only", "bracket"):
        shape = summary["shapes"][name]
        assert shape["combo"] == sp.ACCEPTED
        assert all(v == ap.LEG_ENDED_WITH_MASTER for k, v in shape.items()
                   if k.startswith(name) and not k.endswith("master"))
    fill = summary["fill"]
    assert fill["shape"] == "stop-only" and fill["entry"] == sp.FILLED
    assert fill["after_fill"]["stop"] == ap.STOP_WORKING
    assert fill["after_fill"]["open_orders"] == ap.LISTED
    assert fill["legs_after_master"] == {"fill-stop_loss": ap.LEG_WORKING_AFTER_FILL}
    assert summary["close"] == sp.CLOSED and summary["broker_after"]["position"] == "0"
    # every leg is in the evidence before its one combo request, and none is sent twice
    steps = world.steps()
    intents = [s for s in steps if s.get("step") == "combo-intent"]
    assert len(intents) == 3
    sent = [r.client_order_id for r in world.broker.sent]
    assert len(sent) == len(set(sent))
    logged = {leg["client_order_id"] for i in intents for leg in i["legs"]}
    assert logged <= set(sent) and len(set(sent) - logged) == 1  # the close, sent alone


def test_legs_outliving_their_cancelled_master_are_cancelled_and_named(tmp_path):
    world = World(tmp_path, marketable)
    world.broker.combo_cancels_legs = False
    code, summary = world.run()
    assert code == sp.EXIT_ANSWERED
    assert summary["shapes"]["stop-only"]["stop-only-stop_loss"] == ap.LEG_OUTLIVED_MASTER
    assert not [o for o in world.broker.open_orders() if not o.state.terminal]


def test_refused_combos_send_no_fill_and_close_nothing(tmp_path):
    world = World(tmp_path, marketable)
    world.broker.outcomes.extend(["refuse", "refuse"])
    code, summary = world.run()
    assert code == sp.EXIT_ANSWERED
    assert [summary["shapes"][n]["combo"] for n in ("stop-only", "bracket")] == [
        sp.REFUSED, sp.REFUSED]
    assert summary["fill"] == sp.NOT_TESTED and summary["close"] == sp.CLOSED
    assert world.broker.positions() == []


def test_an_unknown_answer_is_read_by_detail_never_resent(tmp_path):
    world = World(tmp_path, marketable)
    world.broker.outcomes.append("unknown")  # stop-only reached the broker, reply lost
    code, summary = world.run()
    assert code == sp.EXIT_ANSWERED
    assert summary["shapes"]["stop-only"]["combo"] == sp.ACCEPTED  # Detail found it
    sent = [r.client_order_id for r in world.broker.sent]
    assert len(sent) == len(set(sent))


def test_an_entry_that_does_not_fill_leaves_no_order(tmp_path):
    world = World(tmp_path)  # nothing fills
    code, summary = world.run()
    assert code == sp.EXIT_ANSWERED
    assert summary["fill"]["entry"] == sp.NOT_FILLED
    assert not [o for o in world.broker.open_orders() if not o.state.terminal]


def test_the_webull_combo_body_carries_each_leg_with_its_role():
    from tickr.broker.webull import build_order_body
    from tickr.types import OrderRequest, TimeInForce, TradingSession

    leg = OrderRequest("p-stop", SYMBOL, Side.SELL, OrderType.STOP_LOSS, Decimal(1),
                       TimeInForce.DAY, TradingSession.CORE, stop_price=Decimal("9.80"))
    body = build_order_body(leg, 32, ComboRole.STOP_LOSS.value)
    assert body["combo_type"] == "STOP_LOSS" and body["client_order_id"] == "p-stop"
    with pytest.raises(Refused, match="longer"):
        build_order_body(leg, 3, ComboRole.STOP_LOSS.value)


class OwnLegIds(FakeBroker):
    """Books attached legs under ids of the broker's own and never ends them with the MASTER:
    absence by a leg's client id then proves nothing."""

    def place_combo(self, combo_id, legs):
        self.sent.extend(r for _, r in legs)
        for number, (role, request) in enumerate(legs):
            if role is not ComboRole.MASTER:
                request = replace(request, client_order_id=f"broker-{combo_id}-{number}")
            self._book(request)
        master = next(r for role, r in legs if role is ComboRole.MASTER)
        return Acknowledgement(master.client_order_id, f"fake-combo-{combo_id}")


def test_legs_kept_under_broker_ids_stop_the_run_and_are_named(tmp_path):
    world = World(tmp_path, marketable)
    world.broker = OwnLegIds()
    code, summary = world.run()
    assert code == sp.EXIT_OPERATOR
    assert "still lists working orders" in summary["unresolved"]
    assert "stop-only" not in summary.get("shapes", {})  # no verdict stands on absence
    leftover = summary["broker_after"]["working_probe_orders"]
    assert leftover and all(o.startswith("broker-") for o in leftover)
    assert any("open-orders" == s["step"] and s["orders"] for s in world.steps())


def test_a_stale_quote_in_the_regular_session_is_refused(tmp_path):
    world = World(tmp_path, marketable)
    world.at = NOON + timedelta(seconds=float(CFG.risk.max_quote_age_seconds) + 1)
    world.market.set_quote(Quote(SYMBOL, world.bid, world.ask, Decimal(100), Decimal(100),
                                 NOON, NOON))
    world.sleep = lambda seconds: None  # type: ignore[method-assign]
    code, summary = world.run()
    assert "stopped" in summary and world.broker.sent == []
