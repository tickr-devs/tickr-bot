"""Read-only Webull probes for the owner's Mac: `python -m tickr.probes <command> SYMBOL`.

They answer open facts of docs/webull-facts.md with market-data reads only (no account route,
no order, the account id is never read). Section C, with chart bars reads:

- `chart-limit` (C2): is the bars route limit counted per App Key across processes? It starts
  `[probe] chart_limit_processes` processes that each send `chart_limit_requests` bars requests
  inside one route window. Each process stays within the route limit on its own; together they
  pass it. An HTTP 429 in any of them says the limit is shared. This is deliberately more than
  one process may send, so it REFUSES while a bot process is recorded in the lock (it has no
  place in the bot's budget) and during the regular session, when the watchdog may start a bot
  at any moment and a paused bars route would cost it evaluations.
- `chart-client` (C1): can a second Webull client read next to the running bot? It reads bars
  from its own market-data-only client while the bot runs, and after each read notes whether the
  bot still reads the broker and whether the token folder changed. It runs only inside the
  chart's own slice of the bars limit: `[chart] webull_enabled` must be true, and the config
  file must not have changed since the bot started (the bot gave up that slice when it started).
  If the experiment fails the bot may lose its broker reads, so it REFUSES to start while the
  bot's status or its last broker read shows a position or a working order of its own, or
  either cannot be read or is stale.

Section K, on the L1 stream:

- `tick SYMBOL...` (K): does the stream deliver trade-by-trade TICK messages on our
  entitlement, and how many symbols may one connection hold for TICK? See
  tickr/engine/tick_probe.py. Only one device may hold the market-data entitlement, so it
  REFUSES while a bot process is recorded in the lock (the bot's own stream would be taken).
  The cap ramp takes its symbols from the three screeners after the given ones.

Section H, account read only (the adapter reads the account id itself; it is never printed):

- `history` (H2): how many rows one order-history page holds and in which order they come, read
  page by page up to `[probe] history_max_pages`. Next to a running bot only while its status
  and last broker read show it flat (the bot's own history reads share the route).
- `history-since` (H2b edges): whether the order history's window may end in the future (the
  next New York midnight) and whether it filters on placing time. Read only, flat as above.

Every request is sent once and never repeated. Evidence goes to
`<data_dir>/<[probe] evidence_dir>/<NY date>/<probe>-<UTC time>/`: one JSON line per request and
a `summary.json`. Nothing in it names the account or a credential; the token folder is described
by file count, sizes and modification times only.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from collections.abc import Callable, Sequence
from datetime import UTC, datetime, timedelta
from datetime import time as dt_time
from decimal import Decimal
from pathlib import Path
from typing import Any

from tickr import config as config_module
from tickr.api import contract as api
from tickr.api.chart import CHART_SYMBOL
from tickr.broker.base import BrokerError, RateLimited
from tickr.broker.budget import BudgetExhausted
from tickr.broker.webull import (
    BrokerReadFailed,
    Route,
    broker_from_config,
    budget_from_config,
    chart_market_data_from_config,
    chart_probe_market_data_from_config,
    tick_probe_stream_from_config,
)
from tickr.calendar import (
    NEW_YORK,
    CalendarCause,
    CalendarRefused,
    SessionCalendar,
    ny_date_of,
)
from tickr.cli import data_paths
from tickr.config import Config, ConfigError
from tickr.data.base import BarSpan, MarketData, MarketDataError, ScreenerKind, SymbolsRefused
from tickr.engine import tick_probe
from tickr.engine.core import Fence
from tickr.lock import AlreadyRunning, SingleInstanceLock, recorded_holder

MICROSECONDS_PER_MILLISECOND = 1000  # unit factor: the history window's milliseconds
# One bar is enough: the probes count requests and answers, never prices.
BARS_PER_READ = 1

OK = "ok"
NOT_SENT = "not-sent"
HTTP_429 = "http-429"
READ_FAILED = "read-failed"
ERROR = "error"

SHARED = "shared"
NOT_SHARED = "not-shared"
INCONCLUSIVE = "inconclusive"
PASSED = "passed"
UNFINISHED = "unfinished"
FAILED = "failed"

SUMMARY_FILE = "summary.json"
EXIT_ANSWERED = 0
EXIT_CONFIG = 2
EXIT_REFUSED = 3
EXIT_NO_ANSWER = 4

Clock = Callable[[], datetime]
Sleep = Callable[[float], None]
Launch = Callable[[list[list[str]]], list[int]]


class ProbeRefused(Exception):
    """The probe may not run now; nothing was sent. The message is the single cause."""


def read_once(market: MarketData, symbol: str, now: datetime) -> dict[str, Any]:
    """One bars request and what came back, as evidence. Never raises for a broker answer."""
    try:
        bars = market.history_bars(symbol, BarSpan.M1, now, BARS_PER_READ)
    except BudgetExhausted as exc:  # our own budget refused: nothing was sent
        return {"outcome": NOT_SENT, "cause": exc.cause}
    except RateLimited:
        return {"outcome": HTTP_429}
    except (BrokerReadFailed, SymbolsRefused) as exc:
        return {"outcome": READ_FAILED, "http_status": exc.http_status, "code": exc.code}
    except (BrokerError, MarketDataError) as exc:
        return {"outcome": ERROR, "type": type(exc).__name__}
    return {"outcome": OK, "bars": len(bars)}


def _wait_until(due: datetime, now: Clock, sleep: Sleep) -> None:
    delay = (due - now()).total_seconds()
    if delay > 0:
        sleep(delay)


def _append(path: Path, row: dict[str, Any]) -> None:
    with path.open("a", encoding="utf-8") as out:
        out.write(json.dumps(row, sort_keys=True) + "\n")


def _rows(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text("utf-8").splitlines() if line]


def evidence_folder(cfg: Config, probe: str, at: datetime) -> Path:
    folder = (cfg.root / cfg.run.data_dir / cfg.probe.evidence_dir / ny_date_of(at).isoformat()
              / f"{probe}-{at.astimezone(UTC):%H%M%S}")
    folder.mkdir(parents=True, exist_ok=False)
    return folder


def _require_symbol(symbol: str) -> None:
    if not CHART_SYMBOL.match(symbol):
        raise ProbeRefused(f"{symbol!r} is not a ticker")


# --------------------------------------------------------------------------------------------
# chart-limit (C2)
# --------------------------------------------------------------------------------------------

NO_SESSION_CAUSES = frozenset({CalendarCause.WEEKEND, CalendarCause.HOLIDAY})


def _refuse_in_regular_session(cfg: Config, at: datetime) -> None:
    """Outside the regular session only. A calendar that cannot answer is a refusal too."""
    try:
        day = SessionCalendar.load((cfg.root / cfg.session.calendar_file).parent).day_at(at)
    except CalendarRefused as exc:
        if exc.cause in NO_SESSION_CAUSES:
            return
        raise ProbeRefused(f"the calendar cannot say whether the regular session is on: "
                           f"{exc.cause}: {exc.detail}") from exc
    if day.in_regular_session(at):
        raise ProbeRefused("the regular session is on: the watchdog may start a bot at any "
                           "moment, and this probe can pause the bars route it needs")


def run_limit_worker(market: MarketData, symbol: str, requests: int, window_seconds: float,
                     start_at: datetime, out: Path, *, now: Clock, sleep: Sleep) -> None:
    """One process of `chart-limit`: `requests` reads spread evenly over one route window that
    starts at `start_at`, each written to `out` as soon as it is answered."""
    step = timedelta(seconds=window_seconds / requests)
    for seq in range(requests):
        _wait_until(start_at + seq * step, now, sleep)
        sent_at = now()
        answer = read_once(market, symbol, sent_at)
        _append(out, {"seq": seq, "sent_at": sent_at.isoformat(),
                      "answered_at": now().isoformat(), **answer})


def limit_verdict(workers: Sequence[Sequence[dict[str, Any]]], requests: int) -> str:
    """SHARED: Webull refused a request with HTTP 429 although no process passed the limit on
    its own. NOT_SHARED: every request of every process was answered. Anything else (a process
    that did not finish, a failed read) proves nothing."""
    rows = [row for worker in workers for row in worker]
    if any(row["outcome"] == HTTP_429 for row in rows):
        return SHARED
    if all(len(worker) == requests for worker in workers) and all(
            row["outcome"] == OK for row in rows):
        return NOT_SHARED
    return INCONCLUSIVE


def _counts(rows: Sequence[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in rows:
        counts[row["outcome"]] = counts.get(row["outcome"], 0) + 1
    return counts


def _launch(commands: list[list[str]]) -> list[int]:
    processes = [subprocess.Popen(command) for command in commands]  # noqa: S603 - our own argv
    return [process.wait() for process in processes]


def chart_limit(cfg: Config, config_path: Path, symbol: str, *, now: Clock,
                launch: Launch = _launch) -> tuple[str, Path]:
    _require_symbol(symbol)
    _, lock_path = data_paths(cfg)
    pid = recorded_holder(lock_path)
    if pid is not None:
        raise ProbeRefused(f"a bot process (pid {pid}) is recorded in the lock; this probe "
                           "sends more than one process may and has no place in its budget")
    _refuse_in_regular_session(cfg, now())
    probe = cfg.probe
    limit = budget_from_config(cfg, quiet_split=False).limit_for(Route.BARS.path)
    if probe.chart_limit_requests > limit.max_requests:
        raise ProbeRefused(f"[probe] chart_limit_requests {probe.chart_limit_requests} is above "
                           f"the bars route limit {limit.max_requests}")
    total = probe.chart_limit_processes * probe.chart_limit_requests
    if total <= limit.max_requests:
        raise ProbeRefused(f"{total} requests in all do not pass the bars route limit "
                           f"{limit.max_requests}: the probe would prove nothing")
    started = now()
    folder = evidence_folder(cfg, "chart-limit", started)
    start_at = started + timedelta(seconds=probe.chart_limit_start_delay_seconds)
    files = [folder / f"worker-{index}.jsonl" for index in range(probe.chart_limit_processes)]
    codes = launch([[sys.executable, "-m", "tickr.probes", "--config", str(config_path),
                     "chart-limit-worker", symbol, "--out", str(out),
                     "--start-at", start_at.isoformat()] for out in files])
    workers = [_rows(out) for out in files]
    verdict = limit_verdict(workers, probe.chart_limit_requests)
    (folder / SUMMARY_FILE).write_text(json.dumps({
        "probe": "chart-limit", "environment": cfg.environment_name, "symbol": symbol,
        "started_at": started.isoformat(), "window_start": start_at.isoformat(),
        "route_limit": limit.max_requests, "window_seconds": limit.window_seconds,
        "requests_per_process": probe.chart_limit_requests,
        "workers": [{"exit_code": code, "outcomes": _counts(rows)}
                    for code, rows in zip(codes, workers, strict=True)],
        "verdict": verdict,
    }, indent=1, sort_keys=True) + "\n", "utf-8")
    return verdict, folder


# --------------------------------------------------------------------------------------------
# chart-client (C1)
# --------------------------------------------------------------------------------------------


def token_fingerprint(folder: Path) -> list[tuple[int, int]]:
    """Size and modification time of every file in the token folder. Never a name or content."""
    if not folder.is_dir():
        return []
    stats = [path.stat() for path in folder.rglob("*") if path.is_file()]
    return sorted((stat.st_size, stat.st_mtime_ns) for stat in stats)


def bot_health(cfg: Config, at: datetime) -> dict[str, Any]:
    """What the bot's own status file says about its broker reads right now."""
    path = cfg.root / cfg.run.data_dir / cfg.api.status_file
    try:
        status = api.read_status(path)
    except (OSError, api.ContractError) as exc:
        return {"readable": False, "type": type(exc).__name__}
    last_read = status.freshness.last_broker_read_at
    return {
        "readable": True,
        "status_age_seconds": round((at - status.written_at).total_seconds()),
        "last_broker_read_at": None if last_read is None else last_read.isoformat(),
        "broker_read_failed": any(fence.kind.split(":", 1)[0] == Fence.BROKER_READ
                                  for fence in status.fences),
    }


