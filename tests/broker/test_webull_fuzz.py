"""Seeded mutation tests of the Webull adapter's readers and send paths.

Every recorded fixture (and the representative bodies the other broker tests build) is mutated
field by field and at random with fixed seeds: fields dropped, set to None, "", wrong types,
negative, NaN, Infinity, huge numbers, unknown or lower-case statuses, another account, extra
unknown fields, duplicate rows, empty and non-list bodies. The property is always the same:
a reader returns a value equal to what an independent oracle reads off the body, or raises a
typed error (`BrokerError`, or `MarketDataError` for market data). Never a bare KeyError,
TypeError, ValueError or InvalidOperation, never a defaulted value, and never a state other than
UNKNOWN for a status outside the spec-v1 section 9 map.

The oracle never pins acceptance of nonsense (hard rule 7). Where today's reader passes a value
a stricter one may refuse or treat as absent (a blank string; a fill, stop or limit price, a bid
or ask, or a position cost that is not positive), the oracle forks: the reading "as today" and
the hardened one are both accepted, so hardening is never blocked. The clear violations are
pinned as strict xfail bugs in tests/broker/test_bug_nonsense_values.py.

Out-of-range epoch and bar times are left out of the market-data fuzz here: they escape as
bare exceptions (tests/broker/test_bug_time_out_of_range.py).
"""

from __future__ import annotations

import copy
import json
import random
from collections.abc import Callable, Iterator, Mapping
from datetime import UTC, datetime
from decimal import Decimal as D
from decimal import InvalidOperation
from pathlib import Path
from typing import Any

import pytest

from tickr.broker.base import (
    BrokerError,
    OrderNotFound,
    OutcomeUnknown,
    RateLimited,
    Refused,
    ReplaceAck,
)
from tickr.broker.status import map_status
from tickr.broker.webull import (
    DETAIL_NOT_VISIBLE,
    BrokerReadFailed,
    ForeignOrdersUnreadable,
    MalformedResponse,
    Route,
    TransportFailure,
    WireResponse,
    parse_json,
    read_accounts,
    read_acknowledgement,
    read_bars,
    read_daily_bars,
    read_history_bars,
    read_order_detail,
    read_order_rows,
    read_positions,
    read_profiles,
    read_replace_ack,
    read_screener,
    read_snapshot,
)
from tickr.data.base import BarSpan, DelayedData, MarketDataError, ScreenerKind
from tickr.types import OrderDetail, OrderState, OrderType, Side

from .test_webull_broker import OWN, broker, limit_buy

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "webull"
CID = "probe-d13d7316437b4405b9a3a655c0"
PREFIXES = ("ap-", "probe-")
RECEIPT = datetime(2026, 11, 13, 15, 0, tzinfo=UTC)
QUOTE_MS = 1794431115431  # recorded example value of snapshot quote_time
NON_TRADING = "OPENAPI_CAN_NOT_TRADING_FOR_NON_TRADING_HOURS"
PARAM_ERR = "OPENAPI_PARAM_ERR"

TYPED = (BrokerError, MarketDataError)


class _Missing:
    def __repr__(self) -> str:
        return "MISSING"


MISSING = _Missing()  # the field is dropped from its object

# Values every field is set to. Python floats never come out of parse_json (numbers with a
# fraction become Decimal), so 1.5 stands for a transport that skipped it.
MUTANTS: tuple[Any, ...] = (
    MISSING, None, "", " ", [], {}, ["x"], {"k": "v"}, True, False, 0, 1, -1, 1.5,
    D("-1"), D("0"), D("NaN"), D("sNaN"), D("Infinity"), D("1E+30"),
    "-1", "0", "-0", "1.5", "abc", "NaN", "-NaN", "sNaN", "Infinity", "-Infinity", "inf",
    "1e999999999999999999999", "9" * 40, "1e30",
    "filled", "submitted", "Filled", "PARTIAL-FILLED", "PENDING_CANCEL", "REPLACED",
    "OTHER-ACCOUNT", "ap-OTHER", "BUY ", "sell",
)

# Statuses the spec maps; everything else must be UNKNOWN (spec-v1 section 9).
SPEC_STATUS = {
    "PENDING": OrderState.ACCEPTED,
    "SUBMITTED": OrderState.ACCEPTED,
    "PARTIAL_FILLED": OrderState.PARTIALLY_FILLED,
    "PARTIAL FILLED": OrderState.PARTIALLY_FILLED,
    "FILLED": OrderState.FILLED,
    "CANCELLED": OrderState.CANCELLED,
    "FAILED": OrderState.REJECTED,
    "EXPIRED": OrderState.EXPIRED,
}


def own_account(account_id: str) -> bool:
    return account_id == OWN


def own_order(cid: str) -> bool:
    return cid.startswith(PREFIXES)


def load(name: str) -> Any:
    raw = json.loads((FIXTURES / name).read_text("utf-8"))
    return parse_json(json.dumps(raw["body"]))


# --------------------------------------------------------------------------------------------
# Independent oracle: what a strict reader may conclude from one value
# --------------------------------------------------------------------------------------------


class Refuse(Exception):
    """The oracle says the body must be refused (a typed error)."""


class _Refused:
    def __repr__(self) -> str:
        return "REFUSED"


REFUSED = _Refused()  # a reading in which the body is refused


class Choices:
    """One path through the oracle: each doubtful point forks into today's reading (False)
    and a hardened one (True); `readings` walks every path."""

    def __init__(self, prefix: list[bool]) -> None:
        self.prefix = prefix
        self.made: list[bool] = []
        self.forks: list[list[bool]] = []

    def hardened(self) -> bool:
        index = len(self.made)
        if index < len(self.prefix):
            pick = self.prefix[index]
        else:
            pick = False
            self.forks.append([*self.made, True])
        self.made.append(pick)
        return pick


