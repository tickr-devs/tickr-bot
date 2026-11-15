"""Seeded end-to-end properties of the daily loss room and the per-symbol pause.

Trades are opened, filled and closed through a real Engine and Store on the fake broker
(spec 13), with the store's clock moved to each close time. Then the room the platform
computes (`daily_loss_room`), the loss budget it hands the strategy (`Evaluator._loss_budget`),
the quantity the real price-action-v1 strategy sizes into it, and the admission of that
decision by the EntryDesk are checked against a plain model of the owner decisions of
2026-11-14: net realized result of the New York day (wins offset losses), 300 USD ceiling,
size down near it, no entries below `min_daily_room_usd`, 30-minute pause per symbol after a
loss (exactly at the end allowed), unknown fill prices count as a loss.
"""

from __future__ import annotations

import copy
import random
import tomllib
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal as D
from pathlib import Path
from types import SimpleNamespace

import pytest

from tickr import config
from tickr.broker.fake import FakeBroker
from tickr.calendar import NEW_YORK, SessionCalendar
from tickr.config import GateResult
from tickr.engine.core import Engine, Leg
from tickr.observers.log import DecisionLog
from tickr.risk.admission import AdmissionInputs, AdmissionLimits, Reconciliation, admit
from tickr.risk.ticks import round_to_tick
from tickr.runtime.entries import (
    EntryDesk,
    daily_loss_room,
    last_loss_at,
    realized_loss_today,
)
from tickr.runtime.evaluation import Evaluator
from tickr.store import Store
from tickr.strategy import price_action_v1 as pa
from tickr.strategy.base import SymbolContext, Verdict
from tickr.types import Bar, Quote

ROOT = Path(__file__).resolve().parents[2]
CFG = config.load(ROOT / "config" / "bot.toml")
CALENDAR = SessionCalendar.load((ROOT / CFG.session.calendar_file).parent)
GATE_OPEN = config.trading_gate(CFG).open
pytestmark = pytest.mark.skipif(not GATE_OPEN, reason="the configured trading gate is closed")

RISK = CFG.risk
CEILING = RISK.max_daily_loss_usd
MIN_ROOM = RISK.min_daily_room_usd
PAUSE = timedelta(minutes=RISK.symbol_cooldown_minutes_after_loss)
ZERO = D(0)
SYMBOL = "ABCD"

# Trading days around both 2026 DST changes and an ordinary week (New York dates).
DAYS = [
    (date(2026, 3, 6), date(2026, 3, 9)),  # Fri EST -> Mon EDT
    (date(2026, 10, 30), date(2026, 11, 2)),  # Fri EDT -> Mon EST
    (date(2026, 11, 13), date(2026, 11, 16)),
]


def ny(day: date, hh: int, mm: int, ss: int = 0) -> datetime:
    return datetime(day.year, day.month, day.day, hh, mm, ss, tzinfo=NEW_YORK)


class Clock:
    def __init__(self, at: datetime) -> None:
        self.at = at

    def __call__(self) -> datetime:
        return self.at


@dataclass(frozen=True)
class Closed:
    symbol: str
    closed_at: datetime
    result: D | None  # None: a fill price is unknown


class Rig:
    def __init__(self, path: Path, now: datetime) -> None:
        self.clock = Clock(now)
        self.broker = FakeBroker()
        self.store = Store.open(path / "db.sqlite3", busy_timeout_seconds=0, now=self.clock)
        self.engine = Engine(self.store, self.broker, CFG, "prop0001",
                             lambda: GateResult(True, "t"),
                             clock=lambda: self.clock().timestamp())  # the rig's time, never wall
        self.engine.recover()
        self.desk = EntryDesk(self.engine, CFG, calendar=CALENDAR, last_broker_read_at=self.clock,
                              log=DecisionLog(path / "decisions"))

    def steps(self) -> None:
        for _ in range(3):
            self.engine.step()

    def round_trip(self, symbol: str, quantity: D, entry: D, exit_: D, closed: datetime,
                   unknown: bool = False) -> Closed:
        """Open, fill and close one trade; the close is stamped `closed`."""
        self.clock.at = closed - timedelta(minutes=1)
        trade_id = self.engine.open_trade(symbol, quantity, D("10.10"), D("9.75"), D("3.50"))
        self.broker.fill(self.engine.orders(trade_id)[0].client_order_id, quantity, entry)
        self.steps()
        (stop,) = [o for o in self.engine.orders(trade_id) if o.leg is Leg.STOP and o.live]
        self.clock.at = closed
        self.broker.fill(stop.client_order_id, quantity, exit_)
        if unknown:  # the broker's detail lost the exit price
            order = self.broker.orders[stop.client_order_id]
            self.broker.orders[stop.client_order_id] = replace(order, average_fill_price=None)
        self.steps()
        assert not self.engine.trades(), "the trade should be closed"
        if unknown:
            # The engine keeps what the broker said: a fill without a price.
            stop_row = self.engine.order(stop.client_order_id)
            assert stop_row.filled_quantity > 0 and stop_row.average_fill_price is None
        return Closed(symbol, closed, None if unknown else (exit_ - entry) * quantity)