def _refuse_while_exposed(cfg: Config, at: datetime) -> None:
    """Only next to a bot whose fresh status shows no position and no working order of its own:
    if the second client costs the bot its broker reads, nothing of ours is then at stake."""
    path = cfg.root / cfg.run.data_dir / cfg.api.status_file
    try:
        status = api.read_status(path)
    except (OSError, api.ContractError) as exc:
        raise ProbeRefused(f"the bot's status file cannot be read ({type(exc).__name__}), so "
                           "it is unknown whether a trade is open") from exc
    age = (at - status.written_at).total_seconds()
    if age > cfg.api.status_stale_seconds:
        raise ProbeRefused(f"the bot's status is {round(age)} s old, so it is unknown whether "
                           "a trade is open")
    block = status.entry_block
    if status.positions or status.position is not None or (
            block is not None and block.cause == api.OWN_TRADE_OPEN):
        raise ProbeRefused("the bot has an open position or a working order of its own; run "
                           "this probe while it is flat")
    # The status hides a working entry behind an earlier entry block (after the last-entry
    # time, say), so the bot's last broker read has to agree.
    try:
        account = api.read_account(cfg.root / cfg.run.data_dir / cfg.api.account_file)
    except (OSError, api.ContractError) as exc:
        raise ProbeRefused(f"the bot's account file cannot be read ({type(exc).__name__}), so "
                           "it is unknown whether a trade is open") from exc
    age = (at - account.read_at).total_seconds()
    if age > cfg.api.account_stale_seconds:
        raise ProbeRefused(f"the bot's last broker read is {round(age)} s old, so it is "
                           "unknown whether a trade is open")
    if account.positions or any(order.ours for order in account.orders):
        raise ProbeRefused("the broker shows a position or a working order of the bot's; run "
                           "this probe while it is flat")