def readings(oracle: Callable[[Choices], Any]) -> list[Any]:
    """Every result the oracle allows: values, and REFUSED where a path refuses."""
    found: list[Any] = []
    pending: list[list[bool]] = [[]]
    while pending:
        choices = Choices(pending.pop())
        try:
            found.append(oracle(choices))
        except Refuse:
            found.append(REFUSED)
        pending.extend(choices.forks)
    return found


def absent(value: Any) -> bool:
    return value is MISSING or value is None or value == ""


def blank(value: Any) -> bool:
    return isinstance(value, str) and value != "" and not value.strip()


def missing(value: Any, choices: Choices | None = None) -> bool:
    """Absent; or blank, where a hardened reader may treat blank as absent."""
    return absent(value) or (choices is not None and blank(value) and choices.hardened())


def implausible(choices: Choices) -> None:
    """A nonsense value today's reader passes: a hardened reader may refuse it."""
    if choices.hardened():
        raise Refuse


def get(obj: Mapping[str, Any], key: str) -> Any:
    return obj.get(key, MISSING)


def number(value: Any) -> D:
    """A finite Decimal from a str, int or Decimal; never float or bool. Refuse otherwise."""
    if absent(value) or isinstance(value, bool | float):
        raise Refuse
    if isinstance(value, D):
        result = value
    elif isinstance(value, int):
        result = D(value)
    elif isinstance(value, str):
        try:
            result = D(value.strip())
        except InvalidOperation:
            raise Refuse from None
    else:
        raise Refuse
    if not result.is_finite():
        raise Refuse
    return result


def optional_number(value: Any, choices: Choices | None = None) -> D | None:
    return None if missing(value, choices) else number(value)


def positive_price(value: Any, choices: Choices) -> D | None:
    """An optional price: a present one that is not positive may be refused."""
    price = optional_number(value, choices)
    if price is not None and price <= 0:
        implausible(choices)
    return price


def text(value: Any, choices: Choices | None = None) -> str:
    if missing(value, choices):
        raise Refuse
    return str(value)


def spec_state(raw: str, quantity: D, filled: D) -> OrderState:
    """spec-v1 section 9, written out independently of broker/status.py."""
    state = SPEC_STATUS.get(raw)
    if state is None or quantity <= 0 or filled < 0 or filled > quantity:
        return OrderState.UNKNOWN
    if state is OrderState.ACCEPTED and filled != 0:
        return OrderState.UNKNOWN
    if state is OrderState.PARTIALLY_FILLED and not 0 < filled < quantity:
        return OrderState.UNKNOWN
    if state is OrderState.FILLED and filled != quantity:
        return OrderState.UNKNOWN
    if state is OrderState.REJECTED and filled != 0:
        return OrderState.UNKNOWN
    return state


def check_account(obj: Any) -> None:
    if isinstance(obj, Mapping) and "account_id" in obj and str(obj["account_id"]) != OWN:
        raise Refuse


def expected_order(row: Any, choices: Choices) -> OrderDetail:
    """What read_order may return for `row` on this path; Refuse if it must be refused."""
    if not isinstance(row, Mapping):
        raise Refuse
    check_account(row)
    raw = text(get(row, "status"), choices).strip()
    quantity = number(get(row, "total_quantity"))
    filled = number(get(row, "filled_quantity"))
    side = text(get(row, "side"), choices)
    order_type = text(get(row, "order_type"), choices)
    if side not in {s.value for s in Side} or order_type not in {t.value for t in OrderType}:
        raise Refuse
    fill_price = optional_number(get(row, "filled_price"), choices)
    if fill_price is not None and fill_price <= 0 and filled > 0:
        implausible(choices)  # a fill at no price: tests/broker/test_bug_nonsense_values.py
    limit = get(row, "limit_price")  # not read today; a hardened reader may check it
    if not missing(limit, choices):
        try:
            limit_ok = number(limit) > 0
        except Refuse:
            limit_ok = False
        if not limit_ok:
            implausible(choices)
    order_id = get(row, "order_id")
    return OrderDetail(
        client_order_id=text(get(row, "client_order_id"), choices),
        symbol=text(get(row, "symbol"), choices),
        side=Side(side),
        order_type=OrderType(order_type),
        quantity=quantity,
        filled_quantity=filled,
        average_fill_price=fill_price,
        state=spec_state(raw, quantity, filled),
        raw_status=raw,
        stop_price=positive_price(get(row, "stop_price"), choices),
        broker_order_id=None if missing(order_id, choices) else str(order_id),
    )


def assert_one_reading(body: Any, got: Any, error: BaseException | None,
                       paths: list[Any]) -> None:
    """The reader's answer is one the oracle allows: a refusal only where a path refuses."""
    if error is not None:
        assert isinstance(error, MalformedResponse) and REFUSED in paths, (body, error, paths)
    else:
        assert got in paths, (body, got, paths)


def assert_order_types(got: OrderDetail) -> None:
    for value in (got.quantity, got.filled_quantity):
        assert type(value) is D and value.is_finite()
    if got.raw_status not in SPEC_STATUS:
        assert got.state is OrderState.UNKNOWN


# --------------------------------------------------------------------------------------------
# Mutation helpers
# --------------------------------------------------------------------------------------------


def with_field(obj: Mapping[str, Any], key: str, value: Any) -> dict[str, Any]:
    out = copy.deepcopy(dict(obj))
    if value is MISSING:
        out.pop(key, None)
    else:
        out[key] = copy.deepcopy(value)
    return out


def field_mutations(obj: Mapping[str, Any], extra: tuple[Any, ...] = (),
                    mutants: tuple[Any, ...] = MUTANTS) -> Iterator[dict]:
    """Every field of `obj` set to every mutant, plus unknown extra fields."""
    for key in obj:
        for value in (*mutants, *extra):
            yield with_field(obj, key, value)
        if isinstance(obj[key], str):
            yield with_field(obj, key, obj[key].lower())
    yield {**copy.deepcopy(dict(obj)), "unexpected_field": {"nested": [1, "2"]}}
    yield {**copy.deepcopy(dict(obj)), "x-extra": None}


