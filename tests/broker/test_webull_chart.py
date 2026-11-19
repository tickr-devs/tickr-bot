"""Chart bars through the Webull adapter: recorded responses, request shape, and the chart's own
request budget, which never touches the bot's."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from tickr import config
from tickr.api.chart import place
from tickr.broker import webull
from tickr.broker.base import RateLimited
from tickr.broker.budget import BudgetExhausted
from tickr.broker.webull import (
    AdapterConfigError,
    MalformedResponse,
    Route,
    WireResponse,
    budget_from_config,
    chart_budget_from_config,
    chart_market_data_from_config,
    market_data_from_config,
    read_history_bars,
)
from tickr.calendar import NEW_YORK, SessionCalendar, ny_date_of
from tickr.data.base import BarSpan, DelayedData

from .test_webull_market_data import Scripted, bar_row, ok, recorded, recorded_meta

ROOT = Path(__file__).resolve().parents[2]
BOT = ROOT / "config" / "bot.toml"
ALL_SESSIONS = "stocks_bars_list_aapl_m1_end_time_all_sessions_trimmed.json"
BARS_PATH = Route.BARS.path
END = datetime(2026, 11, 13, 21, 0, tzinfo=UTC)


def enabled(cfg: config.Config | None = None, **chart: Any) -> config.Config:
    cfg = cfg or config.load(BOT)
    return replace(cfg, chart=replace(cfg.chart, webull_enabled=True, **chart))


def chart_adapter(cfg: config.Config, *responses):
    transport = Scripted(*responses)
    budget = chart_budget_from_config(cfg, clock=lambda: 0.0, utc_now=lambda: END)
    return chart_market_data_from_config(cfg, transport, budget), transport, budget


def daily_body(symbol: str, times: list[str]) -> dict[str, Any]:
    rows = [{"time": t, "open": "10", "high": "11", "low": "9", "close": "10.5",
             "volume": "100"} for t in times]
    return {"result": [{"symbol": symbol, "instrument_id": "1", "delay_minutes": "0",
                        "result": rows}]}


def test_recorded_all_sessions_bars_keep_labels_and_match_the_calendar():
    """Probe M4's PRE/RTH/ATH response: every recorded label is what the session calendar says
    for that minute, so the calendar can decide `extended` and cross-check the provider."""
    rows = read_history_bars(recorded(ALL_SESSIONS), BarSpan.M1)
    assert len(rows) == 12
    assert [r.bar.start for r in rows] == sorted(r.bar.start for r in rows)  # oldest first
    assert {r.session for r in rows} == {"PRE", "RTH", "ATH"}
    calendar = SessionCalendar.load(ROOT / "config" / "calendar")
    for row in rows:
        placed = place(calendar.day(ny_date_of(row.bar.start)), row)
        assert placed.extended is (row.session != "RTH")


def test_m1_request_asks_all_sessions_and_d_request_does_not():
    cfg = enabled()
    end_ms = recorded_meta(ALL_SESSIONS)["request"]["end_time"]
    end = datetime.fromtimestamp(end_ms / 1000, tz=UTC)
    md, transport, _ = chart_adapter(cfg, ok(recorded(ALL_SESSIONS)),
                                     ok(daily_body("AAPL", ["2026-11-12T05:00:00.000+0000"])))
    minutes = md.history_bars("AAPL", BarSpan.M1, end, 1200)
    assert len(minutes) == 12 and all(r.bar.start < end for r in minutes)
    days = md.history_bars("AAPL", BarSpan.D, end, 300)
    assert [d.session for d in days] == [None]
    (m1_route, m1), (d_route, d) = transport.sent
    assert m1_route is d_route is Route.BARS
    assert m1 == {"symbols": ["AAPL"], "category": "US_STOCK", "timespan": "M1",
                  "count": "1200", "end_time": end_ms, "trading_sessions": "PRE,RTH,ATH"}
    assert d["timespan"] == "D" and d["count"] == "300" and "trading_sessions" not in d


def test_chart_reads_refuse_unknown_sessions_delay_and_other_symbols():
    body = recorded(ALL_SESSIONS)
    body["result"][0]["result"][0]["trading_session"] = "OVN"
    with pytest.raises(MalformedResponse, match="OVN"):
        read_history_bars(body, BarSpan.M1)
    with pytest.raises(DelayedData):
        read_history_bars({"result": [{"symbol": "A", "delay_minutes": "15", "result": []}]},
                          BarSpan.M1)
    md, _, _ = chart_adapter(enabled(), ok(daily_body("OTHER", ["2026-11-12T05:00:00.000+0000"])))
    with pytest.raises(MalformedResponse, match="OTHER"):
        md.history_bars("AAPL", BarSpan.D, END, 10)


def test_chart_budget_is_refused_when_spent_and_the_bot_budget_is_untouched():
    cfg = enabled()
    slice_ = cfg.chart.requests_per_minute
    bot_budget = budget_from_config(cfg, clock=lambda: 0.0, utc_now=lambda: END)
    bot_transport = Scripted(ok({"result": [{"symbol": "AAA", "delay_minutes": "0",
                                             "result": [bar_row(0)]}]}))
    bot_md = market_data_from_config(cfg, bot_transport, bot_budget)
    md, transport, chart_budget = chart_adapter(
        cfg, *[ok(daily_body("AAPL", [])) for _ in range(slice_)])
    for _ in range(slice_):
        md.history_bars("AAPL", BarSpan.D, END, 10)
    with pytest.raises(BudgetExhausted):  # refused locally: nothing sent, nothing queued
        md.history_bars("AAPL", BarSpan.D, END, 10)
    assert len(transport.sent) == slice_
    assert chart_budget.usage(BARS_PATH).remaining == 0
    # The bot's own budget never saw a chart request and still serves its full bars slice.
    usage = bot_budget.usage(BARS_PATH)
    assert usage.used == 0 and usage.limit == cfg.environment.requests_per_route_per_minute - slice_
    assert len(bot_md.bars(["AAA"], 1)) == 1
    assert bot_budget.usage(BARS_PATH).used == 1 and len(bot_transport.sent) == 1


def test_bot_bars_slice_is_unchanged_while_charts_are_off():
    loaded = config.load(BOT)
    cfg = replace(loaded, chart=replace(loaded.chart, webull_enabled=False))
    budget = budget_from_config(cfg)
    assert budget.limit_for(BARS_PATH).max_requests == cfg.environment.requests_per_route_per_minute
    with pytest.raises(AdapterConfigError, match="webull_enabled"):
        chart_market_data_from_config(cfg, Scripted())


def test_chart_client_builds_a_market_data_only_transport(monkeypatch):
    """The API's chart client is built with `market_data_only`, so it never reads the account id
    variable (tests/broker/test_webull_sdk.py): the launchd API job is given only the app key and
    secret (runtime/launchd.py api_credential_variables), never the account id."""
    built: list[dict[str, Any]] = []

    def fake_transport(settings, *args, **kwargs):
        built.append({"account_env": settings.account_id_env, **kwargs})
        return Scripted()

    monkeypatch.setattr(webull, "SdkTransport", fake_transport)
    cfg = enabled()
    chart_market_data_from_config(cfg)
    assert built == [{"account_env": cfg.environment.account_variable, "market_data_only": True}]


def test_chart_slice_is_carved_from_a_route_specific_bars_limit():
    """The slice's window and size are refused when the config loads (tests/test_config.py)."""
    cfg = enabled()
    env = cfg.environment
    per_minute = replace(cfg, environments={**cfg.environments, cfg.environment_name: replace(
        env, route_limits={**env.route_limits, BARS_PATH: (50, 60)})})
    budget = budget_from_config(enabled(per_minute), utc_now=lambda: END)
    assert budget.limit_for(BARS_PATH).max_requests == 50 - cfg.chart.requests_per_minute
    # Other routes keep their limits.
    assert budget.limit_for(Route.SNAPSHOT.path).max_requests == env.requests_per_route_per_minute