def client_problems(rows: Sequence[dict[str, Any]], token_changed: bool,
                    status_stale_seconds: Decimal) -> list[str]:
    """Every reason the run does not show a second client living next to the bot."""
    problems = []
    refused = [row["seq"] for row in rows if row["outcome"] != OK]
    if refused:
        problems.append(f"probe reads not answered: {refused}")
    if token_changed:
        problems.append("the token folder changed during the run")
    for row in rows:
        bot = row["bot"]
        if not bot["readable"]:
            problems.append(f"read {row['seq']}: the bot's status file was unreadable")
        elif bot["status_age_seconds"] > status_stale_seconds:
            problems.append(f"read {row['seq']}: the bot's status was "
                            f"{bot['status_age_seconds']} s old")
        elif bot["broker_read_failed"]:
            problems.append(f"read {row['seq']}: the bot reports failing broker reads")
    reads = [row["bot"].get("last_broker_read_at") for row in rows]
    if reads and (reads[0] is None or reads[-1] is None or
                  datetime.fromisoformat(reads[-1]) <= datetime.fromisoformat(reads[0])):
        problems.append("the bot's last broker read did not move during the run")
    return problems


def chart_client(cfg: Config, config_path: Path, symbol: str, *, now: Clock, sleep: Sleep,
                 market: Callable[[Config], MarketData] = chart_market_data_from_config,
                 ) -> tuple[str, Path]:
    _require_symbol(symbol)
    _, lock_path = data_paths(cfg)
    if recorded_holder(lock_path) is None:
        raise ProbeRefused("no bot process is recorded in the lock; this probe is about a "
                           "second client next to the running bot")
    if not cfg.chart.webull_enabled:
        raise ProbeRefused("[chart] webull_enabled is false: the bot keeps the whole bars "
                           "limit, so these reads would not be inside any budget")
    if config_path.stat().st_mtime_ns > lock_path.stat().st_mtime_ns:
        raise ProbeRefused(f"{config_path} changed after the bot started; the running bot may "
                           "not have given up the chart's slice of the bars limit")
    _refuse_while_exposed(cfg, now())
    probe = cfg.probe
    if probe.chart_client_reads <= 1:
        raise ProbeRefused("[probe] chart_client_reads must be above 1")
    source = market(cfg)  # its own market-data-only client and the chart's own budget
    token_folder = cfg.root / cfg.run.data_dir / cfg.environment.token_dir
    token_before = token_fingerprint(token_folder)
    started = now()
    folder = evidence_folder(cfg, "chart-client", started)
    out = folder / "reads.jsonl"
    for seq in range(probe.chart_client_reads):
        _wait_until(started + timedelta(seconds=seq * probe.chart_client_interval_seconds),
                    now, sleep)
        sent_at = now()
        answer = read_once(source, symbol, sent_at)
        _append(out, {"seq": seq, "sent_at": sent_at.isoformat(), **answer,
                      "bot": bot_health(cfg, now())})
    rows = _rows(out)
    token_after = token_fingerprint(token_folder)
    problems = client_problems(rows, token_after != token_before,
                               cfg.api.status_stale_seconds)
    verdict = FAILED if problems else PASSED
    (folder / SUMMARY_FILE).write_text(json.dumps({
        "probe": "chart-client", "environment": cfg.environment_name, "symbol": symbol,
        "started_at": started.isoformat(), "finished_at": now().isoformat(),
        "outcomes": _counts(rows),
        "token_files": {"before": len(token_before), "after": len(token_after),
                        "changed": token_after != token_before},
        "problems": problems, "verdict": verdict,
    }, indent=1, sort_keys=True) + "\n", "utf-8")
    return verdict, folder


