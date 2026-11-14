"""Sample contract documents shared by the API tests."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from aperture.api.contract import (
    BotState,
    CommandKind,
    CommandOutcome,
    CommandRecord,
    EntryBlock,
    Fence,
    Freshness,
    Gate,
    Position,
    StatusV1,
    Stop,
    StopState,
    StrategyId,
    StreamHealth,
    Trade,
)

T0 = datetime(2026, 11, 16, 15, 0, 0, 123000, tzinfo=UTC)


def status(written_at: datetime = T0, **overrides) -> StatusV1:
    values = dict(
        written_at=written_at,
        bot_state=BotState.RUNNING,
        run_id="run-1",
        environment="sandbox",
        trading_gate=Gate(True, "trading enabled by configuration"),
        strategy=StrategyId("price-action-v1", "1"),
        entry_stop=False,
        entry_block=EntryBlock("open_position_limit",
                               "One position is already open (limit 1)."),
        fences=(Fence("foreign_exposure", T0, "A position not opened by the bot exists."),),
        position=Position("ABCD", Decimal("10"), Decimal("12.34"), T0,
                          Stop(StopState.VERIFIED, Decimal("11.90"), "ap-0001-s")),
        trades_today=(Trade("WXYZ", Decimal("20"), Decimal("5.10"), Decimal("5.02"), T0, T0,
                            "stop", Decimal("-1.60")),),
        result_today_gross_usd=Decimal("-1.60"),
        freshness=Freshness(T0, None, StreamHealth.FRESH),
        recent_commands=(CommandRecord("0b6f7c1e-2f0a-4c55-9d1e-6a1d2f3e4b5c",
                                       CommandKind.ENTRY_STOP_ON, T0, T0,
                                       CommandOutcome.DONE, None),),
    )
    values.update(overrides)
    return StatusV1(**values)
