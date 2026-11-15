"""Contract fixtures for client tests (the macOS app's Swift tests decode every one of them).

Files are named `<schema name>[.<variant>].json`. They are generated from the contract code;
regenerate with `APERTURE_WRITE_FIXTURES=1 pytest tests/api/test_fixtures.py` and review the diff.
"""

from __future__ import annotations

import json
import os
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from aperture.api import contract
from aperture.api.contract import (
    AccountOrder,
    AccountPosition,
    AccountResponseV1,
    BarsV1,
    BarV1,
    CheckV1,
    CommandAcceptedV1,
    CommandKind,
    CommandV1,
    DaySummary,
    DaysV1,
    DayTrade,
    DayV1,
    ErrorV1,
    ExplainV1,
    PendingCommandsV1,
    RiskSettings,
    SessionSettings,
    SettingsV1,
    StatusResponseV1,
)
from aperture.research.records import Admission
from aperture.strategy.base import Verdict

from .samples import T0, account, status
from .schema_check import errors

FIXTURES = Path(__file__).resolve().parent / "fixtures"
POLL = Decimal("3")
DAY = date(2026, 11, 16)


def _response(st, age, stale, cause=None) -> StatusResponseV1:
    return StatusResponseV1(served_at=T0 + timedelta(seconds=2), status=st, age_seconds=age,
                            stale=stale, unavailable_cause=cause,
                            summary=contract.summarize(st, age, stale, cause), poll_seconds=POLL)


def documents() -> dict[str, object]:
    minimal = status(entry_block=None, fences=(), position=None, trades_today=(),
                     recent_commands=(), run_id=None, result_today_gross_usd=Decimal("0"),
                     realized_loss_today_usd=None, open_reservations_usd=Decimal("0"),
                     daily_room_usd=None, positions=())
    mismatch = account(
        positions=(AccountPosition("ABCD", Decimal("10"), Decimal("12.34")),
                   AccountPosition("EFGH", Decimal("5"), None)),
        orders=(AccountOrder("ap-0001-s", "ABCD", "SELL", "STOP_LOSS", "ACCEPTED",
                             Decimal("10"), None, Decimal("11.90"), True),
                AccountOrder("manual-1", "EFGH", "BUY", "LIMIT", "ACCEPTED", Decimal("5"),
                             None, None, False)),
        matches_records=False,
        mismatch="foreign-position:EFGH: position not opened by this bot; "
                 "foreign-order:manual-1: open order not in our records")
    command = CommandV1("3f2c9a7e-5b1d-4c8e-9a0f-1e2d3c4b5a69", CommandKind.FLATTEN,
                        T0, "api")
    return {
        "status-v1.example": status(),
        "status-response-v1": _response(status(), Decimal("2.000"), False),
        "status-response-v1.minimal": _response(minimal, Decimal("0.250"), False),
        "status-response-v1.stale": _response(status(), Decimal("42.500"), True),
        "status-response-v1.unavailable": _response(
            None, None, False, "the bot has not written a status file"),
        "command-accepted-v1": CommandAcceptedV1(command=command, warning=None),
        "command-accepted-v1.warning": CommandAcceptedV1(
            command=command, warning="the bot does not look alive; the command runs only if "
                                     "the bot reads it before it expires"),
        "pending-commands-v1": PendingCommandsV1(commands=(command,)),
        "pending-commands-v1.empty": PendingCommandsV1(commands=()),
        "error-v1": ErrorV1("forbidden_host", "Host 'evil.example:8766' is not allowed"),
        "days-v1": DaysV1("sandbox", (
            DaySummary(DAY, 2, 1, 1, Decimal("2.50"), True),
            DaySummary(DAY - timedelta(days=3), 0, 0, 0, Decimal("0"), False))),
        "days-v1.empty": DaysV1("sandbox", ()),
        "day-v1": DayV1("sandbox", DAY, (
            DayTrade("t-0001", "WXYZ", Decimal("20"), Decimal("5.10"), Decimal("5.02"), T0,
                     T0 + timedelta(minutes=7), "stop", Decimal("5.02"), Decimal("50"),
                     Decimal("-1.60")),
            DayTrade("t-0002", "ABCD", Decimal("10"), Decimal("12.34"), Decimal("12.75"),
                     T0 + timedelta(minutes=20), T0 + timedelta(minutes=48), "strategy_exit",
                     None, Decimal("50"), Decimal("4.10"))),
            Decimal("2.50"), ("ABCD", "WXYZ")),
        "settings-v1": SettingsV1(
            "sandbox", "price-action-v1", ("price-action-v1-lookback8",),
            RiskSettings(Decimal("50"), Decimal("2500"), Decimal("300"), 1, Decimal("5"),
                         Decimal("10")),
            SessionSettings("America/New_York", 15, 5, False), Decimal("15"), POLL),
        "day-v1.empty": DayV1("sandbox", DAY - timedelta(days=3), (), Decimal("0"), ()),
        "bars-v1": BarsV1("sandbox", "ABCD", DAY, tuple(
            BarV1(T0 + timedelta(minutes=i), Decimal(o), Decimal(h), Decimal(lo), Decimal(c),
                  Decimal(v))
            for i, (o, h, lo, c, v) in enumerate((
                ("12.30", "12.38", "12.28", "12.36", "18450"),
                ("12.36", "12.41", "12.33", "12.34", "9120"),
                ("12.34", "12.52", "12.34", "12.50", "40211"))))),
        "account-v1": account(),
        "account-v1.mismatch": mismatch,
        "account-response-v1": AccountResponseV1(T0 + timedelta(seconds=4), account(),
                                                 Decimal("4.000"), False),
        "account-response-v1.stale": AccountResponseV1(T0 + timedelta(seconds=40), mismatch,
                                                       Decimal("40.000"), True),
        "explain-v1": ExplainV1(
            "sandbox", DAY, "ABCD", T0 + timedelta(minutes=1, seconds=2), T0.replace(
                microsecond=0), "price_action_v1", "1", Verdict.WATCH, "breakout",
            Admission.NOT_APPLICABLE, None, "spread_ratio", (
                CheckV1("bars_consistent", None, "is", None, True, "own symbol, increasing "
                        "start"),
                CheckV1("relative_volume", Decimal("2.4"), ">=", Decimal("1.5"), True, ""),
                CheckV1("spread_ratio", Decimal("0.0031"), "<=", Decimal("0.002"), False,
                        ""))),
    }


def _text(document: object) -> str:
    return json.dumps(contract.to_jsonable(document), indent=2, ensure_ascii=False) + "\n"


def test_fixtures_are_current():
    docs = documents()
    if os.environ.get("APERTURE_WRITE_FIXTURES") == "1":
        for name, document in docs.items():
            (FIXTURES / f"{name}.json").write_text(_text(document), "utf-8")
    on_disk = {p.stem for p in FIXTURES.glob("*.json")}
    assert on_disk == set(docs), "fixtures on disk differ from documents()"
    for name, document in docs.items():
        assert (FIXTURES / f"{name}.json").read_text("utf-8") == _text(document), (
            f"{name}: regenerate with APERTURE_WRITE_FIXTURES=1 and review the diff")


@pytest.mark.parametrize("path", sorted(FIXTURES.glob("*.json")), ids=lambda p: p.name)
def test_fixture_matches_schema_and_parses(path):
    schema_name = path.name.split(".", 1)[0]
    cls = contract.SCHEMA_TYPES[schema_name]
    raw = json.loads(path.read_text("utf-8"))
    assert errors(contract.json_schema(cls), raw) == []
    assert contract.to_jsonable(contract.from_jsonable(cls, raw)) == raw