def chart_client_gate(cfg: Config) -> str | None:
    """Why the API may not build its own Webull chart client yet, or None once it may: the
    newest `chart-client` run recorded for this environment must have passed. With
    `[chart] webull_enabled` the API process is itself a second Webull client next to the bot
    (C1), so it waits for the probe, which runs that experiment with the bot flat and watched.
    A summary that cannot be read counts as a run that did not pass. A run folder without a
    summary (the probe crashed or is still running) is a run of unknown environment that did
    not pass, as new as the newest file in it."""
    how = ("run `python -m tickr.probes chart-client SYMBOL` next to the running bot, then "
           "start the API again")
    remove = "remove that run's folder, then "
    root = cfg.root / cfg.run.data_dir / cfg.probe.evidence_dir
    newest: tuple[datetime, str] | None = None
    for folder in sorted(root.glob("*/chart-client-*")):
        path = folder / SUMMARY_FILE
        if not path.exists():
            try:
                changed = max(item.stat().st_mtime for item in (folder, *folder.iterdir()))
            except OSError as exc:
                return (f"chart probe C1: {folder.name} cannot be read "
                        f"({type(exc).__name__}); {remove}{how}")
            run = (datetime.fromtimestamp(changed, UTC), UNFINISHED)
            if newest is None or run[0] > newest[0]:
                newest = run
            continue
        try:
            summary = json.loads(path.read_text("utf-8"))
            if summary["environment"] != cfg.environment_name:
                continue
            run = (datetime.fromisoformat(summary["started_at"]), str(summary["verdict"]))
            if run[0].tzinfo is None:
                raise ValueError("started_at has no time zone")
        except (OSError, ValueError, KeyError, TypeError) as exc:
            return (f"chart probe C1: {path.parent.name}/{SUMMARY_FILE} cannot be read "
                    f"({type(exc).__name__}); {remove}{how}")
        if newest is None or run[0] > newest[0]:
            newest = run
    if newest is None:
        return f"chart probe C1 is not recorded for {cfg.environment_name}; {how}"
    if newest[1] != PASSED:
        return (f"chart probe C1 last ended {newest[1]!r} for {cfg.environment_name} "
                f"({newest[0].isoformat()}); {how}")
    return None