def model_loss(history: list[Closed], now: datetime) -> D | None:
    today = now.astimezone(NEW_YORK).date()
    todays = [c for c in history if c.closed_at.astimezone(NEW_YORK).date() == today]
    if any(c.result is None for c in todays):
        return None
    return max(-sum((c.result for c in todays), ZERO), ZERO)


def model_last_loss(history: list[Closed], symbol: str, now: datetime) -> datetime | None:
    today = now.astimezone(NEW_YORK).date()
    losses = [c.closed_at for c in history
              if c.symbol == symbol and c.closed_at.astimezone(NEW_YORK).date() == today
              and (c.result is None or c.result < 0)]
    return max(losses, default=None)


def random_history(rig: Rig, rng: random.Random, yesterday: date, today: date,
                   now: datetime, unknown_rate: float) -> list[Closed]:
    """Closed trades at increasing times: yesterday's session and evening, before today's
    open (still today in New York), and today's session up to `now`."""
    times = [ny(yesterday, 10, 0) + timedelta(minutes=rng.randint(0, 840)) for _ in range(
        rng.randint(0, 3))]  # up to 24:00 New York yesterday
    times.append(ny(yesterday, 23, 59, 59))
    times += [ny(today, 0, 0, 1), ny(today, 9, 0)][:rng.randint(0, 2)]
    span = int((now - ny(today, 9, 31)).total_seconds())
    times += [ny(today, 9, 31) + timedelta(seconds=rng.randint(0, max(span, 0)))
              for _ in range(rng.randint(0, 6))]
    # Some closes land right at the edges of the pause for the decision at `now`.
    for edge in (PAUSE, PAUSE - timedelta(seconds=1), PAUSE + timedelta(seconds=1)):
        if rng.random() < 0.2 and now - edge > ny(today, 9, 31):
            times.append(now - edge)
    history = []
    for closed in sorted(set(times)):
        if closed >= now:
            continue
        entry = D(rng.randint(990, 1010)) / D(100)
        exit_ = entry + D(rng.randint(-150, 100)) / D(100)
        if rng.random() < 0.1:
            exit_ = entry  # break-even: neither a loss nor a win
        quantity = D(rng.randint(1, 60))
        symbol = rng.choice([SYMBOL, SYMBOL, "XYZ"])
        history.append(rig.round_trip(symbol, quantity, entry, exit_, closed,
                                      unknown=rng.random() < unknown_rate))
    return history


# ---------------------------------------------------------------- the real strategy


def _strategy() -> pa.PriceActionV1:
    values = tomllib.loads((ROOT / "strategies" / "price-action-v1" / "strategy.toml")
                           .read_text())
    values = copy.deepcopy(values)
    values["indicators"]["warmup_period_multiple"] = 1  # short generated sessions suffice
    return pa.create(values)


STRATEGY = _strategy()
BAR_DAY = date(2026, 11, 13)
PRIOR = [date(2026, 11, d) for d in (6, 9, 10, 11, 12)]
PRIOR_MINUTES = 40  # enough for relative volume at minute 30; keeps the tests fast


def _minute(day: date, index: int) -> datetime:
    return (ny(day, 9, 30) + timedelta(minutes=index)).astimezone(UTC)


def _bars(scale: D) -> tuple[list[Bar], list[list[Bar]]]:
    """The strategy tests' breakout baseline (30 rising bars, ATR 0.14, trigger 22.92,
    invalidation 22.38), with every price multiplied by `scale` and volumes divided by it."""
    volume, flat_volume = D(10_000) / scale, D(3_000) / scale
    today, price = [], D(20) * scale
    step, wick = D("0.10") * scale, D("0.02") * scale
    for i in range(30):
        close = price + step
        today.append(Bar(SYMBOL, _minute(BAR_DAY, i), price, close + wick, price - wick, close,
                         volume))
        price = close
    flat = D(20) * scale
    prior = [[Bar(SYMBOL, _minute(day, i), flat, flat, flat, flat, flat_volume)
              for i in range(PRIOR_MINUTES)] for day in PRIOR]
    return today, prior