def objects_in(value: Any) -> list[dict]:
    found: list[dict] = []
    if isinstance(value, dict):
        found.append(value)
        for v in value.values():
            found.extend(objects_in(v))
    elif isinstance(value, list):
        for v in value:
            found.extend(objects_in(v))
    return found


def random_mutation(body: Any, rng: random.Random, mutants: tuple[Any, ...]) -> Any:
    """1-3 random edits anywhere in a deep copy: drop, replace, add, duplicate, empty."""
    out = copy.deepcopy(body)
    for _ in range(rng.randint(1, 3)):
        targets = [o for o in objects_in(out) if o]
        if not targets:
            break
        target = rng.choice(targets)
        key = rng.choice(list(target))
        roll = rng.random()
        if roll < 0.2:
            target.pop(key)
        elif roll < 0.3:
            target[f"unknown_{rng.randrange(1000)}"] = rng.choice(mutants)
        elif roll < 0.4 and isinstance(target[key], list) and target[key]:
            target[key].append(copy.deepcopy(rng.choice(target[key])))  # duplicate a row
        elif roll < 0.45 and isinstance(target[key], list):
            target[key] = []
        else:
            value = rng.choice(mutants)
            if value is MISSING:
                target.pop(key)
            else:
                target[key] = copy.deepcopy(value)
    return out


def outcome(fn: Callable[[], Any]) -> tuple[Any, BaseException | None]:
    """Run fn; typed errors are an outcome, anything else fails the test with the body."""
    try:
        return fn(), None
    except TYPED as exc:
        return None, exc


def recorded_combo() -> dict:
    return copy.deepcopy(load("historical_orders_cancelled_limit.json")["data"][0])


def row_variants() -> list[dict]:
    """The recorded row and the shapes other tests derive from it (no fill or stop recorded)."""
    base = recorded_combo()["orders"][0]
    filled = {**base, "status": "FILLED", "filled_quantity": "1", "filled_price": "99.98"}
    partial = {**base, "status": "PARTIAL FILLED", "total_quantity": "2",
               "filled_quantity": "1", "filled_price": "100.00"}
    stop = {**base, "client_order_id": "ap-stop-1", "order_type": "STOP_LOSS", "side": "SELL",
            "status": "SUBMITTED", "stop_price": "95.00"}
    stop.pop("limit_price")
    return [base, filled, partial, stop]


# --------------------------------------------------------------------------------------------
# map_status
# --------------------------------------------------------------------------------------------

STATUS_INPUTS = (
    *SPEC_STATUS, "", " ", "filled", "Filled", "submitted", "partial_filled", "PARTIAL  FILLED",
    "PARTIALFILLED", "PENDING_CANCEL", "PENDING_REPLACE", "REPLACED", "REJECTED", "CANCELED",
    "CANCELLED ", " FILLED", "EXPIRE", "NEW", "UNKNOWN", "None", "0", "FILLED\n",
)
QUANTITY_INPUTS = tuple(D(v) for v in (
    "-1", "-0", "0", "0.5", "1", "2", "3", "1.0", "1E+1", "99", "100", "1E+30"))


def test_map_status_is_exactly_the_spec_map():
    for raw in STATUS_INPUTS:
        for quantity in QUANTITY_INPUTS:
            for filled in QUANTITY_INPUTS:
                got = map_status(raw, quantity, filled)
                assert got is spec_state(raw, quantity, filled), (raw, quantity, filled)


def test_map_status_never_maps_an_unknown_status():
    rng = random.Random(9101)
    alphabet = "ABCDEFILNPRSTU _-abcdefilnprstu"
    for _ in range(3000):
        raw = "".join(rng.choice(alphabet) for _ in range(rng.randint(0, 16)))
        quantity = D(rng.randint(-2, 50))
        filled = D(rng.randint(-2, 50))
        got = map_status(raw, quantity, filled)
        if raw not in SPEC_STATUS:
            assert got is OrderState.UNKNOWN, raw
        assert got is spec_state(raw, quantity, filled)


def test_map_status_partial_fill_boundaries():
    for quantity in range(1, 30):
        for filled in range(0, quantity + 1):
            q, f = D(quantity), D(filled)
            assert map_status("CANCELLED", q, f) is OrderState.CANCELLED  # fill may be > 0
            assert map_status("EXPIRED", q, f) is OrderState.EXPIRED
            partial = map_status("PARTIAL FILLED", q, f)
            assert (partial is OrderState.PARTIALLY_FILLED) == (0 < filled < quantity)
            assert (map_status("FILLED", q, f) is OrderState.FILLED) == (filled == quantity)


# --------------------------------------------------------------------------------------------
# Order rows: Detail, open orders
# --------------------------------------------------------------------------------------------


def check_detail(body: Any) -> None:
    got, error = outcome(lambda: read_order_detail(body, CID, own_account))

    def oracle(choices: Choices) -> Any:
        if not isinstance(body, Mapping) or "orders" not in body:
            raise Refuse
        check_account(body)
        orders = body["orders"]
        if orders == []:
            return DETAIL_NOT_VISIBLE
        if not isinstance(orders, list):
            raise Refuse
        ours = [o for o in orders if isinstance(o, Mapping) and o.get("client_order_id") == CID]
        if len(ours) != 1:
            raise Refuse
        return expected_order(ours[0], choices)

    paths = readings(oracle)
    if paths == [DETAIL_NOT_VISIBLE]:
        assert isinstance(error, BrokerReadFailed) and error.code == DETAIL_NOT_VISIBLE
        return
    assert_one_reading(body, got, error, paths)
    if error is None:
        assert_order_types(got)