def test_http_429_pauses_only_the_chart_budget():
    cfg = enabled()
    bot_budget = budget_from_config(cfg, clock=lambda: 0.0)
    md, _, chart_budget = chart_adapter(cfg, WireResponse(429, {"error_code": "TOO_MANY"}))
    with pytest.raises(RateLimited):
        md.history_bars("AAPL", BarSpan.D, END, 10)
    assert chart_budget.usage(BARS_PATH).paused_for_seconds > 0
    assert bot_budget.usage(BARS_PATH).paused_for_seconds == 0


def test_naive_end_is_refused():
    md, transport, _ = chart_adapter(enabled())
    with pytest.raises(ValueError):
        md.history_bars("AAPL", BarSpan.M1, END.replace(tzinfo=None), 10)
    assert transport.sent == []


def test_only_the_api_chart_reads_chart_bars():
    """The trading loop, discovery and history never call history_bars: chart reads cannot
    take the bot's budget or its source. The owner's chart probes (`tickr.probes`) are the
    one other caller: a process of their own, with their own client and budget."""
    callers = sorted(str(p.relative_to(ROOT)) for p in (ROOT / "src").rglob("*.py")
                     if ".history_bars(" in p.read_text("utf-8"))
    assert callers == ["src/tickr/api/chart.py", "src/tickr/probes.py"]


