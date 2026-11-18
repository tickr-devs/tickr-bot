"""Seeded property tests of the risk layer (spec section 7; owner decisions 2026-11-14).

Stdlib `random` with fixed seeds: every run checks the same many cases, and a failure names
the seed and the inputs. These complement the example tests in test_admission.py and
test_sizing_daily_timers.py; they do not repeat them.
"""

from __future__ import annotations

import random
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest

from tickr.calendar import NEW_YORK, CalendarCause, CalendarRefused
from tickr.config import GateResult
from tickr.risk.admission import Admission, admit
from tickr.risk.causes import AdmissionCause as C
from tickr.risk.daily import check_daily
from tickr.risk.sizing import check_size, max_quantity
from tickr.risk.ticks import CENT, ONE_DOLLAR, SUB_DOLLAR_TICK, round_to_tick
from tickr.strategy.base import Verdict

from .conftest import CALENDAR, NOW, D, inputs, limits, quote

CASES = 2000
SEEDS = (1, 2, 3)
ZERO = D(0)
ONE = D(1)


# ---------------------------------------------------------------- generators


def price(rng: random.Random) -> Decimal:
    """A positive price with up to 6 decimals: sub-dollar, around one dollar, or ordinary."""
    regime = rng.random()
    if regime < 0.3:
        return D(rng.randint(1, 999_999)) / D(1_000_000)  # 0.000001 .. 0.999999
    if regime < 0.4:
        return D(1) + D(rng.randint(-5_000, 5_000)) / D(1_000_000)  # straddles 1.00
    return D(rng.randint(1_000_000, 5_000_000_000)) / D(1_000_000)  # 1 .. 5000


def money(rng: random.Random, top: int = 400) -> Decimal:
    return D(rng.randint(0, top * 100)) / D(100)


def tick_of(value: Decimal) -> Decimal:
    return CENT if value >= ONE_DOLLAR else SUB_DOLLAR_TICK


# ---------------------------------------------------------------- round_to_tick


@pytest.mark.parametrize("seed", SEEDS)
def test_round_to_tick_is_down_idempotent_monotone_and_on_a_valid_increment(seed):
    rng = random.Random(seed)
    for _ in range(CASES):
        p, q = price(rng), price(rng)
        r = round_to_tick(p)
        assert r <= p, (p, r)
        assert p - r < tick_of(p), (p, r)
        assert round_to_tick(r) == r, (p, r)
        assert r % tick_of(r) == 0, (p, r)
        lo, hi = sorted((p, q))
        assert round_to_tick(lo) <= round_to_tick(hi), (lo, hi)


def test_round_to_tick_at_one_dollar():
    assert round_to_tick(D("0.99999")) == D("0.9999")
    assert round_to_tick(D("1")) == D("1.00")
    assert round_to_tick(D("1.00999")) == D("1.00")


# ---------------------------------------------------------------- sizing


def size_case(rng: random.Random) -> tuple[Decimal, Decimal, Decimal, Decimal]:
    entry_high = price(rng)
    stop = entry_high * D(rng.randint(1, 999)) / D(1000)  # 0.1 % .. 99.9 % of the entry
    limit = D(rng.randint(1, 20_000)) / D(100)  # planned loss limit 0.01 .. 200
    cap = D(rng.randint(1, 1_000_000)) / D(100)  # notional cap 0.01 .. 10,000
    return entry_high, stop, limit, cap


@pytest.mark.parametrize("seed", SEEDS)
def test_max_quantity_is_the_largest_whole_quantity_within_both_limits(seed):
    rng = random.Random(seed)
    for _ in range(CASES):
        entry_high, stop, limit, cap = size_case(rng)
        risk = entry_high - stop
        q = max_quantity(limit, risk, cap, entry_high)
        assert q == q.to_integral_value() and q >= 0
        assert q * risk <= limit and q * entry_high <= cap, (entry_high, stop, limit, cap, q)
        bigger = q + 1
        assert bigger * risk > limit or bigger * entry_high > cap, (entry_high, stop, q)
        if q >= 1:
            assert check_size(q, entry_high, stop, limit, cap).ok
        refused = check_size(bigger, entry_high, stop, limit, cap)
        assert not refused.ok
        assert refused.cause in (C.NOTIONAL_OVER_CAP, C.PLANNED_LOSS_OVER_LIMIT)


