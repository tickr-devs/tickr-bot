"""POST and GET /lab/replay: the Lab's SIMULATED replay of a strategy version over the last
`[api] lab_replay_sessions` sessions of the backtest data already fetched (docs/backtest.md), or
over one of its pool days, optionally compared with a second version over the same window.

The API process starts `python -m tickr.backtest run --last-sessions N` (with `--end-day D`
for one day) as a child process under `nice`, in the repo root, writing into
`<data_dir>/<[api] lab_replay_results_dir>/<run_id>/<leg>/`. A comparison runs its two versions
one after the other, never side by side. It never fetches market data and never touches the
bot: the bot does not read these files.

Files of the results folder:
- `current.json`: the latest run (id, start, sessions, day, and per leg: folder, strategy,
  version, child pid once started, sub-folder);
- `<run_id>/<leg>/`: the backtest's results (report.json ...), `log.txt` (its output) and
  `exit.json` (finish time and exit code, written when the child is waited for).
A leg is `running` while its child is alive (or, for the second leg, while the first runs);
`done` once report.json exists; `failed` otherwise, with the last line the backtest printed
(`backtest refused: ...`) as the cause. A run written before comparisons existed has one leg
whose files are directly in `<run_id>/`.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import threading
import uuid
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from tickr.api import contract as api
from tickr.backtest.errors import BacktestError
from tickr.backtest.universe import POOL_FILE, read_pool
from tickr.calendar import CalendarCause, CalendarRefused, SessionCalendar
from tickr.config import Config, credential_variables
from tickr.observers.errors import ObserverError
from tickr.observers.loader import STRATEGIES_DIR, STRATEGY_FILE, parse_modules, read_identity

CURRENT = "current.json"
EXIT = "exit.json"
LOG = "log.txt"
REPORT = "report.json"
REFUSED_PREFIX = "backtest refused: "
# Only folders named like a run id are ever deleted, whatever the results folder is set to.
RUN_ID = re.compile(r"[0-9a-f]{32}")
NOT_STARTED = "the API was started without its config file path"
ENDED_WITHOUT_REPORT = "the run ended without a report"
NEVER_STARTED = "the API stopped before this version started"
LEGS = ("a", "b")  # sub-folder of the first and the second version of a run
# Calendar answers that mean "no session today", so a replay may run.
NO_SESSION = (CalendarCause.WEEKEND, CalendarCause.HOLIDAY)


class LabReplayRefused(Exception):
    """A replay that cannot start now; the message is the single cause."""


@dataclass(frozen=True)
class LabReplaySettings:
    config_path: Path | None  # the bot.toml the API loaded; the child reads the same file
    root: Path  # repo root: the child's working directory
    data: Path  # the fetched backtest data (pool.csv, minutes/)
    results: Path
    sessions: int
    niceness: int
    outside_session_only: bool
    calendar: Path
    active: str  # [strategy] active: its folder under strategies/
    observers: tuple[str, ...]  # [strategy] observers: folders under strategies/
    modules: tuple[str, ...]  # [observers] strategy_modules: name=module
    # NAMES of the credential variables (config.credential_variables) the child never gets: the
    # API holds the Webull app key and secret while [chart] webull_enabled (runtime/launchd.py),
    # and a replay runs strategy code that needs no credential.
    scrub: frozenset[str]

    @classmethod
    def from_config(cls, cfg: Config, config_path: Path | None) -> LabReplaySettings:
        data = cfg.root / cfg.run.data_dir
        return cls(
            config_path=None if config_path is None else config_path.resolve(),
            root=cfg.root, data=data / cfg.api.lab_replay_data_dir,
            results=data / cfg.api.lab_replay_results_dir,
            sessions=cfg.api.lab_replay_sessions, niceness=cfg.api.lab_replay_niceness,
            outside_session_only=cfg.api.lab_replay_outside_session_only,
            calendar=(cfg.root / cfg.session.calendar_file).parent,
            active=cfg.strategy.active, observers=tuple(cfg.strategy.observers),
            modules=tuple(cfg.observers.strategy_modules),
            scrub=credential_variables(cfg))


Spawn = Callable[..., Any]  # subprocess.Popen, or a fake in tests


class LabReplay:
    """One replay at a time, shared by both listeners of the API."""

    def __init__(self, settings: LabReplaySettings, clock: Callable[[], datetime],
                 spawn: Spawn = subprocess.Popen) -> None:
        self.settings = settings
        self.clock = clock
        self.spawn = spawn
        self._lock = threading.Lock()
        self._pool_key: tuple[int, int] | None = None
        self._pool_days: tuple[date, ...] = ()
        # The run id whose waiter thread is still alive: its run is running even in the moment
        # between one version's exit and the next one's start, when no child is alive.
        self._waiting: str | None = None

    # ------------------------------------------------------------ choices

    def versions(self) -> tuple[api.LabVersionV1, ...]:
        """Every folder under strategies/: the active one, the observers in configuration
        order, then the others by name. Read from their files only (nothing imported)."""
        s = self.settings
        try:
            folders = sorted(p.name for p in (s.root / STRATEGIES_DIR).iterdir() if p.is_dir())
        except OSError:
            folders = []
        ordered = [s.active] + [f for f in s.observers if f != s.active]
        ordered += [f for f in folders if f not in ordered]
        try:
            modules: dict[str, str] | None = parse_modules(s.modules)
        except ObserverError:
            modules = None
        out = []
        for folder in ordered:
            role = (api.LabVersionRole.ACTIVE if folder == s.active else
                    api.LabVersionRole.OBSERVER if folder in s.observers else
                    api.LabVersionRole.CANDIDATE)
            try:
                name, version = read_identity(s.root, folder)
            except ObserverError as exc:
                out.append(api.LabVersionV1(folder, None, None, role, self._relative(str(exc))))
                continue
            cause = None
            if modules is None:
                cause = "[observers] strategy_modules is malformed"
            elif name not in modules:
                cause = f"no module for strategy {name!r} in [observers] strategy_modules"
            out.append(api.LabVersionV1(folder, name, version, role, cause))
        return tuple(out)

    def pool_days(self) -> tuple[date, ...]:
        """Every session date of the fetched pool, newest first; empty without a readable
        pool. Read again only when pool.csv changed."""
        path = self.settings.data / POOL_FILE
        try:
            stat = path.stat()
        except OSError:
            return ()
        key = (stat.st_mtime_ns, stat.st_size)
        with self._lock:
            if key == self._pool_key:
                return self._pool_days
        try:
            days = tuple(sorted({e.session_date for e in read_pool(path)}, reverse=True))
        except (BacktestError, KeyError, ValueError, ArithmeticError):
            days = ()
        with self._lock:
            self._pool_key, self._pool_days = key, days
        return days

    # ------------------------------------------------------------ reading

    def state(self) -> api.LabReplayV1:
        """The latest run, with what a run may ask for and when a start is next allowed."""
        return replace(self._read(), available_at=self.available_at(self.clock()))

    def available_at(self, now: datetime) -> datetime | None:
        """The regular close a start waits for while `[api] lab_replay_outside_session_only`
        refuses it in the regular session; None when the session rule allows a start now (or
        the calendar cannot say: a start then refuses with the calendar's own cause)."""
        if not self.settings.outside_session_only:
            return None
        try:
            day = SessionCalendar.load(self.settings.calendar).day_at(now)
        except (CalendarRefused, OSError):
            return None
        return day.regular_close if day.in_regular_session(now) else None

    def _read(self) -> api.LabReplayV1:
        versions, days = self.versions(), self.pool_days()[:self.settings.sessions]
        try:
            current = self._current()
        except (OSError, ValueError) as exc:
            return api.LabReplayV1(api.LabReplayState.FAILED, None, None, None,
                                   self.settings.sessions, None, None,
                                   f"{CURRENT} unreadable ({type(exc).__name__})", None,
                                   versions=versions, days=days)
        if current is None:
            return api.LabReplayV1(api.LabReplayState.IDLE, None, None, None,
                                   self.settings.sessions, None, None, None, None,
                                   versions=versions, days=days)
        try:
            return self._state(current, versions, days)
        except (KeyError, IndexError, TypeError, ValueError, OSError, api.ContractError) as exc:
            return api.LabReplayV1(api.LabReplayState.FAILED, None, None, None,
                                   self.settings.sessions, None, None,
                                   f"{CURRENT} unreadable ({type(exc).__name__})", None,
                                   versions=versions, days=days)

    def _state(self, current: dict[str, Any], versions: tuple[api.LabVersionV1, ...],
               days: tuple[date, ...]) -> api.LabReplayV1:
        run = self.settings.results / current["run_id"]
        legs = _legs(current)
        waiting = self._waiter_alive(current["run_id"])
        results: list[_Leg] = []
        for leg in legs:
            before = results[-1].state if results else None
            results.append(self._leg(run / leg["dir"], leg["pid"], before, waiting))
        first = results[0]
        running = any(r.state is api.LabReplayState.RUNNING for r in results)
        finished = None if running or any(r.finished is None for r in results) else max(
            r.finished for r in results if r.finished is not None)
        compare = None
        if len(legs) > 1:
            second = results[1]
            compare = api.LabReplayLegV1(legs[1]["folder"], legs[1]["strategy"],
                                         legs[1]["version"], second.state, second.cause,
                                         second.summary)
        state = api.LabReplayState.RUNNING if running else first.state
        day = current.get("day")
        # The first version's numbers wait until the whole run ended, as `summary` documents.
        cause, summary = (None, None) if running else (first.cause, first.summary)
        return api.LabReplayV1(
            state, current["run_id"], legs[0]["strategy"], legs[0]["version"],
            current["sessions"], api.parse_time(current["started_at"]), finished, cause,
            summary, folder=legs[0]["folder"],
            day=None if day is None else date.fromisoformat(day), compare=compare,
            versions=versions, days=days)

    def _waiter_alive(self, run_id: str) -> bool:
        with self._lock:
            return self._waiting == run_id

    def _leg(self, folder: Path, pid: int | None, before: api.LabReplayState | None,
             waiting: bool) -> _Leg:
        """One version's state from its folder; `before`: the state of the leg ahead of it;
        `waiting`: the run's waiter thread is alive, so a leg without exit.json still runs."""
        finished: datetime | None = None
        code: int | None = None
        exit_path = folder / EXIT
        if exit_path.exists():
            done = json.loads(exit_path.read_text("utf-8"))
            finished, code = api.parse_time(done["finished_at"]), done["code"]
        elif waiting:
            return _Leg(api.LabReplayState.RUNNING, None, None, None)
        elif pid is None:
            if before is api.LabReplayState.RUNNING:
                return _Leg(api.LabReplayState.RUNNING, None, None, None)
            return _Leg(api.LabReplayState.FAILED, None, NEVER_STARTED, None)
        elif _alive(pid):
            return _Leg(api.LabReplayState.RUNNING, None, None, None)
        report = folder / REPORT
        if (code is None or code == 0) and report.exists():
            if finished is None:
                finished = datetime.fromtimestamp(report.stat().st_mtime).astimezone()
            try:
                summary = summarize(json.loads(report.read_text("utf-8")))
            except (KeyError, ValueError, TypeError, api.ContractError) as exc:
                return _Leg(api.LabReplayState.FAILED, finished,
                            f"report.json unreadable ({type(exc).__name__})", None)
            return _Leg(api.LabReplayState.DONE, finished, None, summary)
        return _Leg(api.LabReplayState.FAILED, finished, self._cause(folder), None)

    def _current(self) -> dict[str, Any] | None:
        path = self.settings.results / CURRENT
        if not path.exists():
            return None
        return json.loads(path.read_text("utf-8"))

    def _cause(self, folder: Path) -> str:
        """The backtest's own refusal, with the repo root taken out of any path in it."""
        try:
            lines = (folder / LOG).read_text("utf-8").splitlines()
        except OSError:
            return ENDED_WITHOUT_REPORT
        refused = [line for line in lines if line.startswith(REFUSED_PREFIX)]
        if not refused:
            return ENDED_WITHOUT_REPORT
        return self._relative(refused[-1])

    def _relative(self, text: str) -> str:
        """`text` with the repo root taken out of its paths: no home folder leaves the Mac."""
        for root in {str(self.settings.root), str(self.settings.root.resolve())}:
            text = text.replace(root + os.sep, "")
        return text

    # ------------------------------------------------------------ starting

    def start(self, folder: str | None = None, compare: str | None = None,
              day: date | None = None) -> api.LabReplayV1:
        """Start a replay now of strategies/<folder> (the active one when None), then of
        `compare` over the same window, over the last sessions or over pool day `day`; or raise
        LabReplayRefused with the cause."""
        s = self.settings
        with self._lock:
            if self._running():
                raise LabReplayRefused("a replay is already running")
            now = self.clock()
            if s.outside_session_only:
                self._refuse_in_session(now)
            if s.config_path is None:
                raise LabReplayRefused(NOT_STARTED)
        if not (s.data / POOL_FILE).exists():
            raise LabReplayRefused(f"no fetched backtest data: {POOL_FILE} is missing in "
                                   f"[api] lab_replay_data_dir")
        if day is not None and day not in self.pool_days():
            raise LabReplayRefused(f"{day.isoformat()} is not a day of the fetched data")
        versions = {v.folder: v for v in self.versions()}
        chosen = [folder or s.active] + ([compare] if compare is not None else [])
        if len(set(chosen)) != len(chosen):
            raise LabReplayRefused("compare names the same version twice")
        try:
            modules = parse_modules(s.modules)
        except ObserverError as exc:
            raise LabReplayRefused(str(exc)) from exc
        legs = []
        for name, sub in zip(chosen, LEGS, strict=False):
            found = versions.get(name)
            if found is None:
                raise LabReplayRefused(f"no strategy folder {name!r} under {STRATEGIES_DIR}/")
            if found.cause is not None or found.strategy is None:
                raise LabReplayRefused(found.cause or f"{name}: unreadable")
            legs.append({"folder": name, "strategy": found.strategy, "version": found.version,
                         "module": modules[found.strategy], "pid": None, "dir": sub})
        sessions = 1 if day is not None else s.sessions
        with self._lock:
            if self._running():
                raise LabReplayRefused("a replay is already running")
            run_id = uuid.uuid4().hex
            run = s.results / run_id
            s.results.mkdir(parents=True, exist_ok=True)
            for old in s.results.iterdir():  # only the latest run is kept
                if old.is_dir() and not old.is_symlink() and RUN_ID.fullmatch(old.name):
                    shutil.rmtree(old)
            run.mkdir()
            current = {"run_id": run_id, "started_at": api.format_time(now),
                       "sessions": sessions, "day": None if day is None else day.isoformat(),
                       "legs": legs}
            child = self._spawn(run, legs[0], sessions, day)
            legs[0]["pid"] = child.pid
            _write(s.results / CURRENT, current)
            later = [self._spawn_later(run, current, i, sessions, day)
                     for i in range(1, len(legs))]
            self._waiting = run_id
            threading.Thread(target=self._wait, args=(run, current, child, later),
                             daemon=True).start()
        return self.state()

    def _running(self) -> bool:
        """Called with the lock held. A run whose current.json cannot be read is not running:
        the next start replaces it."""
        if self._waiting is not None:
            return True
        try:
            current = self._current()
            if current is None:
                return False
            run = self.settings.results / current["run_id"]
            before = None
            for leg in _legs(current):
                before = self._leg(run / leg["dir"], leg["pid"], before, False).state
                if before is api.LabReplayState.RUNNING:
                    return True
        except (KeyError, IndexError, TypeError, ValueError, OSError):
            return False
        return False

    def _command(self, run: Path, leg: dict[str, Any], sessions: int,
                 day: date | None) -> list[str]:
        s = self.settings
        command = ["nice", "-n", str(s.niceness), sys.executable, "-m", "tickr.backtest",
                   "--config", str(s.config_path), "run", "--out", str(s.data),
                   "--results", str(run / leg["dir"]), "--strategy", leg["module"],
                   "--values", str(s.root / STRATEGIES_DIR / leg["folder"] / STRATEGY_FILE),
                   "--last-sessions", str(sessions)]
        if day is not None:
            command += ["--end-day", day.isoformat()]
        return command

    def _spawn(self, run: Path, leg: dict[str, Any], sessions: int, day: date | None) -> Any:
        folder = run / leg["dir"]
        folder.mkdir(parents=True, exist_ok=True)
        env = os.environ.copy()
        for name in self.settings.scrub:
            env.pop(name, None)
        with open(folder / LOG, "w", encoding="utf-8") as log:
            child = self.spawn(self._command(run, leg, sessions, day), cwd=self.settings.root,
                               stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                               start_new_session=True, env=env)
        return child

    def _spawn_later(self, run: Path, current: dict[str, Any], index: int, sessions: int,
                     day: date | None) -> Callable[[], Any]:
        def start() -> Any:
            leg = current["legs"][index]
            child = self._spawn(run, leg, sessions, day)
            leg["pid"] = child.pid
            with self._lock:
                _write(self.settings.results / CURRENT, current)
            return child
        return start

    def _refuse_in_session(self, now: datetime) -> None:
        try:
            day = SessionCalendar.load(self.settings.calendar).day_at(now)
        except CalendarRefused as exc:
            if exc.cause in NO_SESSION:
                return
            raise LabReplayRefused(f"calendar: {exc}") from exc
        if day.in_regular_session(now):
            raise LabReplayRefused(f"refused during the regular session until "
                                   f"{api.format_time(day.regular_close)} "
                                   f"([api] lab_replay_outside_session_only)")

    def _wait(self, run: Path, current: dict[str, Any], child: Any,
              later: list[Callable[[], Any]]) -> None:
        """Wait for each version in turn, the next one started before this one's exit.json.
        Until the last exit.json is written the run counts as running (`_waiting`), so no
        reader sees a gap between the versions and no start can replace the run in it."""
        try:
            self._wait_legs(run, current, child, later)
        finally:
            with self._lock:
                if self._waiting == current["run_id"]:
                    self._waiting = None

    def _wait_legs(self, run: Path, current: dict[str, Any], child: Any,
                   later: list[Callable[[], Any]]) -> None:
        legs = current["legs"]
        for index, leg in enumerate(legs):
            code = child.wait()
            stop = False
            if index + 1 < len(legs):
                try:
                    child = later[index]()
                except OSError as exc:
                    following = run / legs[index + 1]["dir"]
                    _refused(following, f"cannot start: {exc}")
                    self._exit(following, -1)
                    stop = True
            self._exit(run / leg["dir"], code)
            if stop:
                return

    def _exit(self, folder: Path, code: int) -> None:
        _write(folder / EXIT, {"finished_at": api.format_time(self.clock()), "code": code})


@dataclass(frozen=True)
class _Leg:
    state: api.LabReplayState
    finished: datetime | None
    cause: str | None
    summary: api.LabReplaySummaryV1 | None


def _legs(current: dict[str, Any]) -> list[dict[str, Any]]:
    """The run's versions; a run from before comparisons is one leg in the run folder."""
    legs = current.get("legs")
    if legs is not None:
        return legs
    return [{"folder": None, "strategy": current["strategy"], "version": current["version"],
             "pid": current["pid"], "dir": ""}]


def _refused(folder: Path, cause: str) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    with open(folder / LOG, "a", encoding="utf-8") as log:
        log.write(f"{REFUSED_PREFIX}{cause}\n")


def _write(path: Path, body: dict[str, Any]) -> None:
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(body) + "\n", "utf-8")
    os.replace(temporary, path)


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _dec(value: Any) -> Decimal | None:
    return None if value is None else Decimal(str(value))