SCALES = [D("0.3"), D("0.5"), D("0.7"), D("1"), D("2"), D("5")]
BARS = {scale: _bars(scale) for scale in SCALES}


def strategy_decision(rng: random.Random, budget: D | None, now: datetime):
    scale = rng.choice(SCALES)
    today, prior = BARS[scale]
    trigger, atr = D("22.92") * scale, D("0.14") * scale
    tick = D("0.01")
    ask = trigger.quantize(tick) + tick * rng.randint(1, max(int(atr / tick), 1))
    # At least one tick: a locked quote (bid == ask) is the case of test_bug_locked_quote_*.
    spread = tick * rng.choice([1, 1, 2])
    q = Quote(SYMBOL, ask - spread, ask, D(100), D(100), now, now)
    decision = STRATEGY.evaluate(SymbolContext(SYMBOL, today, prior, q, None, budget))
    return decision, q


def enter_decision(now: datetime):
    """An ENTER of the real strategy at the baseline prices (cent spread, unscaled)."""
    today, prior = BARS[D(1)]
    q = Quote(SYMBOL, D("22.99"), D("23.00"), D(100), D(100), now, now)
    decision = STRATEGY.evaluate(SymbolContext(SYMBOL, today, prior, q, None, None))
    assert decision.verdict is Verdict.ENTER
    return decision


def loss_budget(rig: Rig, now: datetime) -> D | None:
    """The budget the Evaluator hands the strategy at `now` (its own method, its own inputs)."""
    return Evaluator._loss_budget(SimpleNamespace(engine=rig.engine, config=CFG), now)


# ---------------------------------------------------------------- properties


@pytest.mark.parametrize("seed", range(9))
def test_room_budget_and_admission_follow_the_daily_rule(tmp_path, seed):
    rng = random.Random(seed)
    yesterday, today = DAYS[seed % len(DAYS)]
    rig = Rig(tmp_path, ny(today, 9, 30))
    checked = 0
    for case in range(6):
        now = ny(today, 10, 0) + timedelta(minutes=30 * case + rng.randint(0, 29),
                                           seconds=rng.randint(0, 59))
        if case == 0:
            history = random_history(rig, rng, yesterday, today, now, unknown_rate=0.05)
        else:  # more of today's trades, after the earlier ones
            last = max((c.closed_at for c in history), default=ny(today, 9, 31))
            span = int((now - last).total_seconds())
            for _ in range(rng.randint(0, 3)):
                if span < 2:
                    break
                closed = last + timedelta(seconds=rng.randint(1, span - 1))
                if closed >= now:
                    continue
                entry = D(rng.randint(990, 1010)) / D(100)
                exit_ = entry + D(rng.randint(-200, 100)) / D(100)
                history.append(rig.round_trip(rng.choice([SYMBOL, "XYZ"]),
                                              D(rng.randint(1, 60)), entry, exit_, closed))
                last = closed
        # Often push the day near the ceiling: one XYZ loss leaves a room of -5 .. 60 USD.
        loss = model_loss(history, now)
        last = max((c.closed_at for c in history), default=ny(today, 9, 31))
        closed = now - timedelta(seconds=1)
        if loss is not None and closed > last and rng.random() < 0.6:
            target = D(rng.randint(-500, 6000)) / D(100)
            needed = CEILING - loss - target
            if ZERO < needed < D(1000):
                history.append(rig.round_trip("XYZ", D(100), D(10), D(10) - needed / D(100),
                                              closed))
        rig.clock.at = now

        loss = model_loss(history, now)
        assert realized_loss_today(rig.engine, now) == loss, (seed, case, history)
        room = None if loss is None else CEILING - loss
        assert daily_loss_room(rig.engine, CFG, now) == room
        assert last_loss_at(rig.engine, SYMBOL, now) == model_last_loss(history, SYMBOL, now)

        budget = loss_budget(rig, now)
        assert budget == (room if room is not None and room >= MIN_ROOM else None)

        decision, q = strategy_decision(rng, budget, now)
        sent = len(rig.broker.sent)
        record = rig.desk.on_decision(decision, bar_start=now.replace(second=0) -
                                      timedelta(minutes=1), quote=q, now=now)
        cause = record.refusal_cause or ""
        if decision.verdict is not Verdict.ENTER:
            continue
        checked += 1
        limit = rig.desk._inputs(decision, q, now).limit_price
        paused = model_last_loss(history, SYMBOL, now)
        paused = paused is not None and now < paused + PAUSE
        if record.admission == "taken":
            (trade,) = rig.engine.trades()
            assert room is not None and room >= MIN_ROOM
            assert trade.planned_loss_usd <= min(room, RISK.planned_loss_usd), (seed, case)
            assert not paused
            # Close it at its stop so the day goes on with this loss counted.
            rig.clock.at = now + timedelta(seconds=1)
            (entry,) = rig.engine.orders(trade.trade_id)
            rig.broker.fill(entry.client_order_id, trade.entry_quantity, entry.limit_price)
            rig.steps()
            (stop,) = [o for o in rig.engine.orders(trade.trade_id)
                       if o.leg is Leg.STOP and o.live]
            rig.clock.at = now + timedelta(seconds=2)
            rig.broker.fill(stop.client_order_id, trade.entry_quantity, trade.stop_price)
            rig.steps()
            history.append(Closed(SYMBOL, now + timedelta(seconds=2),
                                  (trade.stop_price - entry.limit_price) * trade.entry_quantity))
            continue
        assert len(rig.broker.sent) == sent  # a refusal sends nothing
        # The first failing check, in admission order (spec 7: 4, 5, 6).
        if paused:
            assert cause.startswith("SYMBOL_COOLDOWN"), cause
        elif q.ask > limit:
            assert cause.startswith("PRICE_OUTSIDE_WINDOW"), cause  # raw window, rounded limit
        elif loss is None:
            assert cause.startswith("DAILY_MONEY_UNKNOWN"), cause
        elif room < MIN_ROOM:
            assert cause.startswith("DAILY_CEILING"), cause
        else:
            # Room above the minimum and one share sized: never refused for money.
            pytest.fail(f"seed {seed} case {case}: room {room}, budget {budget}, "
                        f"quantity {decision.quantity}, refused: {cause}")
    assert checked >= 1