@pytest.mark.parametrize("seed", SEEDS)
def test_check_size_admits_exactly_the_whole_quantities_within_the_limits(seed):
    rng = random.Random(seed)
    for _ in range(CASES):
        entry_high, stop, limit, cap = size_case(rng)
        quantity = D(rng.randint(-3, 3000))
        if rng.random() < 0.2:
            quantity += D(rng.randint(1, 99)) / D(100)
        result = check_size(quantity, entry_high, stop, limit, cap)
        whole = quantity == quantity.to_integral_value()
        notional, loss = quantity * entry_high, quantity * (entry_high - stop)
        expected = (C.QUANTITY_NOT_WHOLE if not whole else
                    C.QUANTITY_BELOW_ONE if quantity < 1 else
                    C.NOTIONAL_OVER_CAP if notional > cap else
                    C.PLANNED_LOSS_OVER_LIMIT if loss > limit else None)
        assert result.cause == expected, (quantity, entry_high, stop, limit, cap, result)
        assert result.ok == (expected is None)
        if result.ok:
            assert loss <= limit and notional <= cap
            assert quantity <= max_quantity(limit, entry_high - stop, cap, entry_high)


@pytest.mark.parametrize("seed", SEEDS)
def test_sizing_on_rounded_order_prices_never_exceeds_the_limits(seed):
    """What admission checks (quantity on the tick-rounded limit and stop) is what will be
    sent; whatever the raw strategy prices, an admitted size stays within the limits."""
    rng = random.Random(seed)
    checked = 0
    for _ in range(CASES):
        entry_high, stop, limit, cap = size_case(rng)
        lim_price, stop_price = round_to_tick(entry_high), round_to_tick(stop)
        if not ZERO < stop_price < lim_price:
            continue
        q = max_quantity(limit, lim_price - stop_price, cap, lim_price)
        result = check_size(q, lim_price, stop_price, limit, cap)
        assert result.ok == (q >= 1)
        if result.ok:
            checked += 1
            assert q * (lim_price - stop_price) <= limit and q * lim_price <= cap
            assert result.planned_loss_usd == q * (lim_price - stop_price)
    assert checked > CASES // 4


# ---------------------------------------------------------------- daily rule


CEILING = D(300)
MIN_ROOM = D(10)


def daily(realized, reserved, new):
    return check_daily(realized, reserved, new, CEILING, MIN_ROOM)


@pytest.mark.parametrize("seed", SEEDS)
def test_daily_rule_matches_its_formula(seed):
    rng = random.Random(seed)
    for _ in range(CASES):
        realized, reserved, new = money(rng), money(rng, 120), money(rng, 120)
        result = daily(realized, reserved, new)
        room = CEILING - realized - reserved
        ok = room >= MIN_ROOM and realized + reserved + new <= CEILING
        assert result.ok == ok, (realized, reserved, new, result)
        if ok:
            assert new <= room
            assert result.total_usd == realized + reserved + new
        else:
            assert result.cause is C.DAILY_CEILING


@pytest.mark.parametrize("seed", SEEDS)
def test_daily_rule_is_monotone(seed):
    """More loss, more reservation or a bigger trade never turns a refusal into an admission."""
    rng = random.Random(seed)
    for _ in range(CASES):
        realized, reserved, new = money(rng), money(rng, 120), money(rng, 120)
        more = [money(rng, 50) for _ in range(3)]
        base = daily(realized, reserved, new)
        worse = daily(realized + more[0], reserved + more[1], new + more[2])
        if not base.ok:
            assert not worse.ok, (realized, reserved, new, more)
        if worse.ok:
            assert base.ok


