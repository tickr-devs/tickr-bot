"""Contract fixtures for client tests (the macOS app's Swift tests decode every one of them).

Files are named `<schema name>[.<variant>].json`. They are generated from the contract code;
regenerate with `TICKR_WRITE_FIXTURES=1 pytest tests/api/test_fixtures.py` and review the diff.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from tickr.advisor_files import OwnerVerdict
from tickr.advisor_files import Verdict as HoldoutVerdict
from tickr.api import contract
from tickr.api.advisor import NO_FOLDER as NO_ADVISOR_FOLDER
from tickr.api.contract import (
    AccountOrder,
    AccountPosition,
    AccountResponseV1,
    Agreement,
    BackupJobV1,
    BarsV1,
    BarV1,
    BotStartV1,
    BotState,
    ChartBarV1,
    ChartInterval,
    ChartRange,
    ChartV1,
    CheckV1,
    CleanCause,
    CleanCauseKind,
    CleanDay,
    CleanVerdict,
    CommandAcceptedV1,
    CommandKind,
    CommandV1,
    ConfigKeyV1,
    ConfigSectionV1,
    ConfigV1,
    DaySummary,
    DaysV1,
    DayTrade,
    DayV1,
    DecisionsV1,
    DecisionV1,
    EntrySend,
    ErrorV1,
    ExplainV1,
    Fence,
    FlowChipV1,
    FlowReconcileV1,
    FlowStage,
    FlowStageV1,
    FlowStopState,
    FlowStopV1,
    FlowV1,
    JobsV1,
    JournalRowV1,
    JournalV1,
    LabReplayLegV1,
    LabReplayState,
    LabReplaySummaryV1,
    LabReplayV1,
    LabVersionRole,
    LabVersionV1,
    LatestSizeV1,
    LaunchdJobV1,
    NightlyFetchJobV1,
    ObserverDecisionV1,
    ObserversV1,
    ObserverTradeV1,
    ObserverV1,
    PairedV1,
    PendingCommandsV1,
    PriceAfterV1,
    ReconcileJobV1,
    RiskSettings,
    SessionSettings,
    SessionTimesV1,
    SettingsV1,
    SizeBinding,
    SkippedSignalV1,
    SkippedV1,
    StatusResponseV1,
    StrategyId,
    TradeWhatIfSignalsV1,
    TradeWhatIfTradeV1,
    TradeWhatIfV1,
    WatchingRowV1,
    WatchingV1,
    WhatIfV1,
)
from tickr.api.history import NO_ENTRY, NOT_READ
from tickr.api.performance import performance
from tickr.data.base import ScreenerKind
from tickr.replay.engine import ExitReason
from tickr.research.records import Admission
from tickr.strategy.base import Verdict

from .samples import T0, account, entry, status
from .schema_check import errors

FIXTURES = Path(__file__).resolve().parent / "fixtures"
LIMIT_CAUSE = "POSITION_LIMIT_REACHED: 1 open position(s), limit 1"
# As admission records it: New York offsets, microseconds (risk/admission.py _check_cooldown).
COOLDOWN_CAUSE = ("SYMBOL_COOLDOWN: ZNTH closed at a loss at 2026-11-16T10:14:07.250000-05:00; "
                  "no entry before 2026-11-16T10:44:07.250000-05:00")
POLL = Decimal("3")
DAY = date(2026, 11, 16)
LAB_SUMMARY = LabReplaySummaryV1(
    DAY - timedelta(days=25), DAY - timedelta(days=2), 20, 1, 143, 37, Decimal("43.24"),
    Decimal("0.18"), Decimal("6.66"), Decimal("1.41"), Decimal("-0.21"), Decimal("0.57"),
    Decimal("95"), Decimal("4.20"), 22, Decimal("412.50"), Decimal("187.00"))
LAB_VERSIONS = (
    LabVersionV1("price-action-v1", "price-action-v1", "1", LabVersionRole.ACTIVE, None),
    LabVersionV1("price-action-v2", "price-action-v2", "2", LabVersionRole.OBSERVER, None),
    LabVersionV1("volume-shock-v1", "volume-shock-v1", "1", LabVersionRole.CANDIDATE, None),
    LabVersionV1("my-idea", None, None, LabVersionRole.CANDIDATE,
                 "my-idea: strategy.toml has no [strategy] name"))
LAB_DAYS = tuple(DAY - timedelta(days=d) for d in (3, 4, 5, 6, 7, 10, 11))


def _response(st, age, stale, cause=None) -> StatusResponseV1:
    return StatusResponseV1(served_at=T0 + timedelta(seconds=2), status=st, age_seconds=age,
                            stale=stale, unavailable_cause=cause,
                            summary=contract.summarize(st, age, stale, cause), poll_seconds=POLL)


def documents() -> dict[str, object]:
    minimal = status(entry_block=None, fences=(), position=None, trades_today=(),
                     recent_commands=(), run_id=None, result_today_gross_usd=Decimal("0"),
                     realized_loss_today_usd=None, open_reservations_usd=Decimal("0"),
                     daily_room_usd=None, positions=(), code_version=None)
    mismatch = account(
        positions=(AccountPosition("ABCD", Decimal("10"), Decimal("12.34")),
                   AccountPosition("EFGH", Decimal("5"), None)),
        orders=(AccountOrder("ap-0001-s", "ABCD", "SELL", "STOP_LOSS", "ACCEPTED",
                             Decimal("10"), None, Decimal("11.90"), True),
                AccountOrder("manual-1", "EFGH", "BUY", "LIMIT", "ACCEPTED", Decimal("5"),
                             Decimal("19.80"), None, False)),
        matches_records=False,
        # No balance read has succeeded yet: every balance field is null.
        equity_usd=None, buying_power_usd=None, balance_read_at=None, cash_usd=None,
        positions_value_usd=None, unrealized_pl_usd=None, day_pl_usd=None,
        overnight_buying_power_usd=None, maintenance_margin_usd=None, margin_calls=None,
        day_trades_left=None, account_label=None,
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
        # Settings > Start: launchd took the start; the operator stop was cleared first.
        "bot-start-v1": BotStartV1(T0, True),
        # An example only: real tokens never leave the phone's Keychain and the Mac's memory.
        "paired-v1": PairedV1(token="example-token-not-a-secret_0123456789-abcdef"),
        "command-accepted-v1.warning": CommandAcceptedV1(
            command=command, warning="the bot does not look alive; the command runs only if "
                                     "the bot reads it before it expires"),
        "command-accepted-v1.flatten-symbol": CommandAcceptedV1(
            command=CommandV1("8b1e4d2c-6a3f-4e9b-8c7d-2f1a0b9c8d7e", CommandKind.FLATTEN,
                              T0, "api", "ABCD"), warning=None),
        "pending-commands-v1": PendingCommandsV1(commands=(command,)),
        "pending-commands-v1.empty": PendingCommandsV1(commands=()),
        "error-v1": ErrorV1("forbidden_host", "Host 'evil.example:8766' is not allowed"),
        "days-v1": DaysV1("sandbox", (
            DaySummary(DAY, 2, 1, 1, Decimal("2.50"), True, CleanDay(
                CleanVerdict.UNKNOWN, "the day is not over", (), 41, T0 - timedelta(hours=6),
                T0 + timedelta(minutes=5), None)),
            DaySummary(DAY - timedelta(days=3), 0, 0, 0, Decimal("0"), False, CleanDay(
                CleanVerdict.CLEAN, None, (), 80, T0 - timedelta(days=3, hours=6),
                T0 - timedelta(days=3) + timedelta(hours=1), 9120)),
            DaySummary(DAY - timedelta(days=4), 1, 0, 1, Decimal("-1.60"), True, CleanDay(
                CleanVerdict.NOT_CLEAN, None, (
                    CleanCause(CleanCauseKind.RECONCILE_MISMATCH,
                               "foreign-order:manual-1: open order not in our records; "
                               "reconcile: foreign-order:manual-1: open order not in our records",
                               (8410, 8411)),
                    CleanCause(CleanCauseKind.SAFETY_EXIT, "exit stream-lost for trade t-0007",
                               (8502,))),
                82, T0 - timedelta(days=4, hours=6), T0 - timedelta(days=4) + timedelta(hours=1),
                8890)),
            DaySummary(DAY - timedelta(days=5), 0, 0, 0, Decimal("0"), True, CleanDay(
                CleanVerdict.UNKNOWN, "not recorded: no reconcile-checked row that date (the "
                "bot records them since [orders] reconcile_proof_seconds was added)", (), 0,
                None, None, 7001)))),
        "days-v1.empty": DaysV1("sandbox", ()),
        "day-v1": DayV1("sandbox", DAY, (
            DayTrade("t-0001", "WXYZ", Decimal("20"), Decimal("5.10"), Decimal("5.02"), T0,
                     T0 + timedelta(minutes=7), "stop", Decimal("5.02"), Decimal("50"),
                     Decimal("-1.60"), Decimal("1.250"), Decimal("-2.20"), Decimal("0.80"),
                     entry("ap-0002-e", T0), None, (
                         PriceAfterV1(1, T0 + timedelta(minutes=8), Decimal("5.00"),
                                      Decimal("-0.02"), None),
                         PriceAfterV1(5, T0 + timedelta(minutes=12), Decimal("5.06"),
                                      Decimal("0.04"), None),
                         PriceAfterV1(15, T0 + timedelta(minutes=22), None, None,
                                      "no bar recorded for the minute 2026-11-16T15:22:00.000Z"),
                         PriceAfterV1(60, T0 + timedelta(minutes=67), Decimal("5.21"),
                                      Decimal("0.19"), None)),
                     None, Decimal("0.24"), Decimal("0.0960"), None,
                     StrategyId("price-action-v1", "1"),
                     "6c4eec8b0f3a91d2e5b7c6a8d9e0f1a2b3c4d5e6"),
            DayTrade("t-0002", "ABCD", Decimal("10"), Decimal("12.34"), Decimal("12.75"),
                     T0 + timedelta(minutes=20), T0 + timedelta(minutes=48), "strategy_exit",
                     None, Decimal("50"), Decimal("4.10"), None, None, None,
                     EntrySend(None, None, None, None, None, None, None),
                     "journal unreadable: journal row 812 (order-observed ap-0003-e): "
                     "KeyError: 'filled_quantity'", (
                         PriceAfterV1(1, T0 + timedelta(minutes=49), Decimal("12.70"),
                                      Decimal("-0.05"), None),
                         PriceAfterV1(5, T0 + timedelta(minutes=53), None, None,
                                      "the bars end at 2026-11-16T15:49:00.000Z"),
                         PriceAfterV1(15, T0 + timedelta(minutes=63), None, None,
                                      "the bars end at 2026-11-16T15:49:00.000Z"),
                         PriceAfterV1(60, T0 + timedelta(minutes=108), None, None,
                                      "the bars end at 2026-11-16T15:49:00.000Z")),
                     None, None, None,
                     "the bars end at 2026-11-16T15:49:00.000Z, before 2026-11-16T16:48:00.000Z")),
            Decimal("2.50"), ("ABCD", "WXYZ")),
        # Settings > Configuration: the file as on disk, loads, same as the running processes.
        "config-v1": ConfigV1("config/bot.toml", "9" * 64, False, True, None, (
            ConfigSectionV1("run", (
                ConfigKeyV1("environment", '"sandbox"', "Active environment; must be a table "
                            "under [environments]; read by config.load and the trading gate."),
                ConfigKeyV1("maintain_keep_sessions", "20", "Closed sessions whose capture and "
                            "decision folders `tickr maintain` leaves uncompressed; read by "
                            "maintain."))),
            ConfigSectionV1("environments.sandbox", (
                ConfigKeyV1("app_key_variable", '"WEBULL_SANDBOX_APP_KEY"', "Name of the "
                            "variable holding the app key (never the value)."),
                ConfigKeyV1("route_limits", "{}", "Route-specific limits; empty = every route "
                            "uses requests_per_route_per_minute."))))),
        # An edit that no longer loads: shown with its error, differs from what runs.
        "config-v1.invalid": ConfigV1("config/bot.toml", "a" * 64, True, False,
                                      "[run] maintain_keep_sessions must be at least 1 (today "
                                      "is always kept)", (
            ConfigSectionV1("run", (
                ConfigKeyV1("maintain_keep_sessions", "0", "Closed sessions kept "
                            "uncompressed."),)),)),
        "settings-v1": SettingsV1(
            "sandbox", "price-action-v1", ("price-action-v1-lookback8",),
            RiskSettings(Decimal("50"), Decimal("2500"), Decimal("300"), 1, Decimal("5"),
                         Decimal("10")),
            SessionSettings("America/New_York", 15, 5, False), Decimal("15"), POLL,
            Decimal("30"), Decimal("15")),
        # 10:00 New York on a Monday: the bot runs, its last broker read was 4 s ago.
        "jobs-v1": JobsV1(
            T0, BotState.RUNNING, False, T0.replace(hour=13, minute=12, second=4),
            ReconcileJobV1(Decimal("10"), T0 - timedelta(seconds=4),
                           T0 + timedelta(seconds=6)),
            BackupJobV1(T0.replace(hour=13, minute=12, second=9), None, None, None),
            _LAUNCHD, SessionTimesV1(
                DAY, datetime(2026, 11, 16, 14, 30, tzinfo=UTC),
                datetime(2026, 11, 16, 20, 45, tzinfo=UTC),
                datetime(2026, 11, 16, 20, 55, tzinfo=UTC),
                datetime(2026, 11, 16, 21, 0, tzinfo=UTC),
                datetime(2026, 11, 16, 9, 0, tzinfo=UTC),
                datetime(2026, 11, 17, 1, 0, tzinfo=UTC)), None,
            # Last night added Friday 2026-11-13 to the Lab's data.
            NightlyFetchJobV1(datetime(2026, 11, 14, 1, 42, 7, tzinfo=UTC), DAY - timedelta(days=3),
                              1, None)),
        # A Saturday with the bot stopped: no session, the last backup failed after Friday's.
        "jobs-v1.weekend": JobsV1(
            T0 + timedelta(days=5), BotState.STOPPED, True, None,
            ReconcileJobV1(Decimal("10"), T0 + timedelta(days=4, hours=6), None),
            BackupJobV1(T0 + timedelta(days=4), T0 + timedelta(days=4, hours=7),
                        "OperationalError", None),
            _LAUNCHD, None, "2026-11-21 is a weekend",
            # Friday night stopped before the 429 window cleared: the data still ends Thursday.
            NightlyFetchJobV1(T0 + timedelta(days=4, hours=21), None, 0,
                              "stopped at 2026-11-23T12:30:00+00:00, before the next session "
                              "([backtest] nightly_stop_minutes_before_open); current is "
                              "unchanged")),
        # The Overview's journal card: the latest rows, oldest first; "line #731".
        "journal-v1": JournalV1(731, (
            JournalRowV1(728, T0 - timedelta(seconds=9), "order-intent", "ABCD"),
            JournalRowV1(729, T0 - timedelta(seconds=8), "trade-opened", "ABCD"),
            JournalRowV1(730, T0 - timedelta(seconds=4), "reconcile-checked", None),
            JournalRowV1(731, T0, "stop-from-fill", "ABCD")), None),
        "journal-v1.empty": JournalV1(None, (), "the bot has not created its database yet"),
        # Saturday: the last 20 sessions replayed for the active version, then compared with
        # the price-action-v2 candidate over the same window; both SIMULATED.
        "lab-replay-v1": LabReplayV1(
            LabReplayState.DONE, "3f0c9a1e5b7d4c2a8e6f1b0d9c7a5e3f", "price-action-v1", "1", 20,
            T0 + timedelta(days=5), T0 + timedelta(days=5, minutes=9, seconds=2), None,
            LAB_SUMMARY, folder="price-action-v1",
            compare=LabReplayLegV1("price-action-v2", "price-action-v2", "2",
                                   LabReplayState.DONE, None, LabReplaySummaryV1(
                                       DAY - timedelta(days=25), DAY - timedelta(days=2), 20, 1,
                                       143, 29, Decimal("48.28"), Decimal("0.27"),
                                       Decimal("7.83"), Decimal("1.66"), Decimal("-0.12"),
                                       Decimal("0.64"), Decimal("95"), Decimal("3.10"), 19,
                                       Decimal("488.00"), Decimal("142.50"))),
            versions=LAB_VERSIONS, days=LAB_DAYS),
        "lab-replay-v1.idle": LabReplayV1(LabReplayState.IDLE, None, None, None, 20, None,
                                          None, None, None, versions=LAB_VERSIONS, days=LAB_DAYS),
        # One day replayed: the first version runs, the second waits for it.
        "lab-replay-v1.running": LabReplayV1(
            LabReplayState.RUNNING, "3f0c9a1e5b7d4c2a8e6f1b0d9c7a5e3f", "price-action-v1",
            "1", 1, T0 + timedelta(days=5), None, None, None, folder="price-action-v1",
            day=DAY - timedelta(days=3),
            compare=LabReplayLegV1("volume-shock-v1", "volume-shock-v1", "1",
                                   LabReplayState.RUNNING, None, None),
            versions=LAB_VERSIONS, days=LAB_DAYS),
        "lab-replay-v1.failed": LabReplayV1(
            LabReplayState.FAILED, "3f0c9a1e5b7d4c2a8e6f1b0d9c7a5e3f", "price-action-v1", "1",
            20, T0 + timedelta(days=5), T0 + timedelta(days=5, seconds=2),
            "backtest/minutes is missing: run fetch first", None, folder="price-action-v1",
            versions=LAB_VERSIONS, days=LAB_DAYS),
        # Trades "Another strategy…": price-action-v2 on ABCD's day, SIMULATED.
        "trade-whatif-v1": TradeWhatIfV1(
            DAY - timedelta(days=3), "ABCD", "price-action-v2", "price-action-v2", "2",
            TradeWhatIfSignalsV1(2, 1, 0, 0), (
                TradeWhatIfTradeV1(T0 - timedelta(days=3, minutes=18),
                                   T0 - timedelta(days=3, minutes=16), Decimal("12.3400"),
                                   Decimal("11.9000"), T0 - timedelta(days=3, minutes=-41),
                                   Decimal("12.9100"), ExitReason.STRATEGY_EXIT,
                                   Decimal("1.2955"), Decimal("10"), Decimal("5.7000")),
                TradeWhatIfTradeV1(T0 - timedelta(days=3, minutes=-72),
                                   T0 - timedelta(days=3, minutes=-74), Decimal("13.0200"),
                                   Decimal("12.7000"), T0 - timedelta(days=3, minutes=-80),
                                   Decimal("12.6900"), ExitReason.STOP, Decimal("-1.0313"),
                                   None, None)),
            Decimal("0.2642"), None, None),
        "trade-whatif-v1.failed": TradeWhatIfV1(
            DAY - timedelta(days=3), "ABCD", "volume-shock-v1", "volume-shock-v1", "1", None,
            (), None, None, "no cached prior sessions for ABCD on 2026-11-13"),
        "day-v1.empty": DayV1("sandbox", DAY - timedelta(days=3), (), Decimal("0"), ()),
        "bars-v1": BarsV1("sandbox", "ABCD", DAY, tuple(
            BarV1(T0 + timedelta(minutes=i), Decimal(o), Decimal(h), Decimal(lo), Decimal(c),
                  Decimal(v))
            for i, (o, h, lo, c, v) in enumerate((
                ("12.30", "12.38", "12.28", "12.36", "18450"),
                ("12.36", "12.41", "12.33", "12.34", "9120"),
                ("12.34", "12.52", "12.34", "12.50", "40211"))))),
        # 5m candles around the regular open of 2026-11-16 (09:20 and 09:25 New York are
        # pre-market, 09:30 is the first regular candle).
        "chart-v1": ChartV1("ABCD", ChartInterval.M5, ChartRange.D1, "webull",
                            datetime(2026, 11, 16, 14, 41, 2, 250000, tzinfo=UTC), None, tuple(
            ChartBarV1(datetime(2026, 11, 16, 14, 20, tzinfo=UTC) + timedelta(minutes=5 * i),
                       Decimal(o), Decimal(h), Decimal(lo), Decimal(c), Decimal(v), i < 2)
            for i, (o, h, lo, c, v) in enumerate((
                ("12.10", "12.18", "12.05", "12.16", "3120"),
                ("12.16", "12.31", "12.15", "12.30", "5410"),
                ("12.30", "12.52", "12.28", "12.47", "182300"))))),
        "chart-v1.empty": ChartV1("ABCD", ChartInterval.D1, ChartRange.Y1, "webull",
                                  datetime(2026, 11, 16, 14, 41, 2, 250000, tzinfo=UTC),
                                  "no 1d bars from webull for ABCD in range 1y", ()),
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
        "observers-v1": ObserversV1(DAY, (
            ObserverV1("price-action-v1", "2", (
                ObserverDecisionV1(T0.replace(microsecond=0), "ABCD", Verdict.WATCH,
                                   ("ask_above_trigger",)),
                ObserverDecisionV1(T0.replace(microsecond=0) + timedelta(minutes=1), "ABCD",
                                   Verdict.ENTER, ()),
                ObserverDecisionV1(T0.replace(microsecond=0) + timedelta(minutes=4), "ABCD",
                                   Verdict.ENTER, ())), Decimal("-12.4000"), None, (
                ObserverTradeV1("ABCD", T0.replace(microsecond=0) + timedelta(minutes=1),
                                T0.replace(microsecond=0) + timedelta(minutes=2),
                                Decimal("10.0700"), T0.replace(microsecond=0) +
                                timedelta(minutes=9), Decimal("9.9460"), Decimal(100),
                                ExitReason.STOP, Decimal("-12.4000"), Decimal("-1.0333"), None),
                ObserverTradeV1("ABCD", T0.replace(microsecond=0) + timedelta(minutes=4), None,
                                None, None, None, None, None, None, None,
                                "skipped_overlap: an earlier simulated trade of ABCD was still "
                                "open")), None, False),
            ObserverV1("price-action-v2", "1", (), None, "no decisions recorded that day", None,
                       "no decisions recorded that day", False),
            ObserverV1("price-action-v2", "2", (
                ObserverDecisionV1(T0.replace(microsecond=0) + timedelta(minutes=2), "WXYZ",
                                   Verdict.ENTER, ()),), None,
                "no captured bars for 2026-11-16", None, "no captured bars for 2026-11-16",
                False))),
        "observers-v1.empty": ObserversV1(DAY - timedelta(days=3), ()),
        "decisions-v1": DecisionsV1("ABCD", DAY, (
            DecisionV1(T0.replace(microsecond=0), Verdict.WATCH,
                       StrategyId("price-action-v1", "1"), False, (
                           CheckV1("ask_above_trigger", Decimal("12.33"), ">",
                                   Decimal("12.35"), False, ""),), None, None,
                       Admission.NOT_APPLICABLE, None, None),
            DecisionV1(T0.replace(microsecond=0) + timedelta(minutes=1), Verdict.ENTER,
                       StrategyId("price-action-v1", "2"), True, (
                           CheckV1("ask_above_trigger", Decimal("12.37"), ">",
                                   Decimal("12.35"), True, ""),),
                       Decimal("12.40"), Decimal("12.10"), Admission.NOT_EVALUATED, None,
                       None),
            DecisionV1(T0.replace(microsecond=0) + timedelta(minutes=2), Verdict.ENTER,
                       StrategyId("price-action-v1", "1"), False, (
                           CheckV1("ask_above_trigger", Decimal("12.41"), ">",
                                   Decimal("12.35"), True, ""),),
                       Decimal("12.45"), Decimal("12.15"), Admission.REFUSED,
                       LIMIT_CAUSE, contract.refusal_of(LIMIT_CAUSE)))),
        "skipped-v1": SkippedV1(DAY, (
            SkippedSignalV1(T0.replace(microsecond=0) + timedelta(minutes=2), "ABCD",
                            StrategyId("price-action-v1", "1"), Admission.REFUSED,
                            LIMIT_CAUSE, contract.refusal_of(LIMIT_CAUSE), Decimal("12.45"),
                            Decimal("12.15"), (
                                CheckV1("relative_volume", Decimal("2.4"), ">=",
                                        Decimal("1.5"), True, ""),
                                CheckV1("ask_above_trigger", Decimal("12.41"), ">",
                                        Decimal("12.35"), True, "")),
                            _after(T0 + timedelta(minutes=2), "12.38", "12.44", "12.52", "12.20"),
                            None, WhatIfV1(Decimal("-6.20"), Decimal("-1.0000"),
                                           ExitReason.STOP, None)),
            SkippedSignalV1(T0.replace(microsecond=0) + timedelta(minutes=35), "ZNTH",
                            StrategyId("price-action-v1", "1"), Admission.REFUSED,
                            COOLDOWN_CAUSE, contract.refusal_of(COOLDOWN_CAUSE),
                            Decimal("0.9876"), Decimal("0.9512"), (
                                CheckV1("ask_above_trigger", Decimal("0.9881"), ">",
                                        Decimal("0.9850"), True, ""),),
                            _after(T0 + timedelta(minutes=35), "0.9870", "0.9910", "1.0040",
                                   "0.9950"),
                            None, WhatIfV1(Decimal("17.05"), Decimal("0.4620"),
                                           ExitReason.FLATTEN, None)),
            SkippedSignalV1(T0.replace(microsecond=0) + timedelta(minutes=47), "WXYZ",
                            StrategyId("price-action-v1", "1"), Admission.NOT_EVALUATED, None,
                            None, None, None, (
                                CheckV1("ask_above_trigger", Decimal("5.31"), ">",
                                        Decimal("5.25"), True, ""),),
                            (), "no captured bars for 2026-11-16",
                            WhatIfV1(None, None, None, "no captured bars for 2026-11-16")))),
        "skipped-v1.empty": SkippedV1(DAY - timedelta(days=3), ()),
        "flow-v1": FlowV1("sandbox", DAY, (
            FlowChipV1("ABCD", (ScreenerKind.GAINERS, ScreenerKind.TOP_ACTIVES),
                       Decimal("12.40"), None, None),
            FlowChipV1("SPY", (ScreenerKind.TOP_ACTIVES,), Decimal("0.31"), None,
                       "SECURITY_TYPE_NOT_ALLOWED: ETF"),
            FlowChipV1("WXYZ", (ScreenerKind.GAINERS,), None, "the captured row has no "
                       "pre_close", None),
            FlowChipV1("EFGH", (ScreenerKind.LOSERS,), Decimal("-8.05"), None, None)), (
            FlowStageV1(FlowStage.SCREENERS, 4, ("ABCD", "EFGH", "SPY", "WXYZ")),
            FlowStageV1(FlowStage.UNIVERSE, 3, ("ABCD", "EFGH", "WXYZ")),
            FlowStageV1(FlowStage.STRATEGY, 3, ("ABCD", "EFGH", "WXYZ")),
            FlowStageV1(FlowStage.RISK, 3, ("ABCD", "EFGH", "WXYZ")),
            FlowStageV1(FlowStage.ORDER, 3, ("ABCD", "EFGH", "WXYZ")),
            FlowStageV1(FlowStage.STOP, 1, ("WXYZ",))), (
            FlowStopV1("t-0001", "WXYZ", FlowStopState.VERIFIED),
            FlowStopV1("t-0002", "EFGH", FlowStopState.NEVER_VERIFIED),
            FlowStopV1("t-0003", "ABCD", FlowStopState.PENDING)),
            FlowReconcileV1(Agreement.MATCH, None, T0, None, (
                Fence("foreign-position:EFGH", T0, "position not opened by this bot",
                      "foreign_position"),),
                None)),
        "flow-v1.empty": FlowV1("sandbox", DAY - timedelta(days=3), (), tuple(
            FlowStageV1(stage, 0, ()) for stage in FlowStage), (), FlowReconcileV1(
            None, "only the latest reconciliation is kept; it is not recorded per day", None,
            None, None, "only the latest status is kept; it is not recorded per day")),
        "watching-v1": WatchingV1(DAY, 3, (
            WatchingRowV1("ABCD", T0.replace(microsecond=0), StrategyId("price-action-v1", "1"),
                          Verdict.WATCH, CheckV1("ask_above_trigger", Decimal("12.33"), ">",
                                                 Decimal("12.35"), False, ""),
                          Decimal("0.9983"), None, None,
                          "not recorded: decision records hold no quote time"),
            WatchingRowV1("WXYZ", T0.replace(microsecond=0), StrategyId("price-action-v1", "1"),
                          Verdict.IGNORE, CheckV1("spread_ratio", Decimal("0.0031"), "<=",
                                                  Decimal("0.002"), False, ""),
                          Decimal("0.6451"), None, None,
                          "not recorded: decision records hold no quote time"),
            WatchingRowV1("EFGH", T0.replace(microsecond=0), StrategyId("price-action-v1", "1"),
                          Verdict.IGNORE, CheckV1("quote_valid", None, "is", None, False,
                                                  "own symbol, 0 < bid <= ask"),
                          None, "quote_valid: a yes/no check has no measured value", None,
                          "not recorded: decision records hold no quote time")),
            LatestSizeV1("QRST", T0.replace(microsecond=0) - timedelta(minutes=3),
                         StrategyId("price-action-v1", "1"), Verdict.ENTER, Decimal("200"),
                         None, contract.BINDING_NOT_RECORDED)),
        # Late in a losing day: the latest sized decision was cut to the daily room.
        "watching-v1.daily-room": WatchingV1(DAY, 0, (), LatestSizeV1(
            "QRST", T0.replace(microsecond=0), StrategyId("price-action-v1", "1"),
            Verdict.WATCH, Decimal("37"), SizeBinding.DAILY_ROOM, None)),
        "watching-v1.empty": WatchingV1(DAY - timedelta(days=3), 0, (), None),
        "performance-v1": performance("sandbox", DAY - timedelta(days=6), DAY, (
            _closed("t-0001", -3, "100", "50"), _closed("t-0002", -3, "-50", "50"),
            _closed("t-0003", 0, "0", "50"), _closed("t-0004", 0, "-25", "50"),
            _closed("t-0005", 0, "37.50", "25"), _closed("t-0006", 0, "4.10", "0")), PLACES),
        "performance-v1.empty": performance("sandbox", DAY - timedelta(days=6), DAY, (), PLACES),
        "advisor-runs-v1": _advisor_runs(),
        "advisor-runs-v1.empty": contract.AdvisorRunsV1((), (), 0, NO_ADVISOR_FOLDER),
    }


def _stats(trades: int, win: str, mean: str, total: str, pf: str | None, low: str | None,
           high: str | None) -> contract.AdvisorStatsV1:
    return contract.AdvisorStatsV1(
        trades, Decimal(win), Decimal(mean), Decimal("0.0"), Decimal(total),
        None if pf is None else Decimal(pf), None if low is None else Decimal(low),
        None if high is None else Decimal(high))


def _advisor_runs() -> contract.AdvisorRunsV1:
    """Lab: a backtest proposal judged on the hold-out and awaiting review, an accepted idea,
    a refused answer, and a folder from before run.json."""
    base = "price-action-v1 v1"
    judged = contract.AdvisorRunV1(
        "20261115T150000Z-price-action-v1", T0 - timedelta(days=1),
        contract.AdvisorMode.BACKTEST, contract.AdvisorRunStatus.PROPOSED, base,
        "price-action-v1 v2", "claude-opus-5-5",
        "SIMULATED backtest out/backtest-v1, fitting period 2026-08-03 .. 2026-10-02; hold-out "
        "from 2026-10-05 withheld",
        "backtest sessions 2026-08-03 .. 2026-10-02 (before the hold-out from 2026-10-05)",
        "the backtest hold-out from 2026-10-05 (python -m tickr.advisor.judge) or observer "
        "days after the proposal",
        "Stops are too tight: most stopped trades reach +1R later the same session.",
        (contract.AdvisorChangeV1("exit", "stop_buffer_atr", "0.25", "0.5",
                                  "half of the stops are inside one minute's noise",
                                  "62% of stop exits later reached +1R"),
         contract.AdvisorChangeV1("entry", "require_vwap_above", "false", "true",
                                  "entries below VWAP lose", "mean R -0.31 below VWAP, 0.08 "
                                  "above")),
        "fewer stop exits; fewer trades", ("wider stops make 1 R bigger: smaller size",),
        "", "", None,
        (contract.AdvisorJudgeV1(
            "price-action-v1 v2", base, date(2026, 10, 5), HoldoutVerdict.NO_EVIDENCE,
            HoldoutVerdict.NEGATIVE_EDGE,
            _stats(41, "46.3", "0.04", "1.64", "1.08", "-0.18", "0.27"),
            _stats(57, "38.6", "-0.21", "-11.97", "0.71", "-0.39", "-0.03"),
            _stats(188, "51.1", "0.22", "41.36", "1.52", "0.09", "0.35")),),
        None)
    idea = contract.AdvisorRunV1(
        "20261114T150000Z-price-action-v1", T0 - timedelta(days=2),
        contract.AdvisorMode.BACKTEST, contract.AdvisorRunStatus.IDEA, base, None,
        "claude-opus-5-5", judged.evidence, judged.tuned_on, judged.judge_on,
        "The entry idea itself fails; a pullback entry is worth a module.", (), "", (),
        "no value change rescues the breakout entry", "", contract.AdvisorIdeaV1(
            "pullback-v1", "gainers that hold the VWAP continue",
            "buy the first higher low above VWAP after 10:00",
            "stop under the higher low; flatten at the close", "price above 5 USD",
            "stops after +0.6R, then 2.4R after the stop",
            "backtest on sessions after the window"),
        (), contract.AdvisorDecisionV1(OwnerVerdict.ACCEPTED, T0 - timedelta(days=1, hours=3)))
    refused = contract.AdvisorRunV1(
        "20261114T120000Z-price-action-v1", T0 - timedelta(days=2, hours=3),
        contract.AdvisorMode.LIVE, contract.AdvisorRunStatus.REFUSED, base, None,
        "claude-opus-5-5", "5 recorded days (2026-11-09 .. 2026-11-13)",
        "2026-11-09, 2026-11-10, 2026-11-11, 2026-11-12, 2026-11-13",
        "days after 2026-11-13 (observers) or on replay days outside that list", "", (), "", (),
        "", "4 changes proposed, at most 3 allowed", None, (), None)
    return contract.AdvisorRunsV1(
        (judged, idea, refused),
        (contract.AdvisorUnreadableV1(
            "20261113T180000Z-price-action-v1",
            "no run.json (a run written before the apps read runs, or an interrupted one)"),),
        1, None)


PLACES = 4


def _after(at: datetime, reference: str, *prices: str) -> tuple[PriceAfterV1, ...]:
    """Points at 1, 5 and 15 minutes after `at`; the 60-minute one is after the bars' end."""
    points = tuple(PriceAfterV1(m, at.replace(microsecond=0) + timedelta(minutes=m),
                                Decimal(p), Decimal(p) - Decimal(reference), None)
                   for m, p in zip((1, 5, 15), prices, strict=True))
    end = at.replace(microsecond=0) + timedelta(minutes=40)
    return (*points, PriceAfterV1(60, at.replace(microsecond=0) + timedelta(minutes=60), None,
                                  None, f"the bars end at {contract.format_time(end)}"))