@pytest.mark.parametrize("seed", range(3))
def test_wins_offset_losses_and_yesterday_never_counts(tmp_path, seed):
    """Adding a winning trade today never lowers the room; adding any trade closed yesterday
    (New York date, up to 23:59:59 the evening before) never changes it."""
    rng = random.Random(100 + seed)
    yesterday, today = DAYS[seed % len(DAYS)]
    now = ny(today, 14, 0)
    rig = Rig(tmp_path, now)
    history = random_history(rig, rng, yesterday, today, ny(today, 12, 0), unknown_rate=0)
    before = daily_loss_room(rig.engine, CFG, now)
    assert before == CEILING - model_loss(history, now)
    rig.round_trip("XYZ", D(rng.randint(1, 40)), D("10.00"), D("10.40"), ny(yesterday, 23, 0))
    rig.round_trip("XYZ", D(rng.randint(1, 40)), D("10.00"), D("9.10"), ny(yesterday, 23, 59, 59))
    assert daily_loss_room(rig.engine, CFG, now) == before
    win = rig.round_trip("XYZ", D(rng.randint(1, 40)), D("10.00"),
                         D("10.00") + D(rng.randint(1, 80)) / D(100), ny(today, 12, 30))
    after = daily_loss_room(rig.engine, CFG, now)
    assert after == min(CEILING, before + win.result)
    assert after >= before
    # A trade closed after midnight in New York but before the open counts today.
    early = rig.round_trip("XYZ", D(10), D("10.00"), D("9.00"), ny(today, 0, 0, 1))
    assert early.result == D(-10)
    expected = CEILING - model_loss([*history, win, early], now)
    assert daily_loss_room(rig.engine, CFG, now) == expected


@pytest.mark.parametrize("seed", range(3))
def test_reservations_of_open_trades_reduce_the_room(tmp_path, seed):
    rng = random.Random(200 + seed)
    yesterday, today = DAYS[seed % len(DAYS)]
    now = ny(today, 13, 0)
    rig = Rig(tmp_path, now)
    history = random_history(rig, rng, yesterday, today, ny(today, 12, 0), unknown_rate=0)
    reserved = D(rng.randint(1, 5000)) / D(100)
    rig.clock.at = now
    rig.engine.open_trade("XYZ", D(1), D("10.10"), D("9.75"), reserved)
    loss = model_loss(history, now)
    assert daily_loss_room(rig.engine, CFG, now) == CEILING - loss - reserved
    # The desk hands admission the same numbers: an open trade blocks a second one anyway.
    inputs = rig.desk._inputs(enter_decision(now), None, now)
    assert (inputs.realized_loss_today_usd, inputs.open_reservations_usd) == (loss, reserved)


