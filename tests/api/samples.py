"""Sample contract documents shared by the API tests."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from tickr.api.contract import (
    AccountOrder,
    AccountPosition,
    AccountV1,
    BotState,
    CommandKind,
    CommandOutcome,
    CommandRecord,
    EntryBlock,
    EntrySend,
    Fence,
    Freshness,
    Gate,
    Position,
    SendOutcome,
    StatusV1,
    Stop,
    StopState,
    StrategyId,
    StreamHealth,
    Trade,
)

T0 = datetime(2026, 11, 16, 15, 0, 0, 123000, tzinfo=UTC)


def entry(cid: str, at: datetime) -> EntrySend:
    """A clean entry send: journaled, attempted once 2 ms later, filled 1.5 s later."""
    return EntrySend(cid, at, at + timedelta(milliseconds=2), SendOutcome.ACKNOWLEDGED,
                     SendOutcome.ACKNOWLEDGED, 1, at + timedelta(milliseconds=1502))


POSITION = Position("ABCD", Decimal("10"), Decimal("12.34"), T0,
                    Stop(StopState.VERIFIED, Decimal("11.90"), "ap-0001-s"),
                    entry("ap-0001-e", T0), None)


def status(written_at: datetime = T0, **overrides) -> StatusV1:
    values = dict(
        written_at=written_at,
        bot_state=BotState.RUNNING,
        run_id="run-1",
        environment="sandbox",
        trading_gate=Gate(True, "trading enabled by configuration"),
        strategy=StrategyId("price-action-v1", "1"),
        entry_stop=False,
        entry_block=EntryBlock("own_trade_open",
                               "The bot already has a trade open.", attention=False),
        fences=(Fence("foreign-position:EFGH", T0, "position not opened by this bot",
                      "foreign_position"),),
        position=POSITION,
        trades_today=(Trade("WXYZ", Decimal("20"), Decimal("5.10"), Decimal("5.02"), T0, T0,
                            "stop", Decimal("-1.60"), Decimal("50"), Decimal("1.250"),
                            # Send outcome unknown (timeout), then confirmed by a broker
                            # read and seen filled.
                            EntrySend("ap-0002-e", T0, T0 + timedelta(milliseconds=2),
                                      SendOutcome.UNKNOWN, SendOutcome.ACKNOWLEDGED, 1,
                                      T0 + timedelta(seconds=4)),
                            None, StrategyId("price-action-v1", "1")),),
        result_today_gross_usd=Decimal("-1.60"),
        freshness=Freshness(T0, None, StreamHealth.FRESH),
        recent_commands=(CommandRecord("0b6f7c1e-2f0a-4c55-9d1e-6a1d2f3e4b5c",
                                       CommandKind.ENTRY_STOP_ON, T0, T0,
                                       CommandOutcome.DONE, None),),
        realized_loss_today_usd=Decimal("1.60"),
        open_reservations_usd=Decimal("50"),
        daily_room_usd=Decimal("248.40"),
        positions=(POSITION,),
    )
    values.update(overrides)
    return StatusV1(**values)


def account(read_at: datetime = T0, **overrides) -> AccountV1:
    values = dict(
        read_at=read_at,
        equity_usd=None,
        buying_power_usd=None,
        positions=(AccountPosition("ABCD", Decimal("10"), Decimal("12.34")),),
        orders=(AccountOrder("ap-0001-s", "ABCD", "SELL", "STOP_LOSS", "ACCEPTED",
                             Decimal("10"), None, Decimal("11.90"), True),),
        matches_records=True,
        mismatch=None,
    )
    values.update(overrides)
    return AccountV1(**values)