def test_detail_every_field_mutated():
    for variant in row_variants():
        variant = {**variant, "client_order_id": CID}
        combo = recorded_combo()
        for row in field_mutations(variant):
            check_detail({**combo, "orders": [row]})


def test_detail_envelope_mutated():
    combo = recorded_combo()
    row = combo["orders"][0]
    other = {**row, "client_order_id": "ap-other"}
    envelopes: list[Any] = [
        None, [], "", "orders", 0, [combo], {"data": [combo]},
        {**combo, "orders": None}, {**combo, "orders": {}}, {**combo, "orders": row},
        {**combo, "orders": [row, row]}, {**combo, "orders": [other]},
        {**combo, "orders": [other, row]}, {**combo, "orders": ["x", None, row]},
        {**combo, "account_id": "OTHER-ACCOUNT"}, {**combo, "account_id": None},
        {**combo, "account_id": OWN}, {"orders": [{**row, "account_id": OWN}]},
        {"orders": [{**row, "account_id": "OTHER-ACCOUNT"}]},
    ]
    for body in envelopes:
        check_detail(body)
    combo.pop("orders")
    check_detail(combo)


def test_detail_random_mutations():
    rng = random.Random(20261116)
    bases = [{**recorded_combo(), "orders": [{**v, "client_order_id": CID}]}
             for v in row_variants()]
    for _ in range(1500):
        check_detail(random_mutation(rng.choice(bases), rng, MUTANTS))


def check_rows(body: Any) -> None:
    """Open-orders page: every row is returned, or surfaced as unreadable, or the read fails."""
    got, error = outcome(lambda: read_order_rows(body, own_account, own_order, "open_orders"))

    def oracle(choices: Choices) -> tuple[list[OrderDetail], list[str]]:
        if isinstance(body, list):
            combos = body
        elif isinstance(body, Mapping) and isinstance(body.get("data"), list):
            combos = body["data"]
        else:
            raise Refuse
        readable: list[OrderDetail] = []
        unreadable: list[str] = []
        for combo in combos:
            if not isinstance(combo, Mapping):
                raise Refuse
            check_account(combo)
            nested = get(combo, "orders")
            if absent(nested) or not isinstance(nested, list):
                raise Refuse
            for row in nested:
                check_account(row)
                cid = str(row.get("client_order_id") or "") if isinstance(row, Mapping) else ""
                try:
                    readable.append(expected_order(row, choices))
                except Refuse:
                    if cid and own_order(cid):
                        raise
                    unreadable.append(cid)
        return readable, unreadable

    paths = readings(oracle)
    if error is not None:
        assert_one_reading(body, got, error, paths)
        return
    rows, bad = got
    assert_one_reading(body, (rows, [u.client_order_id for u in bad]), error, paths)
    for row in rows:
        assert_order_types(row)
    for item in bad:
        assert item.reason  # one recorded cause per unreadable row


def test_open_order_rows_every_field_mutated():
    combo = recorded_combo()
    for variant in row_variants():
        for cid in ("ap-1", "app-manual", ""):
            for row in field_mutations({**variant, "client_order_id": cid}):
                check_rows({"data": [{**combo, "orders": [row, variant]}]})


def test_open_order_rows_random_pages():
    rng = random.Random(4242)
    variants = row_variants()
    junk: list[Any] = [None, "row", 7, [], {}]
    for _ in range(600):
        rows = []
        for _ in range(rng.randint(0, 5)):
            row = copy.deepcopy(rng.choice(variants))
            row["client_order_id"] = rng.choice(["ap-1", "ap-2", "probe-x", "app-manual", ""])
            rows.append(row)
        if rng.random() < 0.2:
            rows.append(rng.choice(junk))
        combo = {**recorded_combo(), "orders": rows}
        page = {"data": [combo, copy.deepcopy(combo)][: rng.randint(1, 2)]}
        check_rows(random_mutation(page, rng, MUTANTS) if rng.random() < 0.8 else page)


@pytest.mark.parametrize("body", [None, "", "data", 0, {}, {"data": None}, {"data": {}},
                                  {"data": "x"}, {"orders": []}, [None], [[]], [{"orders": "x"}],
                                  {"data": [{"orders": None}]}, {"data": [{}]}])
def test_open_order_rows_bad_envelopes_are_malformed(body):
    with pytest.raises(MalformedResponse):
        read_order_rows(body, own_account, own_order, "open_orders")


def test_open_orders_over_pages_surfaces_every_unreadable_row():
    """Readable rows of every page come back; unreadable foreign rows of any page are named."""
    rng = random.Random(77)
    variants = row_variants()
    for _ in range(200):
        pages, expect_ok, expect_bad = [], [], []
        count = rng.randint(1, 3)
        for number in range(count):
            rows = []
            for i in range(rng.randint(0, 3)):
                row = copy.deepcopy(rng.choice(variants))
                cid = f"{rng.choice(['ap-', 'manual-'])}{number}-{i}"
                row["client_order_id"] = cid
                if cid.startswith("manual-") and rng.random() < 0.5:
                    row[rng.choice(["side", "order_type", "total_quantity"])] = "SHORT"
                    expect_bad.append(cid)
                else:
                    expect_ok.append(cid)
                rows.append(row)
            body: dict[str, Any] = {"data": [{**recorded_combo(), "orders": rows}]}
            if number < count - 1:
                body["pagination_key"] = f"k{number}"
            pages.append(WireResponse(200, body))
        b, t, _ = broker(*pages, max_pages=3)
        if expect_bad:
            with pytest.raises(ForeignOrdersUnreadable) as exc:
                b.open_orders()
            got_ok = [o.client_order_id for o in exc.value.orders]
            assert [u.client_order_id for u in exc.value.unreadable] == expect_bad
        else:
            got_ok = [o.client_order_id for o in b.open_orders()]
        assert got_ok == expect_ok
        assert len(t.sent) == count


# --------------------------------------------------------------------------------------------
# Positions, accounts, acknowledgements
# --------------------------------------------------------------------------------------------