@pytest.mark.parametrize("seed", SEEDS)
def test_daily_rule_boundaries(seed):
    """Total exactly at the ceiling is admitted, one cent over is refused; room exactly at the
    minimum is admitted, one cent below it is refused even for a one-cent trade."""
    rng = random.Random(seed)
    for _ in range(CASES // 10):
        realized = money(rng, 290)
        reserved = min(money(rng, 100), CEILING - MIN_ROOM - realized)
        room = CEILING - realized - reserved
        assert room >= MIN_ROOM
        assert daily(realized, reserved, room).ok
        assert daily(realized, reserved, room + CENT).cause is C.DAILY_CEILING
        at_minimum = CEILING - MIN_ROOM - reserved
        assert daily(at_minimum, reserved, MIN_ROOM).ok
        assert daily(at_minimum + CENT, reserved, CENT).cause is C.DAILY_CEILING


ODD = (None, D(-1), D("-0.01"), D("NaN"), D("sNaN"), D("Infinity"), D("-Infinity"),
       1.5, 10, D(5))


@pytest.mark.parametrize("seed", SEEDS)
def test_daily_rule_never_counts_odd_money(seed):
    rng = random.Random(seed)
    for _ in range(CASES // 4):
        realized, reserved, new = (rng.choice(ODD) for _ in range(3))
        if new is None:
            new = D(5)
        result = daily(realized, reserved, new)
        values = (realized, reserved, new)
        if realized is None or reserved is None:
            assert result.cause is C.DAILY_MONEY_UNKNOWN, values
        elif any(not isinstance(v, Decimal) or not v.is_finite() or v < 0 for v in values):
            assert result.cause is C.DAILY_MONEY_INVALID, values
        else:
            assert result.ok, values


# ---------------------------------------------------------------- admit(): first failure wins


LIM = limits()
TODAY = NOW.date()
HALF_DAY = date(2026, 11, 27)


def at(day: date, hh: int, mm: int) -> datetime:
    return datetime(day.year, day.month, day.day, hh, mm, tzinfo=NEW_YORK)


def build(now: datetime, day: date | None = None) -> dict:
    """Admissible inputs (as a field dict) relative to `now`."""
    base = inputs()
    fields = {f: getattr(base, f) for f in base.__dataclass_fields__}
    fields.update(
        now=now, session=CALENDAR.day(day or now.date()),
        reconciliation=replace(base.reconciliation, last_broker_read_at=now - timedelta(seconds=2)),
        buying_power=replace(base.buying_power, read_at=now - timedelta(seconds=2)),
        quote=quote(provider_time=now - timedelta(seconds=1),
                    receipt_time=now - timedelta(milliseconds=500)))
    return fields


def _q(f: dict, **changes) -> None:
    if f["quote"] is not None:
        f["quote"] = replace(f["quote"], **changes)


def _d(f: dict, **changes) -> None:
    f["decision"] = replace(f["decision"], **changes)


def _r(f: dict, **changes) -> None:
    f["reconciliation"] = replace(f["reconciliation"], **changes)


# One breaker per check, in the documented order (spec 7, AdmissionCause order). Each makes
# exactly its own check fail and leaves every earlier check passing.
BREAKERS = [
    (C.GATE_CLOSED, lambda f: f.update(gate=GateResult(False, "closed"))),
    (C.RECONCILIATION_NOT_DONE, lambda f: _r(f, startup_done=False)),
    (C.BROKER_READ_MISSING, lambda f: _r(f, last_broker_read_at=None)),
    (C.BROKER_READ_STALE, lambda f: _r(f, last_broker_read_at=f["now"] - timedelta(hours=1))),
    (C.UNRESOLVED_EXPOSURE, lambda f: _r(f, unresolved_exposure=True)),
    (C.FOREIGN_EXPOSURE, lambda f: _r(f, foreign_exposure=True)),
    (C.ENTRY_STOP_SET, lambda f: f.update(entry_stop=True)),
    (C.COUNT_INVALID, lambda f: f.update(open_positions=True)),
    (C.OWN_POSITION_OPEN, lambda f: f.update(own_open_positions=1)),
    (C.OWN_ORDER_WORKING, lambda f: f.update(own_working_orders=2)),
    (C.POSITION_LIMIT_REACHED, lambda f: f.update(open_positions=LIM.max_open_positions)),
    (C.SYMBOL_COOLDOWN, lambda f: f.update(symbol_last_loss_at=f["now"] - timedelta(minutes=5))),
    (C.DECISION_NOT_ENTER, lambda f: _d(f, verdict=Verdict.WATCH)),
    (C.DECISION_NOT_ACTIVE, lambda f: _d(f, strategy="someone-else")),
    (C.DECISION_INCOMPLETE, lambda f: _d(f, stop=None)),
    (C.DECISION_INVALID, lambda f: _d(f, entry_low=D("10.30"))),
    (C.ORDER_PRICES_MISSING, lambda f: f.update(limit_price=None)),
    (C.ORDER_PRICES_INVALID, lambda f: f.update(stop_price=D("10.25"))),
    (C.SETUP_INVALIDATED, lambda f: f.update(setup_valid=False)),
    (C.QUOTE_MISSING, lambda f: f.update(quote=None)),
    (C.QUOTE_SYMBOL_MISMATCH, lambda f: _q(f, symbol="WXYZ")),
    (C.QUOTE_INVALID, lambda f: _q(f, bid=f["quote"].ask + D("0.05"))),
    (C.QUOTE_TIME_INVALID, lambda f: _q(f, receipt_time=f["now"] + timedelta(seconds=1))),
    (C.QUOTE_STALE, lambda f: _q(f, provider_time=f["now"] - timedelta(minutes=1))),
    (C.PRICE_OUTSIDE_WINDOW, lambda f: _q(f, bid=D("9.90"), ask=D("9.95"))),
    (C.QUANTITY_NOT_WHOLE, lambda f: _d(f, quantity=D("10.5"))),
    (C.QUANTITY_BELOW_ONE, lambda f: _d(f, quantity=D(0))),
    (C.NOTIONAL_OVER_CAP, lambda f: _d(f, quantity=D(1000))),
    (C.PLANNED_LOSS_OVER_LIMIT, lambda f: _d(f, quantity=D(200))),
    (C.DAILY_MONEY_UNKNOWN, lambda f: f.update(realized_loss_today_usd=None)),
    (C.DAILY_MONEY_INVALID, lambda f: f.update(open_reservations_usd=D(-1))),
    (C.DAILY_CEILING, lambda f: f.update(realized_loss_today_usd=D(295))),
    (C.ENTRY_BUDGET_UNAVAILABLE, lambda f: f.update(entry_budget_available=False)),
    (C.STOP_BUDGET_UNAVAILABLE, lambda f: f.update(stop_budget_available=False)),
]
RANK = {cause: index for index, (cause, _) in enumerate(BREAKERS)}

# Breakers of check 3 change `now` and the calendar answer together, so at most one applies;
# it ranks between the entry-stop flag and the counts. Each is (cause, now, calendar day).
SESSION = [
    (C.CALENDAR_UNKNOWN, NOW, CalendarRefused(CalendarCause.CALENDAR_INVALID, "test")),
    (C.CALENDAR_DAY_MISMATCH, NOW, TODAY + timedelta(days=1)),
    (C.HALF_DAY, at(HALF_DAY, 10, 0), HALF_DAY),
    (C.OUTSIDE_REGULAR_SESSION, at(TODAY, 9, 29), TODAY),
    (C.AFTER_LAST_ENTRY, at(TODAY, 15, 45), TODAY),
]
SESSION_RANK = RANK[C.ENTRY_STOP_SET] + 0.5
# The naive clock fails check 2 before anything else in it.
CLOCK_RANK = RANK[C.GATE_CLOSED] + 0.5


def run(fields: dict) -> Admission:
    return admit(replace(inputs(), **fields), LIM)


def test_each_breaker_alone_gives_its_cause():
    assert run(build(NOW)).ok
    for cause, breaker in BREAKERS:
        fields = build(NOW)
        breaker(fields)
        assert run(fields).cause is cause
    for cause, now, day in SESSION:
        fields = build(now, day if isinstance(day, date) else None)
        if not isinstance(day, date):
            fields["session"] = day
        assert run(fields).cause is cause


@pytest.mark.parametrize("seed", SEEDS)
def test_the_cause_is_always_the_first_failing_check(seed):
    rng = random.Random(seed)
    for _ in range(CASES // 2):
        chosen = sorted(rng.sample(range(len(BREAKERS)), rng.randint(1, 6)))
        session = rng.choice([None, *SESSION]) if rng.random() < 0.4 else None
        naive = rng.random() < 0.05
        now = session[1] if session else NOW
        day = session[2] if session and isinstance(session[2], date) else None
        fields = build(now, day)
        if session and not isinstance(session[2], date):
            fields["session"] = session[2]
        # Later checks first, so an earlier breaker that shares a field has the last word.
        for index in reversed(chosen):
            BREAKERS[index][1](fields)
        ranks = [(index, BREAKERS[index][0]) for index in chosen]
        if session:
            ranks.append((SESSION_RANK, session[0]))
        if naive:
            fields["now"] = fields["now"].replace(tzinfo=None)
            ranks.append((CLOCK_RANK, C.CLOCK_INVALID))
        expected = min(ranks, key=lambda pair: pair[0])[1]
        result = run(fields)
        assert not result.ok
        assert result.cause is expected, (seed, [c for _, c in sorted(ranks)], result)


# ---------------------------------------------------------------- admit(): never raises


def _odd_time(rng: random.Random, now: datetime):
    return rng.choice([None, now.replace(tzinfo=None), now + timedelta(days=1),
                       now - timedelta(days=1), now, now.astimezone(NEW_YORK).replace(
                           tzinfo=None), datetime(2026, 11, 16, 15, 0, tzinfo=NEW_YORK)])


def _odd_number(rng: random.Random):
    return rng.choice([None, D(0), D(-1), D("NaN"), D("sNaN"), D("Infinity"), D("-Infinity"),
                       D("1E-30"), D("1E+20"), D("10.10"), D("9.80"), D("0.0001"), 3, 2.5,
                       True])


def _odd_count(rng: random.Random):
    return rng.choice([0, 1, 2, -1, True, False, None, 1.0, D(1)])


FUZZ_FIELDS = {
    "gate": lambda rng, f: GateResult(rng.random() < 0.8, "x"),
    "now": lambda rng, f: _odd_time(rng, NOW),
    "session": lambda rng, f: rng.choice([
        None, CALENDAR.day(TODAY), CALENDAR.day(HALF_DAY), CALENDAR.day(date(2026, 3, 9)),
        CalendarRefused(CalendarCause.CALENDAR_INVALID, "x"), "not a day"]),
    "reconciliation": lambda rng, f: replace(
        f["reconciliation"], startup_done=rng.random() < 0.9,
        last_broker_read_at=_odd_time(rng, f["now"] if isinstance(f["now"], datetime) else NOW)),
    "own_open_positions": lambda rng, f: _odd_count(rng),
    "own_working_orders": lambda rng, f: _odd_count(rng),
    "open_positions": lambda rng, f: _odd_count(rng),
    "symbol_last_loss_at": lambda rng, f: _odd_time(rng, NOW),
    "decision": lambda rng, f: replace(
        f["decision"], **{rng.choice(["entry_low", "entry_high", "stop", "quantity"]):
                          _odd_number(rng)},
        verdict=rng.choice(list(Verdict)) if rng.random() < 0.2 else Verdict.ENTER),
    "limit_price": lambda rng, f: _odd_number(rng),
    "stop_price": lambda rng, f: _odd_number(rng),
    "quote": lambda rng, f: None if rng.random() < 0.1 else replace(
        f["quote"] or quote(), **{rng.choice(["bid", "ask"]): _odd_number(rng)},
        provider_time=_odd_time(rng, NOW) or NOW,
        receipt_time=_odd_time(rng, NOW) or NOW),
    "realized_loss_today_usd": lambda rng, f: _odd_number(rng),
    "open_reservations_usd": lambda rng, f: _odd_number(rng),
}


@pytest.mark.parametrize("seed", SEEDS)
def test_admit_never_raises_on_odd_inputs(seed):
    """Unknown status, missing field, nonsense value: a typed refusal, never an exception
    (CLAUDE.md rule 7). Values stay within what a buggy caller could plausibly pass."""
    rng = random.Random(seed)
    for case in range(CASES):
        fields = build(NOW)
        for name in rng.sample(sorted(FUZZ_FIELDS), rng.randint(1, 4)):
            fields[name] = FUZZ_FIELDS[name](rng, fields)
        try:
            result = run(fields)
        except Exception as exc:  # noqa: BLE001 - the property is "no exception at all"
            raise AssertionError(f"seed {seed} case {case}: {exc!r} for {fields}") from exc
        assert isinstance(result, Admission)
        assert result.ok == (result.cause is None)
        if result.ok:
            assert result.planned_loss_usd is not None
            assert ZERO < result.planned_loss_usd <= LIM.planned_loss_usd


@pytest.mark.parametrize("seed", SEEDS)
def test_an_admitted_entry_stays_within_every_money_limit(seed):
    """Random sizes, prices and day money around the baseline: whenever admit() says yes,
    the reservation it returns fits the trade limits and the room under the ceiling."""
    rng = random.Random(seed)
    admitted = 0
    for _ in range(CASES):
        fields = build(NOW)
        entry_low = D(rng.randint(500, 2000)) / D(100)
        entry_high = entry_low + D(rng.randint(0, 100)) / D(1000)
        stop_raw = entry_low - D(rng.randint(1, 200)) / D(1000)
        limit, stop = round_to_tick(entry_high), round_to_tick(stop_raw)
        quantity = D(rng.randint(0, 400))
        _d(fields, entry_low=entry_low, entry_high=entry_high, stop=stop_raw, quantity=quantity)
        fields.update(limit_price=limit, stop_price=stop,
                      realized_loss_today_usd=money(rng, 300),
                      open_reservations_usd=money(rng, 60))
        _q(fields, bid=entry_low, ask=entry_low)
        result = run(fields)
        if not result.ok:
            continue
        admitted += 1
        loss = result.planned_loss_usd
        room = (LIM.max_daily_loss_usd - fields["realized_loss_today_usd"]
                - fields["open_reservations_usd"])
        assert loss == quantity * (limit - stop)
        assert loss <= LIM.planned_loss_usd and loss <= room and room >= LIM.min_daily_room_usd
        assert quantity * limit <= LIM.position_notional_cap_usd
    assert admitted > CASES // 20


# ---------------------------------------------------------------- cooldown boundary


@pytest.mark.parametrize("seed", SEEDS)
def test_cooldown_boundary_is_exact_at_any_loss_time(seed):
    rng = random.Random(seed)
    minutes = LIM.symbol_cooldown_minutes_after_loss
    assert minutes is not None
    off = replace(LIM, symbol_cooldown_minutes_after_loss=None)
    for _ in range(CASES // 10):
        loss_at = at(TODAY, 9, 30) + timedelta(seconds=rng.randint(0, 5 * 3600),
                                               microseconds=rng.randint(0, 999_999))
        if rng.random() < 0.5:
            loss_at = loss_at.astimezone(UTC)
        end = loss_at + timedelta(minutes=minutes)
        for now, expected in ((end, None), (end - timedelta(seconds=1), C.SYMBOL_COOLDOWN),
                              (end - timedelta(microseconds=1), C.SYMBOL_COOLDOWN)):
            if not at(TODAY, 9, 30) <= now < at(TODAY, 15, 45):
                continue
            fields = build(now)
            fields["symbol_last_loss_at"] = loss_at
            assert run(fields).cause is expected, (loss_at, now)
            assert admit(replace(inputs(), **fields), off).ok