# --------------------------------------------------------------------------------------------
# tick (K)
# --------------------------------------------------------------------------------------------


def tick(cfg: Config, symbols: Sequence[str], *, now: Clock, sleep: Sleep,
         market: Callable[[Config], MarketData],
         stream: Callable[[Config, Callable[[dict[str, Any]], None]], tick_probe.TickStream],
         out: Callable[[str], None] = print) -> tuple[int, Path]:
    for symbol in symbols:
        _require_symbol(symbol)
    _, lock_path = data_paths(cfg)
    pid = recorded_holder(lock_path)
    if pid is not None:
        raise ProbeRefused(f"a bot process (pid {pid}) is recorded in the lock; this probe "
                           "takes the market-data entitlement its stream holds")
    universe, reads = list(symbols), market(cfg)
    for kind in (ScreenerKind.TOP_ACTIVES, ScreenerKind.GAINERS, ScreenerKind.LOSERS):
        try:
            universe += [row.symbol for row in reads.screener(kind)]
        except (BrokerError, MarketDataError) as exc:
            out(f"screener {kind} not read ({type(exc).__name__}); the cap ramp has fewer "
                "symbols")
    probe = cfg.probe
    settings = tick_probe.TickSettings(
        float(probe.tick_connect_wait_seconds), float(probe.tick_flow_wait_seconds),
        probe.tick_cap_steps, float(probe.tick_step_wait_seconds),
        cfg.market_data.stream_max_symbols)
    try:
        # Held for the whole run, as the trading probes do: a bot job starting meanwhile is
        # refused instead of sharing the one market-data entitlement with this stream.
        with SingleInstanceLock(lock_path):
            folder = evidence_folder(cfg, "tick", now())
            out(f"tick {' '.join(symbols)}; evidence in {folder}")
            code = tick_probe.run(lambda on_event: stream(cfg, on_event), symbols, universe,
                                  settings, folder, now=now, sleep=sleep, out=out,
                                  environment=cfg.environment_name)
    except AlreadyRunning as exc:
        raise ProbeRefused("another tickr process holds the lock") from exc
    return code, folder