POSITION = {"symbol": "AAPL", "quantity": "3", "cost_price": "100.25"}
SHORT = {"symbol": "MSFT", "quantity": "-2", "cost_price": "410.50"}


def repeats(rows: Any) -> bool:
    """A reading that lists one symbol twice (tests/broker/test_bug_duplicate_positions.py)."""
    if rows is REFUSED:
        return False
    symbols = [row[0] for row in rows]
    return len(symbols) != len(set(symbols))


def one_row_per_symbol(got: list[tuple], want: list[tuple]) -> bool:
    """An accepted fix of the duplicate-positions bug: each symbol once; a symbol the body
    lists once keeps its own row, a repeated one may come back merged in any way."""
    symbols = [row[0] for row in want]
    once = [row for row in want if symbols.count(row[0]) == 1]
    return (not repeats(got) and {row[0] for row in got} == set(symbols)
            and all(row in once for row in got if symbols.count(row[0]) == 1))


def check_positions(body: Any) -> None:
    got, error = outcome(lambda: read_positions(body, own_account))

    def oracle(choices: Choices) -> list[tuple]:
        if isinstance(body, list):
            rows = body
        elif isinstance(body, Mapping) and isinstance(body.get("data"), list):
            rows = body["data"]
        else:
            raise Refuse
        want = []
        for row in rows:
            if not isinstance(row, Mapping):
                raise Refuse
            check_account(row)
            want.append((text(get(row, "symbol"), choices), number(get(row, "quantity")),
                         positive_price(get(row, "cost_price"), choices)))
        return want

    paths = readings(oracle)
    duplicated = [p for p in paths if repeats(p)]
    if duplicated:  # a repeated symbol must be refused or come back once
        paths = [*(p for p in paths if not repeats(p)), REFUSED]
    if error is not None:
        assert_one_reading(body, got, error, paths)
        return
    rows = [(p.symbol, p.quantity, p.average_cost) for p in got]
    if duplicated and repeats(rows):
        return  # today's reader: excluded here, pinned by the strict xfail bug test
    assert rows in paths or any(one_row_per_symbol(rows, want) for want in duplicated), \
        (body, rows, paths)


def test_positions_every_field_mutated():
    for row in field_mutations({**POSITION, "account_id": OWN}):
        check_positions([row])
        check_positions({"data": [row, SHORT]})


def test_positions_random_mutations():
    rng = random.Random(31337)
    bases: list[Any] = [load("positions_empty.json"), [POSITION], {"data": [POSITION]},
                        [POSITION, SHORT]]
    for _ in range(1000):
        check_positions(random_mutation(rng.choice(bases), rng, MUTANTS))
    for body in (None, "", 0, {}, {"data": None}, {"data": "x"}, [None], ["AAPL"], [[]]):
        check_positions(body)


def test_accounts_mutated():
    rng = random.Random(5)
    bases = [[{"account_id": OWN}], {"data": [{"account_id": OWN}, {"account_id": "B"}]}]
    for _ in range(500):
        body = random_mutation(rng.choice(bases), rng, (*MUTANTS, OWN))
        got, error = outcome(lambda b=body: read_accounts(b, own_account))
        if error is not None:
            assert isinstance(error, MalformedResponse)
            continue
        rows = body if isinstance(body, list) else body["data"]
        listed = [r for r in rows if isinstance(r, Mapping) and str(r.get("account_id")) == OWN]
        assert len(listed) <= 1
        assert got.account_present is got.trading_permitted is (len(listed) == 1)
    with pytest.raises(MalformedResponse):
        read_accounts([{"account_id": OWN}, {"account_id": OWN}], own_account)
    for body in (None, {}, "x", {"data": None}):
        with pytest.raises(MalformedResponse):
            read_accounts(body, own_account)


ACK = {"client_order_id": "ap-1", "order_id": "X9"}


def valid_ack(body: Any) -> bool:
    if not isinstance(body, Mapping) or "error_code" in body:
        return False
    try:
        check_account(body)
        echoed, order_id = text(get(body, "client_order_id")), text(get(body, "order_id"))
    except Refuse:
        return False
    return echoed == "ap-1" and bool(order_id.strip())


def check_ack(body: Any) -> None:
    got, error = outcome(lambda: read_acknowledgement(body, "ap-1", own_account))
    if valid_ack(body):
        assert error is None, (body, error)
        assert got.client_order_id == "ap-1"
        assert got.broker_order_id == str(body["order_id"]).strip()
    else:
        assert isinstance(error, MalformedResponse), (body, got)


def test_acknowledgement_mutated():
    for body in field_mutations({**ACK, "account_id": OWN}, extra=("  ", "ap-1")):
        check_ack(body)
    rng = random.Random(8)
    for _ in range(500):
        check_ack(random_mutation({**ACK, "account_id": OWN}, rng, MUTANTS))
    for body in (None, "", [], [ACK], "ap-1", {**ACK, "error_code": None},
                 {**ACK, "error_code": "X"}, {"data": ACK}):
        check_ack(body)


def test_replace_ack_mutated():
    rng = random.Random(13)
    for _ in range(500):
        body = random_mutation({**ACK, "account_id": OWN}, rng, MUTANTS)
        for candidate in (body, [body], None, "ok"):
            got, error = outcome(lambda c=candidate: read_replace_ack(c))
            if isinstance(candidate, Mapping) and "error_code" in candidate:
                assert isinstance(error, MalformedResponse)
                continue
            assert error is None and isinstance(got, ReplaceAck)
            if isinstance(candidate, Mapping):
                for key, value in (("client_order_id", got.client_order_id),
                                   ("order_id", got.broker_order_id)):
                    raw = get(candidate, key)
                    # blank may be read as absent by a hardened reader
                    assert value == (None if absent(raw) else str(raw)) or (
                        blank(raw) and value is None)
            else:
                assert got == ReplaceAck(None, None)


