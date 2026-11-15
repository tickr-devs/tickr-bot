"""POST and GET /lab/replay: the Lab's SIMULATED replay of the active strategy over the last
`[api] lab_replay_sessions` sessions of the backtest data already fetched (docs/backtest.md).

The API process starts `python -m tickr.backtest run --last-sessions N` as a child process
under `nice`, in the repo root, writing into `<data_dir>/<[api] lab_replay_results_dir>/<run_id>/`.
It never fetches market data and never touches the bot: the bot does not read these files.

Files of the results folder:
- `current.json`: the latest run (id, start, child pid, strategy, version, sessions);
- `<run_id>/`: the backtest's results (report.json ...), `log.txt` (its output) and
  `exit.json` (finish time and exit code, written when the child is waited for).
A run is `running` while its child is alive; `done` once report.json exists; `failed`
otherwise, with the last line the backtest printed (`backtest refused: ...`) as the cause.
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
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from tickr.api import contract as api
from tickr.backtest.universe import POOL_FILE
from tickr.calendar import CalendarCause, CalendarRefused, SessionCalendar
from tickr.config import Config
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
    modules: tuple[str, ...]  # [observers] strategy_modules: name=module

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
            active=cfg.strategy.active, modules=tuple(cfg.observers.strategy_modules))


Spawn = Callable[..., Any]  # subprocess.Popen, or a fake in tests


class LabReplay:
    """One replay at a time, shared by both listeners of the API."""

    def __init__(self, settings: LabReplaySettings, clock: Callable[[], datetime],
                 spawn: Spawn = subprocess.Popen) -> None:
        self.settings = settings
        self.clock = clock
        self.spawn = spawn
        self._lock = threading.Lock()

    # ------------------------------------------------------------ reading

    def state(self) -> api.LabReplayV1:
        current = self._current()
        if current is None:
            return api.LabReplayV1(api.LabReplayState.IDLE, None, None, None,
                                   self.settings.sessions, None, None, None, None)
        folder = self.settings.results / current["run_id"]
        started = api.parse_time(current["started_at"])
        finished: datetime | None = None
        code: int | None = None
        exit_path = folder / EXIT
        if exit_path.exists():
            done = json.loads(exit_path.read_text("utf-8"))
            finished, code = api.parse_time(done["finished_at"]), done["code"]
        elif _alive(current["pid"]):
            return self._doc(current, api.LabReplayState.RUNNING, started, None, None, None)
        report = folder / REPORT
        if (code is None or code == 0) and report.exists():
            if finished is None:
                finished = datetime.fromtimestamp(report.stat().st_mtime).astimezone()
            try:
                summary = summarize(json.loads(report.read_text("utf-8")))
            except (KeyError, ValueError, TypeError, api.ContractError) as exc:
                return self._doc(current, api.LabReplayState.FAILED, started, finished,
                                 f"report.json unreadable ({type(exc).__name__})", None)
            return self._doc(current, api.LabReplayState.DONE, started, finished, None, summary)
        return self._doc(current, api.LabReplayState.FAILED, started, finished,
                         self._cause(folder), None)

    def _doc(self, current: dict[str, Any], state: api.LabReplayState, started: datetime,
             finished: datetime | None, cause: str | None,
             summary: api.LabReplaySummaryV1 | None) -> api.LabReplayV1:
        return api.LabReplayV1(state, current["run_id"], current["strategy"],
                               current["version"], current["sessions"], started, finished,
                               cause, summary)

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
        return self._relative(refused[-1].removeprefix(REFUSED_PREFIX))

    def _relative(self, text: str) -> str:
        """`text` with the repo root taken out of its paths: no home folder leaves the Mac."""
        for root in {str(self.settings.root), str(self.settings.root.resolve())}:
            text = text.replace(root + os.sep, "")
        return text

    # ------------------------------------------------------------ starting

    def start(self) -> api.LabReplayV1:
        """Start a replay now, or raise LabReplayRefused with the cause."""
        s = self.settings
        with self._lock:
            if self.state().state is api.LabReplayState.RUNNING:
                raise LabReplayRefused("a replay is already running")
            now = self.clock()
            if s.outside_session_only:
                self._refuse_in_session(now)
            if s.config_path is None:
                raise LabReplayRefused(NOT_STARTED)
            if not (s.data / POOL_FILE).exists():
                raise LabReplayRefused(f"no fetched backtest data: {POOL_FILE} is missing in "
                                       f"[api] lab_replay_data_dir")
            try:
                name, version = read_identity(s.root, s.active)
                module = parse_modules(s.modules).get(name)
            except ObserverError as exc:
                raise LabReplayRefused(self._relative(str(exc))) from exc
            if module is None:
                raise LabReplayRefused(f"no module for strategy {name!r} in "
                                       "[observers] strategy_modules")
            run_id = uuid.uuid4().hex
            folder = s.results / run_id
            s.results.mkdir(parents=True, exist_ok=True)
            for old in s.results.iterdir():  # only the latest run is kept
                if old.is_dir() and not old.is_symlink() and RUN_ID.fullmatch(old.name):
                    shutil.rmtree(old)
            folder.mkdir()
            command = ["nice", "-n", str(s.niceness), sys.executable, "-m", "tickr.backtest",
                       "--config", str(s.config_path), "run", "--out", str(s.data),
                       "--results", str(folder), "--strategy", module,
                       "--values", str(s.root / STRATEGIES_DIR / s.active / STRATEGY_FILE),
                       "--last-sessions", str(s.sessions)]
            with open(folder / LOG, "w", encoding="utf-8") as log:
                child = self.spawn(command, cwd=s.root, stdout=log, stderr=subprocess.STDOUT,
                                   stdin=subprocess.DEVNULL, start_new_session=True)
            current = {"run_id": run_id, "started_at": api.format_time(now), "pid": child.pid,
                       "strategy": name, "version": version, "sessions": s.sessions}
            _write(s.results / CURRENT, current)
            threading.Thread(target=self._wait, args=(child, folder), daemon=True).start()
            return self._doc(current, api.LabReplayState.RUNNING, now, None, None, None)

    def _refuse_in_session(self, now: datetime) -> None:
        try:
            day = SessionCalendar.load(self.settings.calendar).day_at(now)
        except CalendarRefused as exc:
            if exc.cause in NO_SESSION:
                return
            raise LabReplayRefused(f"calendar: {exc}") from exc
        if day.in_regular_session(now):
            raise LabReplayRefused("refused during the regular session "
                                   "([api] lab_replay_outside_session_only)")

    def _wait(self, child: Any, folder: Path) -> None:
        code = child.wait()
        _write(folder / EXIT, {"finished_at": api.format_time(self.clock()), "code": code})


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
