"""GET /performance: gross results in R and USD over a range of New York dates, read-only."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest

from aperture import config
from aperture.api import contract
from aperture.api.history import NO_ENTRY, NOT_READ
from aperture.api.performance import (
    NO_LOSSES,
    NO_TRADES,
    NO_WINS,
    NOT_POSITIVE_PLANNED_LOSS,
    performance,
)
from aperture.api.server import settings_from_config

from .schema_check import errors
from .test_history import OPEN, _run, _store, _trade
from .test_server import ROOT, api, doc, request, schema, settings  # noqa: F401 - pytest fixtures

DAY = date(2026, 11, 16)
FRIDAY = date(2026, 11, 13)
PLACES = 4


def _closed(n: int, day: date, result: str, planned_loss: str = "50") -> contract.DayTrade:
    """Closed at 10:00 New York + n minutes on `day`, 10 shares bought at 10.00."""
    closed_at = datetime(day.year, day.month, day.day, 15, n, tzinfo=UTC)
    return contract.DayTrade(
        f"t{n}", "ABCD", Decimal(10), Decimal("10.00"), Decimal(10) + Decimal(result) / 10,
        closed_at - timedelta(minutes=1), closed_at, "strategy_exit", None,
        Decimal(planned_loss), Decimal(result), None, None, None, NO_ENTRY, NOT_READ)


def _of(*trades: contract.DayTrade, first: date = FRIDAY, last: date = DAY):
    return performance("sandbox", first, last, trades, PLACES)


# ---------------------------------------------------------------- the numbers


def test_hand_built_set():
    # R: +2, -1 | 0, -0.5, +1.5 (planned loss 25), and one trade without 1 R.
    doc_ = _of(_closed(1, FRIDAY, "100"), _closed(2, FRIDAY, "-50"), _closed(3, DAY, "0"),
               _closed(4, DAY, "-25"), _closed(5, DAY, "37.50", planned_loss="25"),
               _closed(6, DAY, "4.10", planned_loss="0"))
    assert (doc_.trades, doc_.wins, doc_.losses, doc_.scratches) == (5, 2, 2, 1)
    assert doc_.exclusions == (contract.ExclusionV1(NOT_POSITIVE_PLANNED_LOSS, 1),)
    assert doc_.win_rate == Decimal("0.4000")
    assert doc_.expectancy_gross_r == Decimal("0.4000")  # 2 R / 5
    assert doc_.expectancy_gross_usd == Decimal("12.5000")  # 62.50 / 5
    assert doc_.average_win_gross_r == Decimal("1.7500")
    assert doc_.average_loss_gross_r == Decimal("-0.7500")
    assert doc_.profit_factor_gross == Decimal("1.8333")  # 137.50 / 75 in USD, not R
    # Cumulative R 2, 1, 1, 0.5, 2: peak 2, low 0.5. USD 100, 50, 50, 25, 62.5.
    assert doc_.max_drawdown_gross_r == Decimal("1.5000")
    assert doc_.max_drawdown_gross_usd == Decimal("75")
    assert (doc_.result_gross_r, doc_.result_gross_usd) == (Decimal("2.0000"), Decimal("62.50"))
    assert [(d.day, d.trades, d.result_gross_r, d.result_gross_usd, d.cumulative_gross_r)
            for d in doc_.days] == [
        (FRIDAY, 2, Decimal("1.0000"), Decimal("50"), Decimal("1.0000")),
        (DAY, 3, Decimal("1.0000"), Decimal("12.50"), Decimal("2.0000"))]
    for name in ("win_rate", "expectancy_gross_r", "expectancy_gross_usd",
                 "average_win_gross_r", "average_loss_gross_r", "profit_factor_gross"):
        assert getattr(doc_, f"{name}_cause") is None


def test_drawdown_starts_from_zero_before_the_first_trade():
    doc_ = _of(_closed(1, DAY, "-50"), _closed(2, DAY, "25"))
    assert doc_.max_drawdown_gross_r == Decimal("1.0000")
    assert doc_.max_drawdown_gross_usd == Decimal("50")


def test_negative_planned_loss_is_excluded_never_counted_as_zero():
    doc_ = _of(_closed(1, DAY, "-50", planned_loss="-50"))
    assert doc_.trades == 0 and doc_.result_gross_usd == 0
    assert doc_.exclusions == (contract.ExclusionV1(NOT_POSITIVE_PLANNED_LOSS, 1),)
    assert doc_.days == ()


def test_only_trades_closed_inside_the_range_count():
    before = _closed(1, FRIDAY - timedelta(days=1), "100")
    after = _closed(2, DAY + timedelta(days=1), "100")
    # 01:30 UTC on the 17th is still the 16th in New York.
    late = replace(_closed(3, DAY, "-50"), closed_at=datetime(2026, 11, 17, 1, 30, tzinfo=UTC))
    doc_ = _of(before, after, late)
    assert doc_.trades == 1 and doc_.result_gross_usd == Decimal("-50")
    assert [d.day for d in doc_.days] == [DAY]


def test_no_losses_leaves_profit_factor_and_average_loss_null_with_a_cause():
    doc_ = _of(_closed(1, DAY, "50"), _closed(2, DAY, "0"))
    assert doc_.profit_factor_gross is None and doc_.profit_factor_gross_cause == NO_LOSSES
    assert doc_.average_loss_gross_r is None and doc_.average_loss_gross_r_cause == NO_LOSSES
    assert doc_.max_drawdown_gross_r == 0


def test_no_wins_gives_profit_factor_zero_and_average_win_null():
    doc_ = _of(_closed(1, DAY, "-50"))
    assert doc_.profit_factor_gross == 0 and doc_.profit_factor_gross_cause is None
    assert doc_.average_win_gross_r is None and doc_.average_win_gross_r_cause == NO_WINS
    assert doc_.win_rate == 0


def test_quotients_round_only_at_the_end():
    # 1/3 R three times: the total is 1 R exactly, not 3 x 0.3333.
    doc_ = _of(*(_closed(n, DAY, "10", planned_loss="30") for n in range(3)))
    assert doc_.expectancy_gross_r == Decimal("0.3333")
    assert doc_.result_gross_r == Decimal("1.0000")
    assert doc_.days[0].cumulative_gross_r == Decimal("1.0000")


def test_empty_range_zeros_where_defined_nulls_with_causes_elsewhere():
    doc_ = _of()
    assert (doc_.trades, doc_.wins, doc_.losses, doc_.scratches) == (0, 0, 0, 0)
    assert doc_.exclusions == () and doc_.days == ()
    assert doc_.result_gross_usd == doc_.result_gross_r == doc_.max_drawdown_gross_r == 0
    for name in ("win_rate", "expectancy_gross_r", "expectancy_gross_usd",
                 "average_win_gross_r", "average_loss_gross_r", "profit_factor_gross"):
        assert getattr(doc_, name) is None
        assert getattr(doc_, f"{name}_cause") == NO_TRADES


# ---------------------------------------------------------------- GET /performance


def test_endpoint_reads_the_journal_of_this_environment(api, settings):  # noqa: F811
    server, _ = api
    store = _store(settings.history)
    run = _run(store)
    _trade(store, run, "win", OPEN + timedelta(minutes=30))  # +4.10 for 1 R = 50
    _trade(store, run, "loss", OPEN + timedelta(minutes=50), exit_="12.00")  # -3.40
    _trade(store, _run(store, "other"), "elsewhere", OPEN + timedelta(minutes=55))
    store.close()
    resp, payload = request(server, "GET", f"/performance?from={DAY}&to={DAY}")
    body = doc(payload)
    assert resp.status == 200, body
    assert errors(schema("performance-v1"), body) == []
    assert (body["trades"], body["wins"], body["losses"]) == (2, 1, 1)
    assert body["result_gross_usd"] == "0.70"
    assert body["result_gross_r"] == "0.0140"
    assert body["profit_factor_gross"] == "1.2059"  # 4.10 / 3.40
    assert body["max_drawdown_gross_usd"] == "3.40"
    assert [d["day"] for d in body["days"]] == [DAY.isoformat()]


def test_endpoint_empty_range(api):  # noqa: F811
    server, _ = api
    resp, payload = request(server, "GET", f"/performance?from={FRIDAY}&to={DAY}")
    body = doc(payload)
    assert resp.status == 200
    assert errors(schema("performance-v1"), body) == []
    assert body["trades"] == 0 and body["days"] == [] and body["win_rate"] is None
    assert body["win_rate_cause"] == NO_TRADES
    assert (body["from_day"], body["to_day"]) == (FRIDAY.isoformat(), DAY.isoformat())


def test_longest_range_is_accepted_one_more_day_is_refused(api, settings):  # noqa: F811
    server, _ = api
    limit = settings.performance_max_range_days
    first = DAY - timedelta(days=limit - 1)
    resp, _ = request(server, "GET", f"/performance?from={first}&to={DAY}")
    assert resp.status == 200
    resp, payload = request(server, "GET",
                            f"/performance?from={first - timedelta(days=1)}&to={DAY}")
    body = doc(payload)
    assert (resp.status, body["error"]) == (400, "bad_request")
    assert "performance_max_range_days" in body["cause"]


@pytest.mark.parametrize("query", [
    f"from={DAY}&to={FRIDAY}",  # from after to
    f"from={DAY}", f"to={DAY}", "", f"from={DAY}&to={DAY}&to={DAY}",
    f"from={DAY}&to=16.11.2026", f"from={DAY}&to=", f"from={DAY}&to={DAY}&symbol=ABCD"])
def test_bad_ranges_are_refused(api, query):  # noqa: F811
    server, _ = api
    resp, payload = request(server, "GET", f"/performance?{query}")
    assert (resp.status, doc(payload)["error"]) == (400, "bad_request")


def test_from_after_to_names_both_dates(api):  # noqa: F811
    server, _ = api
    _, payload = request(server, "GET", f"/performance?from={DAY}&to={FRIDAY}")
    assert doc(payload)["cause"] == f"from {DAY} is after to {FRIDAY}"


def test_post_is_not_allowed(api):  # noqa: F811
    server, _ = api
    resp, _ = request(server, "POST", "/performance", {})
    assert resp.status == 405


@pytest.mark.parametrize(("key", "value"), [("performance_max_range_days", 0),
                                            ("performance_decimal_places", -1)])
def test_settings_refuse_bad_performance_limits(tmp_path, key, value):
    cfg = config.load(ROOT / "config" / "bot.toml", root=tmp_path)
    with pytest.raises(config.ConfigError, match=key):
        settings_from_config(replace(cfg, api=replace(cfg.api, **{key: value})))