# --------------------------------------------------------------------------------------------
# history (H2)
# --------------------------------------------------------------------------------------------

NEWEST_FIRST = "newest-first"
OLDEST_FIRST = "oldest-first"
MIXED = "mixed"
ONE_PAGE = "one-page"
PAGED = "paged"


def history_order(times: Sequence[str]) -> str:
    """Row order of the history across pages, from `place_time_at` (ISO, UTC)."""
    stamps = [datetime.fromisoformat(t) for t in times if t]
    if all(a >= b for a, b in zip(stamps, stamps[1:], strict=False)):
        return NEWEST_FIRST
    if all(a <= b for a, b in zip(stamps, stamps[1:], strict=False)):
        return OLDEST_FIRST
    return MIXED


def history(cfg: Config, *, now: Clock, broker: Callable[[Config], Any],
            out: Callable[[str], None] = print) -> tuple[str, Path]:
    """H2: how many rows one order-history page holds and in which order, read page by page
    (`[probe] history_max_pages`). Read only. Next to a running bot only while it is flat (its
    own history reads share the route)."""
    _, lock_path = data_paths(cfg)
    if recorded_holder(lock_path) is not None:
        _refuse_while_exposed(cfg, now())
    started = now()
    pages = broker(cfg).history_page_shapes(cfg.probe.history_max_pages)
    folder = evidence_folder(cfg, "history", started)
    for seq, page in enumerate(pages):
        _append(folder / "pages.jsonl", {"seq": seq, **page})
    full = [page["rows"] for page in pages if page["next_page"]]
    times = [t for page in pages for t in page["place_times"]]
    summary = {
        "probe": "history", "environment": cfg.environment_name,
        "started_at": started.isoformat(), "pages": len(pages),
        "rows_per_page": [page["rows"] for page in pages],
        "combos_per_page": [page["combos"] for page in pages],
        "paging": PAGED if full else ONE_PAGE,
        "page_size": max(full) if full else None,
        "order": history_order(times),
        "oldest": min(times, default=None), "newest": max(times, default=None),
        "last_page_has_next": bool(pages and pages[-1]["next_page"]),
    }
    (folder / SUMMARY_FILE).write_text(json.dumps(summary, indent=1, sort_keys=True) + "\n",
                                       "utf-8")
    out(f"h2_history: {summary['pages']} page(s), rows per page {summary['rows_per_page']}, "
        f"combos per page {summary['combos_per_page']}; paging {summary['paging']}, "
        f"page size {summary['page_size']}; order {summary['order']}; "
        f"oldest {summary['oldest']}, newest {summary['newest']}")
    verdict = PASSED if summary["paging"] == PAGED and summary["order"] != MIXED else UNFINISHED
    return verdict, folder