def _closed(trade_id: str, day_offset: int, result: str, planned_loss: str) -> DayTrade:
    """A closed trade of 10 shares at 10.00 with the given gross result and 1 R."""
    closed_at = T0 + timedelta(days=day_offset, minutes=int(trade_id[-1]))
    exit_price = Decimal("10") + Decimal(result) / 10
    return DayTrade(trade_id, "ABCD", Decimal("10"), Decimal("10.00"), exit_price,
                    closed_at - timedelta(minutes=1), closed_at, "strategy_exit", None,
                    Decimal(planned_loss), Decimal(result), None, None, None, NO_ENTRY,
                    NOT_READ, (), NOT_READ, None, None, NOT_READ)


_LAUNCHD = (LaunchdJobV1("preflight", "08:30", (1, 2, 3, 4, 5)),
            LaunchdJobV1("bot", "09:00", (1, 2, 3, 4, 5)),
            LaunchdJobV1("watchdog", "09:05", (1, 2, 3, 4, 5)),
            LaunchdJobV1("nightly-fetch", "19:00", (1, 2, 3, 4, 5)))


def _text(document: object) -> str:
    return json.dumps(contract.to_jsonable(document), indent=2, ensure_ascii=False) + "\n"


def test_fixtures_are_current():
    docs = documents()
    if os.environ.get("TICKR_WRITE_FIXTURES") == "1":
        for name, document in docs.items():
            (FIXTURES / f"{name}.json").write_text(_text(document), "utf-8")
    on_disk = {p.stem for p in FIXTURES.glob("*.json")}
    assert on_disk == set(docs), "fixtures on disk differ from documents()"
    for name, document in docs.items():
        assert (FIXTURES / f"{name}.json").read_text("utf-8") == _text(document), (
            f"{name}: regenerate with TICKR_WRITE_FIXTURES=1 and review the diff")


@pytest.mark.parametrize("path", sorted(FIXTURES.glob("*.json")), ids=lambda p: p.name)
def test_fixture_matches_schema_and_parses(path):
    schema_name = path.name.split(".", 1)[0]
    cls = contract.SCHEMA_TYPES[schema_name]
    raw = json.loads(path.read_text("utf-8"))
    assert errors(contract.json_schema(cls), raw) == []
    assert contract.to_jsonable(contract.from_jsonable(cls, raw)) == raw