# --------------------------------------------------------------------------------------------
# Sending: one request, typed outcome, never a retry
# --------------------------------------------------------------------------------------------

HTTP_STATUSES = (0, 100, 200, 201, 202, 204, 299, 300, 301, 304, 400, 401, 403, 404, 408, 409,
                 417, 422, 429, 500, 502, 503, 504, 599)
ERROR_BODIES: tuple[Any, ...] = (
    {"error_code": NON_TRADING, "message": "non trading hours"},
    {"error_code": NON_TRADING.lower(), "message": "non trading hours"},
    {"error_code": PARAM_ERR, "message": "Parameter error, Order not present."},
    {"error_code": PARAM_ERR, "message": "something else"},
    {"error_code": "TOO_MANY_REQUESTS", "message": "Too many requests"},
    {"error_code": "UNPARSEABLE_BODY"},  # what SdkTransport answers for a non-JSON body
    {"error_code": None, "message": None},
    {"error_code": "", "message": ""},
    {"message": "Order not present"},
)
SUCCESS_BODIES: tuple[Any, ...] = (
    None, "", "<html>bad gateway</html>", [], {}, [ACK], ACK, {**ACK, "account_id": OWN},
    {**ACK, "client_order_id": "ap-OTHER"}, {**ACK, "account_id": "OTHER-ACCOUNT"},
    {"client_order_id": "ap-1"}, {**ACK, "order_id": ""}, {**ACK, "order_id": "  "},
    {**ACK, "order_id": None}, {"order_id": "X9"}, {**ACK, "error_code": "X"},
    {"data": ACK}, {"client_order_id": "ap-1", "order_id": 0},
)
TRANSPORT_FAILURES = tuple(TransportFailure(c) for c in (
    "SDK.HttpError", "ReadTimeout", "ConnectTimeout", "SSLError", "ConnectionError", ""))


def answers() -> Iterator[WireResponse | TransportFailure]:
    yield from TRANSPORT_FAILURES
    for status in HTTP_STATUSES:
        for body in (*ERROR_BODIES, *SUCCESS_BODIES):
            yield WireResponse(status, copy.deepcopy(body))


def is_error_envelope(body: Any) -> bool:
    return isinstance(body, Mapping) and "error_code" in body


def not_present(answer: WireResponse) -> bool:
    return answer.error_code == PARAM_ERR and "order not present" in answer.message.lower()


def test_place_sends_once_and_maps_every_answer():
    for answer in answers():
        b, t, _ = broker(answer)
        got, error = outcome(lambda b=b: b.place(limit_buy()))
        assert len(t.sent) == 1 and t.sent[0][0] is Route.PLACE, answer
        if isinstance(answer, WireResponse) and answer.ok and valid_ack(answer.body):
            assert error is None and got.client_order_id == "ap-1", answer
        elif (isinstance(answer, WireResponse) and not answer.ok and answer.status != 429
              and answer.error_code == NON_TRADING):
            # The only code proven to mean "no order created" (error_non_trading_hours.json).
            assert isinstance(error, Refused) and error.code == NON_TRADING, answer
        else:
            # The order may exist: never Refused, never RateLimited, never a resend.
            assert type(error) is OutcomeUnknown, (answer, error)


def test_cancel_sends_once_and_maps_every_answer():
    for answer in answers():
        if isinstance(answer, WireResponse) and answer.ok and is_error_envelope(answer.body):
            continue  # tests/broker/test_bug_cancel_success_with_error_envelope.py
        b, t, _ = broker(answer)
        got, error = outcome(lambda b=b: b.cancel("ap-1"))
        assert len(t.sent) == 1 and t.sent[0][0] is Route.CANCEL, answer
        if isinstance(answer, WireResponse) and answer.ok:
            assert error is None and got is None, answer
        elif isinstance(answer, WireResponse) and answer.status != 429 and not_present(answer):
            assert isinstance(error, OrderNotFound), answer
        else:
            assert type(error) is OutcomeUnknown, (answer, error)


def test_replace_sends_once_and_maps_every_answer():
    for answer in answers():
        b, t, _ = broker(answer)
        got, error = outcome(lambda b=b: b.replace_stop_price("ap-1", D(1), D("9.90")))
        assert len(t.sent) == 1 and t.sent[0][0] is Route.REPLACE, answer
        if isinstance(answer, WireResponse) and answer.ok:
            if is_error_envelope(answer.body):  # not an acknowledgement: Detail decides
                assert type(error) is OutcomeUnknown, (answer, error)
            else:
                assert error is None and isinstance(got, ReplaceAck), answer
        elif isinstance(answer, WireResponse) and answer.status != 429 and not_present(answer):
            assert isinstance(error, OrderNotFound), answer
        else:
            assert type(error) is OutcomeUnknown, (answer, error)


def test_reads_send_once_and_fail_typed():
    """Detail, positions, open orders and accounts: a non-success answer or garbage is a typed
    read failure, sent once (a garbage body carries no pagination key)."""
    reads: dict[Route, Callable[[Any], Any]] = {
        Route.ORDER_DETAIL: lambda b: b.order_detail(CID),
        Route.POSITIONS: lambda b: b.positions(),
        Route.OPEN_ORDERS: lambda b: b.open_orders(),
        Route.ACCOUNTS: lambda b: b.account(),
    }
    for route, read in reads.items():
        for answer in answers():
            b, t, _ = broker(answer)
            _, error = outcome(lambda b=b, read=read: read(b))
            assert [r for r, _ in t.sent] == [route], (route, answer)
            if isinstance(answer, TransportFailure):
                assert type(error) is BrokerReadFailed and error.http_status is None
            elif answer.status == 429:
                assert isinstance(error, RateLimited)
            elif not answer.ok:
                if route is Route.ORDER_DETAIL and not_present(answer):
                    assert isinstance(error, OrderNotFound)
                else:
                    assert type(error) is BrokerReadFailed and error.http_status == answer.status
            assert not isinstance(error, OutcomeUnknown | Refused)  # reads change nothing