def history_time(at: datetime) -> str:
    """An aware instant as the history's window takes it (H2b, 2026-11-17): UTC, three
    millisecond digits, a literal Z."""
    utc = at.astimezone(UTC)
    return (utc.strftime("%Y-%m-%dT%H:%M:%S.")
            + f"{utc.microsecond // MICROSECONDS_PER_MILLISECOND:03d}Z")


def history_since(cfg: Config, *, now: Clock, broker: Callable[[Config], Any],
                  out: Callable[[str], None] = print) -> tuple[str, Path]:
    """H2b edges, read only, next to a running bot only while it is flat (its own history
    reads share the route). H2b (2026-11-17) proved the window: `start_time` with `end_time`,
    both UTC yyyy-MM-dd'T'HH:mm:ss.SSS'Z' (alone, +0000 or epoch ms: HTTP 417). Fable's
    conditions before the bot relies on it (review of #629):
    - `to-now`: today's New York midnight to now (the form the bot sends);
    - `to-next-midnight`: the same start, the end at the next New York midnight, a time in the
      future: accepted, and does it list at least what `to-now` lists? Then the bot can end
      its window there and no clock skew or exclusive end can hide a just-placed order;
    - `after-oldest`: from 1 ms after the oldest `place_time_at` of `to-now`, to now: does
      that oldest order drop out (the window filters on placing time) or stay (it filters on
      a later time, such as the fill or the last update)?
    Passed when `to-next-midnight` is accepted, lists no row before midnight and at least as
    many rows as `to-now`."""
    _, lock_path = data_paths(cfg)
    if recorded_holder(lock_path) is not None:
        _refuse_while_exposed(cfg, now())
    started = now()
    day = started.astimezone(NEW_YORK).date()
    midnight = datetime.combine(day, dt_time(), NEW_YORK)
    next_midnight = datetime.combine(day + timedelta(days=1), dt_time(), NEW_YORK)
    client = broker(cfg)
    folder = evidence_folder(cfg, "history-since", started)
    shapes: dict[str, dict[str, Any]] = {}

    def read(name: str, start: datetime, end: datetime) -> dict[str, Any]:
        window = {"start_time": history_time(start), "end_time": history_time(end)}
        shape = {"name": name, **client.history_window_shape(window)}
        _append(folder / "reads.jsonl", shape)
        shapes[name] = shape
        return shape

    to_now = read("to-now", midnight, started)
    ahead = read("to-next-midnight", midnight, next_midnight)
    if to_now.get("oldest"):
        oldest = datetime.fromisoformat(to_now["oldest"])
        read("after-oldest", oldest + timedelta(milliseconds=1), started)
    future_end_ok = (ahead.get("rows") is not None and to_now.get("rows") is not None
                     and ahead["rows"] >= to_now["rows"]
                     and (ahead["oldest"] is None
                          or datetime.fromisoformat(ahead["oldest"]) >= midnight))
    after = shapes.get("after-oldest", {})
    filters_on_placing = (None if after.get("rows") is None else
                          after["oldest"] is None
                          or after["oldest"] > to_now["oldest"])
    (folder / SUMMARY_FILE).write_text(json.dumps({
        "probe": "history-since", "environment": cfg.environment_name,
        "started_at": started.isoformat(), "start": midnight.isoformat(),
        "next_midnight": next_midnight.isoformat(), "reads": list(shapes.values()),
        "future_end_ok": future_end_ok, "filters_on_placing": filters_on_placing},
        indent=1, sort_keys=True) + "\n", "utf-8")
    for s in shapes.values():
        out(f"h2_since {s['name']}: HTTP {s['status']}"
            + (f" {s.get('error_code')} {s.get('message')!r}" if s.get("rows") is None else
               f", rows {s['rows']} (own {s['own_rows']}), oldest {s['oldest']}, "
               f"newest {s['newest']}, next page {s['next_page']}"))
    out(f"h2_since future end accepted: {future_end_ok}; "
        f"filters on placing time: {filters_on_placing}")
    return (PASSED if future_end_ok else UNFINISHED), folder