def summarize(report: dict[str, Any]) -> api.LabReplaySummaryV1:
    """The Lab's numbers from the backtest's report.json (raises on a malformed one)."""
    every, bot, window = report["all_signals"], report["bot"], report["last_sessions"]
    interval = every.get("mean_r_interval")
    return api.LabReplaySummaryV1(
        first_day=date.fromisoformat(window["first"]),
        last_day=date.fromisoformat(window["last"]),
        sessions=int(window["requested"]),
        sessions_without_replayed_days=int(window["sessions_without_replayed_days"]),
        replayed_symbol_days=int(report["pool"]["replayed_symbol_days"]),
        trades=int(every["trades"]),
        win_rate_percent=_dec(every.get("win_rate_percent")),
        mean_r=_dec(every.get("mean_r")),
        total_r=_dec(every.get("total_r")),
        profit_factor=_dec(every.get("profit_factor")),
        mean_r_interval_low=_dec(interval[0]) if interval else None,
        mean_r_interval_high=_dec(interval[1]) if interval else None,
        confidence_percent=Decimal(str(every["confidence_percent"])),
        max_drawdown_r=_dec(every.get("max_drawdown_r")),
        bot_trades=int(bot["trades"]),
        bot_total_usd=Decimal(str(bot["total_usd"])),
        bot_max_drawdown_usd=_dec(bot.get("max_drawdown_usd")),
    )