def test_recorded_error_fixtures_on_every_trading_route():
    """error_non_trading_hours refuses only a place; error_order_not_present is OrderNotFound on
    cancel and replace but leaves a place in doubt; error_too_many_requests on a POST is doubt."""
    def fixture(name: str) -> WireResponse:
        raw = json.loads((FIXTURES / name).read_text("utf-8"))
        return WireResponse(raw["status"], parse_json(json.dumps(raw["body"])))

    expected = {
        "error_non_trading_hours.json": (Refused, OutcomeUnknown, OutcomeUnknown),
        "error_order_not_present.json": (OutcomeUnknown, OrderNotFound, OrderNotFound),
        "error_too_many_requests.json": (OutcomeUnknown, OutcomeUnknown, OutcomeUnknown),
    }
    for name, (place, cancel, replace_) in expected.items():
        sends = (lambda b: b.place(limit_buy()), lambda b: b.cancel("ap-1"),
                 lambda b: b.replace_stop_price("ap-1", D(1), D("9.90")))
        for send, error_type in zip(sends, (place, cancel, replace_), strict=True):
            b, t, _ = broker(fixture(name))
            with pytest.raises(error_type):
                send(b)
            assert len(t.sent) == 1


# --------------------------------------------------------------------------------------------
# Market data
# --------------------------------------------------------------------------------------------

# quote_time and bar `time` are fuzzed with in-range values only; see the module docstring.
OUT_OF_RANGE_TIMES = {"9" * 40, "1e30", "1e999999999999999999999"}
TIME_MUTANTS = tuple(v for v in MUTANTS
                     if not (isinstance(v, str) and v in OUT_OF_RANGE_TIMES)
                     and not (isinstance(v, D) and v.is_finite() and abs(v) > 10**15))

SNAPSHOT = {"symbol": "AAPL", "instrument_id": "913256135", "bid": "10.00", "ask": "10.02",
            "bid_size": "300", "ask_size": "200", "price": "10.01", "quote_time": QUOTE_MS,
            "last_trade_time": QUOTE_MS, "trade_status": "ATH", "list_status": "LISTED"}
QUOTE_KEYS = ("bid", "ask", "bid_size", "ask_size", "quote_time")


def expected_quote(row: Any, choices: Choices) -> tuple | None:
    if not isinstance(row, Mapping):
        raise Refuse
    symbol = text(get(row, "symbol"), choices)
    if any(missing(get(row, k), choices) for k in QUOTE_KEYS):
        return None  # no quote: the caller records NO_QUOTE
    millis = number(get(row, "quote_time"))
    if millis != millis.to_integral_value() or millis <= 0:
        raise Refuse
    bid, ask, bid_size, ask_size = (number(row[k]) for k in QUOTE_KEYS[:4])
    if bid <= 0 or ask <= 0:
        implausible(choices)
    return (symbol, bid, ask, bid_size, ask_size,
            datetime.fromtimestamp(int(millis) / 1000, tz=UTC))


def check_snapshot(body: Any) -> None:
    got, error = outcome(lambda: read_snapshot(body, RECEIPT))

    def oracle(choices: Choices) -> list[tuple]:
        if isinstance(body, list):
            rows = body
        elif isinstance(body, Mapping) and isinstance(body.get("data"), list):
            rows = body["data"]
        else:
            raise Refuse
        return [q for q in (expected_quote(r, choices) for r in rows) if q is not None]

    if error is None:
        assert all(q.receipt_time == RECEIPT for q in got)
        got = [(q.symbol, q.bid, q.ask, q.bid_size, q.ask_size, q.provider_time) for q in got]
    assert_one_reading(body, got, error, readings(oracle))


def test_snapshot_every_field_mutated():
    for row in field_mutations(SNAPSHOT, mutants=TIME_MUTANTS):
        check_snapshot([row, SNAPSHOT])
    check_snapshot([SNAPSHOT, SNAPSHOT])  # a duplicate row is returned twice, never merged


def test_snapshot_random_mutations():
    rng = random.Random(1111)
    bases = [[SNAPSHOT], [SNAPSHOT, {**SNAPSHOT, "symbol": "MSFT"}], {"data": [SNAPSHOT]}]
    for _ in range(1500):
        check_snapshot(random_mutation(rng.choice(bases), rng, TIME_MUTANTS))
    for body in (None, "", 0, {}, {"data": None}, [None], ["AAPL"], [[]]):
        check_snapshot(body)


def recorded_bars(name: str) -> Any:
    raw = json.loads((FIXTURES / "market_data" / name).read_text("utf-8"))
    return parse_json(json.dumps(raw["body"]))


BAR_FIXTURES = ("stocks_bars_list_aapl_m1.json",
                "stocks_bars_list_aapl_m1_end_time_all_sessions_trimmed.json",
                "stocks_bars_list_aapl_m1_window_pre_rth.json")
BAR_FIELDS = ("open", "high", "low", "close", "volume")


BAR_TIME = "%Y-%m-%dT%H:%M:%S.%f%z"


def recorded_starts() -> set[datetime]:
    starts = set()
    for name in BAR_FIXTURES:
        for series in recorded_bars(name)["result"]:
            for row in series["result"]:
                starts.add(datetime.strptime(row["time"], BAR_TIME))
    return starts