# --------------------------------------------------------------------------------------------
# Command line
# --------------------------------------------------------------------------------------------

# Replaced in tests with fakes.
NOW: Clock = lambda: datetime.now(UTC)  # noqa: E731
SLEEP: Sleep = time.sleep
LIMIT_MARKET: Callable[[Config], MarketData] = chart_probe_market_data_from_config
CLIENT_MARKET: Callable[[Config], MarketData] = chart_market_data_from_config
TICK_STREAM: Callable[[Config, Callable[[dict[str, Any]], None]],
                      tick_probe.TickStream] = tick_probe_stream_from_config
HISTORY_BROKER: Callable[[Config], Any] = broker_from_config


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m tickr.probes", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--config", type=Path, default=Path("config/bot.toml"))
    sub = parser.add_subparsers(dest="command", required=True)
    for name, text in (("chart-limit", "C2: is the bars limit shared between processes? "
                                       "Only with the bot stopped"),
                       ("chart-client", "C1: a second client next to the running bot")):
        sub.add_parser(name, help=text).add_argument("symbol")
    sub.add_parser("history", help="H2: order-history page size and row order (read only; "
                   "next to a running bot only while it is flat)")
    sub.add_parser("history-since", help="H2b: does the order history take start_time, "
                   "and in which format (read only; next to a running bot only while it is "
                   "flat)")
    sub.add_parser("tick", help="K: trade-by-trade stream and its symbol cap. Only with the "
                   "bot stopped").add_argument("symbols", nargs="+")
    worker = sub.add_parser("chart-limit-worker", help="one process of chart-limit (internal)")
    worker.add_argument("symbol")
    worker.add_argument("--out", type=Path, required=True)
    worker.add_argument("--start-at", type=datetime.fromisoformat, required=True)
    args = parser.parse_args(argv)
    try:
        cfg = config_module.load(args.config)
        if args.command == "chart-limit-worker":
            limit = budget_from_config(cfg, quiet_split=False).limit_for(Route.BARS.path)
            run_limit_worker(LIMIT_MARKET(cfg), args.symbol, cfg.probe.chart_limit_requests,
                             limit.window_seconds, args.start_at, args.out,
                             now=NOW, sleep=SLEEP)
            return EXIT_ANSWERED
        if args.command == "history":
            verdict, folder = history(cfg, now=NOW, broker=HISTORY_BROKER)
            print(f"history: {verdict}; evidence in {folder}")
            return EXIT_ANSWERED if verdict == PASSED else EXIT_NO_ANSWER
        if args.command == "history-since":
            verdict, folder = history_since(cfg, now=NOW, broker=HISTORY_BROKER)
            print(f"history-since: {verdict}; evidence in {folder}")
            return EXIT_ANSWERED if verdict == PASSED else EXIT_NO_ANSWER
        if args.command == "tick":
            code, folder = tick(cfg, args.symbols, now=NOW, sleep=SLEEP, market=LIMIT_MARKET,
                                stream=TICK_STREAM)
            print(f"tick: evidence in {folder}")
            return code
        if args.command == "chart-limit":
            verdict, folder = chart_limit(cfg, args.config, args.symbol, now=NOW)
        else:
            verdict, folder = chart_client(cfg, args.config, args.symbol, now=NOW, sleep=SLEEP,
                                           market=CLIENT_MARKET)
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return EXIT_CONFIG
    except (ProbeRefused, BrokerError) as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return EXIT_REFUSED
    print(f"{args.command}: {verdict}; evidence in {folder}")
    return EXIT_ANSWERED if verdict in (SHARED, NOT_SHARED, PASSED) else EXIT_NO_ANSWER


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