# New York times around Friday 2026-11-13 (a full session: pre-market 04:00, extended close 20:00),
# the weekend after it and Monday 2026-11-16.
def ny(day: int, hour: int, minute: int = 0, second: int = 0) -> datetime:
    return datetime(2026, 11, day, hour, minute, second, tzinfo=NEW_YORK).astimezone(UTC)


@pytest.mark.parametrize("at, chart_quiet, bot_quiet", [
    (ny(13, 16), False, False),          # regular session
    (ny(13, 19, 59, 59), False, False),  # last second of after-hours
    (ny(13, 20), False, True),           # the session is over: the bot gives the slice up now,
    (ny(13, 20, 0, 59), False, True),    # the chart takes it a window later
    (ny(13, 20, 1), True, True),
    (ny(13, 23, 18), True, True),        # the owner's iPhone chart, 2026-11-19 23:18 ET
    (ny(14, 12), True, True),            # Saturday
    (ny(16, 3, 58, 59), True, True),     # Monday before the pre-market: the chart gives it back
    (ny(16, 3, 59, 1), False, True),     # a window before the session starts,
    (ny(16, 4), False, False),           # the bot takes it back when the session starts
])
def test_quiet_hours_split_the_bars_route_without_overlap(at, chart_quiet, bot_quiet):
    """While no session runs the chart's bars slice is [chart] quiet_requests_per_minute and the
    bot side's limit is that much lower; the chart's quiet span sits a whole window inside the
    bot's, so requests taken under one split have left the window before the other side grows."""
    cfg = enabled()
    chart, quiet = cfg.chart.requests_per_minute, cfg.chart.quiet_requests_per_minute
    route = cfg.environment.requests_per_route_per_minute
    chart_limit = chart_budget_from_config(cfg, utc_now=lambda: at).limit_for(BARS_PATH)
    bot_limit = budget_from_config(cfg, utc_now=lambda: at).limit_for(BARS_PATH)
    assert chart_limit.max_requests == (quiet if chart_quiet else chart)
    assert bot_limit.max_requests == route - (quiet if bot_quiet else chart)
    assert chart_limit.max_requests + bot_limit.max_requests <= route
    # Other routes and the probe's view of the route never change with the hour.
    assert budget_from_config(cfg, utc_now=lambda: at).limit_for(Route.SNAPSHOT.path) \
        .max_requests == route
    assert budget_from_config(cfg, utc_now=lambda: at, quiet_split=False) \
        .limit_for(BARS_PATH).max_requests == route - chart


def test_quiet_slice_counts_requests_already_taken():
    """At the end of quiet hours the chart's limit drops back while its requests still count:
    it refuses until they leave the window, never adding the daytime slice on top."""
    cfg = enabled()
    now = [ny(16, 3, 58)]
    clock = [0.0]
    budget = chart_budget_from_config(cfg, clock=lambda: clock[0], utc_now=lambda: now[0])
    for _ in range(cfg.chart.quiet_requests_per_minute):
        budget.take(BARS_PATH)
    with pytest.raises(BudgetExhausted):
        budget.take(BARS_PATH)
    now[0], clock[0] = ny(16, 3, 59, 30), 30.0
    assert budget.usage(BARS_PATH).remaining == 0
    with pytest.raises(BudgetExhausted):
        budget.take(BARS_PATH)
    now[0], clock[0] = ny(16, 4, 0, 1), 60.0  # all of them have left the window
    budget.take(BARS_PATH)
    assert budget.usage(BARS_PATH).used == 1


def test_without_a_calendar_the_daytime_split_stays(tmp_path):
    cfg = enabled()
    cfg = replace(cfg, root=tmp_path)  # no calendar files under this root
    assert chart_budget_from_config(cfg, utc_now=lambda: ny(14, 12)).limit_for(BARS_PATH) \
        .max_requests == cfg.chart.requests_per_minute
    bot = budget_from_config(cfg, utc_now=lambda: ny(14, 12)).limit_for(BARS_PATH)
    assert bot.max_requests == cfg.environment.requests_per_route_per_minute \
        - cfg.chart.requests_per_minute