def check_bar_reader(body: Any, read: Callable[[Any], list], starts: set[datetime]) -> None:
    """A bar reader returns one bar per input row with the row's own values and a recorded
    start time (no mutant is a valid time), or a typed refusal. A minute the body repeats may
    instead come back once (a fix of tests/broker/test_bug_duplicate_bars.py)."""
    got, error = outcome(lambda: read(body))
    if error is not None:
        assert isinstance(error, MalformedResponse | DelayedData), (body, error)
        return
    rows = [r for s in body["result"] for r in s["result"]]
    if len(got) != len(rows):  # rows dropped: only a repeated minute, kept once
        minutes = [(str(s["symbol"]), datetime.strptime(r["time"].strip(), BAR_TIME))
                   for s in body["result"] for r in s["result"]]
        returned = [(b.bar if hasattr(b, "bar") else b) for b in got]
        kept = sorted((b.symbol, getattr(b, "start", None) or b.time) for b in returned)
        assert len(set(minutes)) < len(minutes) and kept == sorted(set(minutes)), body
    for item in got:
        bar = item.bar if hasattr(item, "bar") else item
        start = getattr(bar, "start", None) or bar.time
        assert start in starts and start.tzinfo is not None
        row_values = [tuple(number(r[k]) for k in BAR_FIELDS) for r in rows]
        assert (bar.open, bar.high, bar.low, bar.close, bar.volume) in row_values
    keys = [(b.bar if hasattr(b, "bar") else b) for b in got]
    times = [getattr(b, "start", None) or b.time for b in keys]
    symbols = [b.symbol for b in keys]
    named = [str(s["symbol"]) for s in body["result"]]
    if len(set(named)) != len(named):
        return  # one symbol in two series: tests/broker/test_bug_duplicate_bars.py
    for i in range(1, len(times)):  # oldest first within each symbol's series
        if symbols[i] == symbols[i - 1]:
            assert times[i - 1] <= times[i]


def test_bars_random_mutations():
    rng = random.Random(2468)
    starts = recorded_starts()
    readers: tuple[Callable[[Any], list], ...] = (
        read_bars, lambda b: read_history_bars(b, BarSpan.M1),
        lambda b: read_history_bars(b, BarSpan.D), read_daily_bars)
    bases = [recorded_bars(name) for name in BAR_FIXTURES]
    for _ in range(400):
        body = random_mutation(rng.choice(bases), rng, TIME_MUTANTS)
        for read in readers:
            check_bar_reader(body, read, starts)


def test_bars_every_bar_field_mutated():
    starts = recorded_starts()
    base = recorded_bars("stocks_bars_list_aapl_m1.json")
    series = base["result"][0]
    for row in field_mutations(series["result"][0], mutants=TIME_MUTANTS):
        body = {"result": [{**series, "result": [row, *series["result"][1:]]}]}
        check_bar_reader(body, read_bars, starts)
    for entry in field_mutations(series, mutants=TIME_MUTANTS):
        check_bar_reader({"result": [entry]}, read_bars, starts)
    for body in (None, [], {}, {"result": None}, {"result": {}}, {"result": [None]},
                 {"result": [[]]}):
        with pytest.raises(MalformedResponse):
            read_bars(body)


def test_bars_delay_is_never_read_as_real_time():
    series = recorded_bars("stocks_bars_list_aapl_m1.json")["result"][0]
    for delay in (1, 15, D("0.5"), "15", D("-1")):
        with pytest.raises(DelayedData):
            read_bars({"result": [{**series, "delay_minutes": delay}]})
    for delay in (MISSING, None, "", "abc", "NaN", D("Infinity"), 1.5, True):
        with pytest.raises(MalformedResponse):
            read_bars({"result": [with_field(series, "delay_minutes", delay)]})


def screener_row(index: int) -> dict[str, str]:
    keys = json.loads((FIXTURES / "market_data" / "screener_gainers_losers_keys.json")
                      .read_text("utf-8"))["observations"][0]["key_names"]
    row = {k: "1.5" for k in keys}
    row.update(symbol=f"S{index}", instrument_id=f"9{index}", exchange_code="NSQ", currency="USD")
    return row


def test_screener_random_mutations():
    rng = random.Random(97)
    base = [screener_row(i) for i in range(4)]
    identity = ("instrument_id", "symbol", "exchange_code", "currency")
    for _ in range(800):
        body = random_mutation(rng.choice([base, {"data": base}]), rng, MUTANTS)
        got, error = outcome(lambda b=body: read_screener(b, ScreenerKind.GAINERS))
        rows = body if isinstance(body, list) else body.get("data")
        if not isinstance(rows, list) or any(
                not isinstance(r, Mapping) or any(absent(get(r, k)) for k in identity)
                for r in rows):
            assert isinstance(error, MalformedResponse), body
            continue
        if error is not None and any(blank(get(r, k)) for r in rows for k in identity):
            assert isinstance(error, MalformedResponse)  # a blank identity may be refused
            continue
        assert error is None, (body, error)
        assert [r.rank for r in got] == list(range(1, len(rows) + 1))
        for item, row in zip(got, rows, strict=True):
            assert (item.instrument_id, item.symbol, item.exchange_code, item.currency) == \
                tuple(str(row[k]) for k in identity)
            assert item.security_type is None  # filled from the profile, never guessed
            assert item.fields == {k: str(v) for k, v in row.items() if v is not None}


def test_profiles_random_mutations():
    rng = random.Random(55)
    base = load("market_data/instruments_stocks_profiles_list_mixed_types.json")
    for _ in range(800):
        body = random_mutation(base, rng, MUTANTS)
        got, error = outcome(lambda b=body: read_profiles(b))
        if error is not None:
            assert isinstance(error, MalformedResponse)
            continue
        rows = body if isinstance(body, list) else body["data"]
        by_id: dict[str, list[Mapping]] = {}
        for row in rows:
            by_id.setdefault(str(row["instrument_id"]), []).append(row)
        assert set(got) == set(by_id)
        for iid, profile in got.items():
            for row in by_id[iid]:  # every row of one id says the same thing
                assert profile.symbol == str(row["symbol"])
                raw = get(row, "sub_category")
                assert profile.security_type == (None if absent(raw) else str(raw)) or (
                    blank(raw) and profile.security_type is None)