@pytest.mark.parametrize("seed", range(3))
def test_pause_after_a_loss_follows_the_latest_losing_close_of_the_symbol(tmp_path, seed):
    """Random closes of several symbols (wins, losses, unknown prices, yesterday's): at a
    random `now`, the desk refuses with SYMBOL_COOLDOWN exactly while now < latest loss today
    + 30 min, and the pause is gone with the config flag off."""
    rng = random.Random(300 + seed)
    yesterday, today = DAYS[seed % len(DAYS)]
    rig = Rig(tmp_path, ny(today, 9, 30))
    end = ny(today, 12, 0)
    history = random_history(rig, rng, yesterday, today, end, unknown_rate=0.15)
    off = EntryDesk(rig.engine, replace(CFG, risk=replace(
        RISK, symbol_cooldown_after_loss_enabled=False)), calendar=CALENDAR,
        last_broker_read_at=rig.clock, log=DecisionLog(tmp_path / "off"))
    probes = [end + timedelta(seconds=rng.randint(0, 3600)) for _ in range(10)]
    latest = model_last_loss(history, SYMBOL, end)
    if latest is not None:
        probes += [latest + PAUSE, latest + PAUSE - timedelta(seconds=1),
                   latest + PAUSE + timedelta(seconds=1)]
    decision = enter_decision(end)
    for now in probes:
        if now < end:
            continue
        rig.clock.at = now
        inputs = rig.desk._inputs(decision, None, now)
        assert inputs.symbol_last_loss_at == latest
        paused = latest is not None and now < latest + PAUSE
        refused = admit(inputs, rig.desk.limits).cause
        assert (refused == "SYMBOL_COOLDOWN") == paused, (now, latest, refused)
        assert admit(off._inputs(decision, None, now), off.limits).cause != "SYMBOL_COOLDOWN"


@pytest.mark.parametrize("seed", range(3))
def test_the_strategy_size_fits_any_room_admission_accepts(seed):
    """No store: the real strategy sized on a random room at random price levels (sub-dollar
    to 100 USD), then admission on the tick-rounded order prices with that room left. With a
    room of at least `min_daily_room_usd` and one share sized, the entry is never refused for
    money (owner 2026-11-14: size down to fit), and its planned loss fits the room."""
    rng = random.Random(400 + seed)
    now = ny(date(2026, 11, 16), 11, 0)
    cache: dict[D, tuple[list[Bar], list[list[Bar]]]] = {}
    entered = 0
    for _ in range(400):
        scale = D(rng.randint(3, 450)) / D(100)
        today, prior = cache.setdefault(scale, _bars(scale))
        trigger, atr = D("22.92") * scale, D("0.14") * scale
        tick = D("0.01") if trigger >= 1 else D("0.0001")
        ask = (trigger.quantize(tick, rounding="ROUND_DOWN")
               + tick * rng.randint(1, max(int(atr / tick), 1)))
        q = Quote(SYMBOL, ask - tick * rng.choice([1, 1, 2]), ask, D(100), D(100), now, now)
        room = D(rng.randint(int(MIN_ROOM * 100), int(CEILING * 100))) / D(100)
        budget = room  # what Evaluator._loss_budget passes at or above the minimum
        decision = STRATEGY.evaluate(SymbolContext(SYMBOL, today, prior, q, None, budget))
        if decision.verdict is not Verdict.ENTER:
            continue
        entered += 1
        assert decision.quantity is not None and decision.entry_high is not None
        assert decision.stop is not None
        limit, stop = round_to_tick(decision.entry_high), round_to_tick(decision.stop)
        result = admit(replace(
            _admissible(now, decision, q), limit_price=limit, stop_price=stop,
            realized_loss_today_usd=CEILING - room), LIMITS)
        if q.ask > limit:
            assert result.cause == "PRICE_OUTSIDE_WINDOW"
            continue
        assert result.ok, (scale, ask, room, decision.quantity, result)
        assert result.planned_loss_usd <= min(room, RISK.planned_loss_usd)
    assert entered > 200


LIMITS = AdmissionLimits.from_config(CFG)


def _admissible(now: datetime, decision, q: Quote) -> AdmissionInputs:
    return AdmissionInputs(
        gate=GateResult(True, "t"),
        reconciliation=Reconciliation(startup_done=True, last_broker_read_at=now,
                                      foreign_exposure=False, unresolved_exposure=False),
        entry_stop=False, session=CALENDAR.day(now.date()), now=now, own_open_positions=0,
        own_working_orders=0, open_positions=0, symbol_last_loss_at=None, decision=decision,
        limit_price=None, stop_price=None, setup_valid=True, quote=q,
        realized_loss_today_usd=ZERO, open_reservations_usd=ZERO, entry_budget_available=True,
        stop_budget_available=True)
