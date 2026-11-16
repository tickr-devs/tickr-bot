"""`tickr demo`: fill a separate folder with a made-up trading day and serve it to the apps.

The real bot (`tickr run`, cli.main) runs on a simulated exchange (`tickr.demo.world`)
instead of Webull: the broker, the market data, the clock, `sleep` and the notifier are
replaced, everything else is the code the owner runs. So the database, the status and account files,
the capture and decision files are exactly what the bot writes on a real day.

Steps:
1. `<root_dir>` (default `demo/`) is rebuilt: a copy of config/ and strategies/ whose bot.toml
   runs as environment `[demo] environment`, with the demo's API port and observers. Its
   data_dir lands inside that folder, never in the real one.
2. Each past session runs from `bot_start` to `past_stop` on a fast clock.
3. The demo day (today if it is a session, else the latest session before it) runs fast up to
   `clock`, then in real time with the local API on bot.toml `[api] demo_port`, until
   `live_until` (then it exits 0, and the launchd job starts a fresh one) or Ctrl-C.
"""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import os
import re
import shutil
import signal
import sys
import tarfile
import threading
from collections.abc import Callable, Iterator
from dataclasses import dataclass, replace
from datetime import date, time, timedelta
from pathlib import Path

from tickr import cli
from tickr import config as config_module
from tickr.api import contract as api
from tickr.api.server import make_server, settings_from_config
from tickr.calendar import CalendarRefused, SessionCalendar, ny_date_of
from tickr.demo.scenario import DemoCommand, Scenario, SymbolPlan
from tickr.demo.world import (
    World,
    daily_history,
    day_bars,
    flat_bars,
    ny,
    session_minutes,
    textured,
    weekdays_before,
)
from tickr.store import DATABASE_FILE, MIGRATIONS, Store

MARKER = ".tickr-demo"  # marks a folder this command built and may delete
COPIED = ("config", "strategies")
COMMAND_SOURCE = "demo"
ONE_DAY = timedelta(days=1)
DEMO_DATA_DIR = "data"  # inside the demo folder, whatever the real bot.toml says
# Broker hosts and credential variable names in the demo copy point nowhere (RFC 2606 .invalid).
HOST_LINE = re.compile(r'^(\w+_host) = ".*"$', re.MULTILINE)
VARIABLE_LINE = re.compile(r'^(\w+_variable) = ".*"$', re.MULTILINE)
UNREACHABLE_HOST = "demo.invalid"
UNSET_VARIABLE_PREFIX = "TICKR_DEMO_NEVER_SET_"


class DemoRefused(Exception):
    """The demo cannot be built; the message is the single cause."""


class Notifier:
    """Alerts of the demo bot go to the terminal, never to Notification Center or iMessage."""

    def notify(self, title: str, text: str) -> None:
        print(f"demo alert: {title}: {text}", file=sys.stderr, flush=True)


# --- the demo folder ---------------------------------------------------------------------------


def _set_line(text: str, key: str, value: str) -> str:
    pattern = re.compile(rf"^{re.escape(key)} = .*$", re.MULTILINE)
    if len(pattern.findall(text)) != 1:
        raise DemoRefused(f"bot.toml does not have exactly one `{key} =` line")
    return pattern.sub(lambda _: f"{key} = {value}", text)


def _environment_table(text: str, name: str) -> str:
    header = f"[environments.{name}]"
    start = text.find(header)
    if start < 0:
        raise DemoRefused(f"bot.toml has no {header}")
    following = re.search(r"^\[", text[start + len(header):], re.MULTILINE)
    end = len(text) if following is None else start + len(header) + following.start()
    return text[start:end].rstrip() + "\n"


def _open_environment(cfg: config_module.Config) -> str:
    """The first environment whose gate is open by configuration alone (no owner file)."""
    for name, env in cfg.environments.items():
        if env.trading_enabled and not env.requires_owner_authorization:
            return name
    raise DemoRefused("no environment trades without an owner authorization to copy")


def demo_root(repo: Path, scenario: Scenario) -> Path:
    return (repo / scenario.root_dir).resolve()


@contextlib.contextmanager
def running_lock(repo: Path, scenario: Scenario) -> Iterator[None]:
    """Held for the whole demo, taken before the folder is wiped: a second `tickr demo` on
    the same folder is refused while the first runs, and leaves its folder alone. The lock file
    sits beside the folder (the folder itself is deleted and rebuilt); the OS drops the lock
    when the process ends, however it ends."""
    root = demo_root(repo, scenario)
    path = root.with_name(root.name + ".lock")
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise DemoRefused(f"another `tickr demo` is running on {root}; "
                              "stop it first (its folder is left as it is)") from exc
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def build_folder(repo: Path, scenario: Scenario) -> Path:
    """(Re)build the demo folder and return the path of its bot.toml."""
    base = config_module.load(repo / "config" / "bot.toml")
    root = demo_root(repo, scenario)
    if root == repo.resolve() or repo.resolve().is_relative_to(root):
        raise DemoRefused(f"[demo] root_dir {scenario.root_dir!r} would replace the repo")
    if base.api.demo_port == 0:
        raise DemoRefused("bot.toml [api] demo_port is 0: the demo has no port to serve on")
    if root == (base.root / base.run.data_dir).resolve():
        raise DemoRefused("[demo] root_dir is the real data_dir")
    if root.exists():
        if not (root / MARKER).is_file():
            raise DemoRefused(f"{root} exists and was not made by `tickr demo`; not touching it")
        shutil.rmtree(root)
    root.mkdir(parents=True)
    (root / MARKER).write_text("built by `tickr demo`; deleted and rebuilt by it\n", "utf-8")
    for part in COPIED:
        shutil.copytree(repo / part, root / part)
    path = root / "config" / "bot.toml"
    text = path.read_text("utf-8")
    table = _environment_table(text, _open_environment(base))
    text = _set_line(text, "environment", f'"{scenario.environment}"')
    text = _set_line(text, "port", str(base.api.demo_port))
    text = _set_line(text, "demo_port", "0")  # the demo's own API forwards nowhere
    text = _set_line(text, "observers", "[" + ", ".join(f'"{o}"' for o in scenario.observers)
                     + "]")
    text = _set_line(text, "data_dir", f'"{DEMO_DATA_DIR}"')
    renamed = table.split("\n", 1)
    text += f"\n# Copy for `tickr demo`.\n[environments.{scenario.environment}]\n{renamed[1]}"
    path.write_text(_unreachable(text), "utf-8")
    cfg = config_module.load(path)  # refuse here, not half way through a day
    if not (cfg.root / cfg.run.data_dir).resolve().is_relative_to(root):
        raise DemoRefused("the demo data_dir is outside the demo folder")
    return path


def _unreachable(text: str) -> str:
    """Every broker host and credential variable name in the copy points nowhere, so even
    `tickr --config demo/config/bot.toml run` outside the demo can never reach an account."""
    text = HOST_LINE.sub(lambda m: f'{m.group(1)} = "{UNREACHABLE_HOST}"', text)
    return VARIABLE_LINE.sub(
        lambda m: f'{m.group(1)} = "{UNSET_VARIABLE_PREFIX}{m.group(1).upper()}"', text)


# --- running sessions --------------------------------------------------------------------------


def sessions(calendar: SessionCalendar, today: date, count: int) -> list[date]:
    """The `count` latest trading sessions on or before `today`, oldest first."""
    found: list[date] = []
    day = today
    while len(found) < count:
        try:
            calendar.day(day)
            found.append(day)
        except CalendarRefused as exc:
            if exc.cause not in (exc.cause.WEEKEND, exc.cause.HOLIDAY):
                raise DemoRefused(f"calendar: {exc}") from exc
        day -= ONE_DAY
    return list(reversed(found))


@dataclass
class Session:
    """One simulated session of the real bot."""

    config_path: Path
    scenario: Scenario
    calendar: SessionCalendar
    day: date
    history_days: list[date]
    chart: bool = False  # the demo day: D charts get [market] chart_days older days

    def world(self, plans: tuple[SymbolPlan, ...],
              listed: tuple[tuple[str, str], ...] = (), start: str | None = None) -> World:
        """The simulated exchange for `plans` from New York time `start` (default
        `bot_start`); `listed` = (symbol, New York time) of symbols the screeners list only
        from that time."""
        s = self.scenario
        listed_from = {symbol: ny(self.day, at) for symbol, at in listed}
        market_day = self.calendar.day(self.day)
        world = World(ny(self.day, start or s.bot_start), market_day, s.half_spread, s.quote_size,
                      s.provider)
        for plan in plans:
            history = [b for d in self.history_days for b in textured(flat_bars(
                plan.symbol, self.calendar.day(d), plan.base, s.history_volume), s.texture)]
            today = day_bars(plan.symbol, market_day, plan.base, plan.volume, plan.legs, s.wick)
            daily = []
            if self.chart:
                first = self.history_days[0] if self.history_days else self.day
                daily = daily_history(plan.symbol, weekdays_before(first, s.chart_days),
                                      plan.base, s.history_volume * session_minutes(market_day),
                                      s.chart_walk)
            world.add_symbol(plan.symbol, textured(today, s.texture), history, (plan.screener,),
                             daily, listed_from.get(plan.symbol))
        return world

    def run(self, world: World) -> int:
        """`tickr run` on `world` until a stop signal (scheduled or Ctrl-C)."""
        def stream_start() -> None:
            world.stream_up = True
            world.feed.transport(True, world.now, "connected")

        def stream_stop() -> None:
            world.stream_up = False

        saved = (cli.RUN_ADAPTERS, cli.NOW, cli.SLEEP, cli.MONOTONIC, cli.NOTIFIER)
        cli.RUN_ADAPTERS = lambda cfg, on_error, on_token: cli.RunAdapters(
            world.exchange, world.feed, stream_start, stream_stop)
        cli.NOW, cli.SLEEP, cli.MONOTONIC = world.clock, world.sleep, world.mono
        cli.NOTIFIER = lambda alerts: Notifier()
        try:
            return cli.main(["--config", str(self.config_path), "run"])
        finally:
            cli.RUN_ADAPTERS, cli.NOW, cli.SLEEP, cli.MONOTONIC, cli.NOTIFIER = saved


def _stop_self() -> None:
    os.kill(os.getpid(), signal.SIGTERM)


def _command_writer(inbox: Path, world: World, command: DemoCommand) -> Callable[[], None]:
    def write() -> None:
        api.write_command(inbox, api.new_command(command.kind, world.now, COMMAND_SOURCE,
                                                 command.symbol))
    return write


def _demo_day(session: Session, world: World, cfg: config_module.Config,
              log: Callable[[str], None]) -> int:
    """Replay the demo day fast from `bot_start` up to `clock`, then stop the bot there."""
    scenario = session.scenario
    _schedule(session, world, cfg, after=None)
    world.at(ny(session.day, scenario.clock), _stop_self)
    log(f"demo: session {session.day.isoformat()} (the demo day, up to {scenario.clock})")
    return session.run(world)


def _schedule(session: Session, world: World, cfg: config_module.Config,
              after: str | None) -> None:
    """Queue the scenario's operator commands and faults (only those after `after`)."""
    scenario = session.scenario
    inbox = cfg.root / cfg.run.data_dir / cfg.api.commands_dir / api.INBOX
    later = (lambda at: True) if after is None else (
        lambda at: time.fromisoformat(at) > time.fromisoformat(after))
    for command in scenario.commands:
        if later(command.at):
            world.at(ny(session.day, command.at), _command_writer(inbox, world, command))
    for fault in scenario.faults:  # one kind so far: STOP_UNVERIFIED, checked by the loader
        if later(fault.at):
            world.at(ny(session.day, fault.at),
                     lambda symbol=fault.symbol: world.exchange.unverify_stops(symbol))


def replay(repo: Path, scenario: Scenario, today: date,
           log: Callable[[str], None]) -> tuple[config_module.Config, date, int]:
    """Build the folder, run the past sessions, then the demo day fast up to `clock` (the bot
    stopped there). Returns the demo's configuration, the demo day and the bot's exit code on
    it. Caller holds the lock."""
    config_path = build_folder(repo, scenario)
    cfg = config_module.load(config_path)
    calendar = SessionCalendar.load((cfg.root / cfg.session.calendar_file).parent)
    history_count = cfg.market_data.history_sessions
    days = sessions(calendar, today, len(scenario.past) + 1 + history_count)
    for n, plans in enumerate((*scenario.past, scenario.today)):
        index = history_count + n
        demo_day = n == len(scenario.past)
        session = Session(config_path, scenario, calendar, days[index],
                          days[index - history_count:index], demo_day)
        world = session.world(plans, scenario.listed if demo_day else ())
        if demo_day:
            return cfg, session.day, _demo_day(session, world, cfg, log)
        log(f"demo: session {session.day.isoformat()} (past)")
        world.at(ny(session.day, scenario.past_stop), _stop_self)
        code = session.run(world)
        if code != 0:
            raise DemoRefused(f"the bot ended {session.day} with exit code {code}")
    raise AssertionError("unreachable: the loop ends on the demo day")


# --- the fixture: the replayed day, committed, so a start replays nothing -----------------------

FIXTURE_DIR = "demo-fixture"  # in the repo, beside config/
FIXTURE_DATA = "data.tar.xz"
FIXTURE_MANIFEST = "manifest.json"
# Left out of the fixture: regenerated by the bot from the simulated feed (prior-session bars),
# per process (lock), or not needed by the apps (backups).
FIXTURE_SKIP = ("history", "backups", "tickr.lock")
# What the fixture's data FORMAT depends on: the demo itself and the API contract the apps
# read (in the tickr package), and the strategies (in the repo) whose names and versions its
# records carry; the scenario (config/demo.toml) goes in through `fixture_inputs`. The store
# schema is handled by its forward migrations at start. Engine, risk or runtime changes do not
# make it stale (owner decision 2026-11-15).
FIXTURE_CODE = ("demo", "api/contract.py")
FIXTURE_REPO_DIRS = ("strategies",)
FIXTURE_REBUILD = "rebuild it with `tickr demo --build-fixture` (docs/demo.md)"


def fixture_inputs(repo: Path, scenario: Scenario) -> str:
    """Digest of what the fixture's records depend on for the apps (see FIXTURE_*): a change
    makes it stale and `tickr demo` refuses to start until it is rebuilt."""
    digest = hashlib.sha256(repr(scenario).encode())
    package = Path(cli.__file__).resolve().parent
    named: list[tuple[str, Path]] = []
    for entry in FIXTURE_CODE:
        path = package / entry
        files = [path] if path.is_file() else sorted(path.rglob("*.py"))
        named += [(f"tickr/{f.relative_to(package).as_posix()}", f) for f in files]
    for folder in FIXTURE_REPO_DIRS:
        named += [(f.relative_to(repo).as_posix(), f) for f in sorted((repo / folder).rglob("*"))
                  if f.is_file() and "__pycache__" not in f.parts]
    for name, path in named:
        digest.update(name.encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()


def _tar_filter(info: tarfile.TarInfo) -> tarfile.TarInfo | None:
    parts = Path(info.name).parts
    if len(parts) > 1 and parts[1] in FIXTURE_SKIP:
        return None
    info.mtime, info.uid, info.gid, info.uname, info.gname = 0, 0, 0, "", ""
    return info


def build_fixture(repo: Path, scenario: Scenario, today: date,
                  log: Callable[[str], None] = print) -> Path:
    """Replay the past sessions and the demo day up to `clock` with the real bot and store
    the result in `demo-fixture/` with its manifest. Refused unless the bot ends flat."""
    with running_lock(repo, scenario):
        cfg, day, code = replay(repo, scenario, today, log)
        if code != 0:
            raise DemoRefused(f"the bot ended the demo day with exit code {code}")
        status = api.read_status(cfg.root / cfg.run.data_dir / cfg.api.status_file)
        if status.positions:
            raise DemoRefused(f"the demo day is not flat at {scenario.clock} "
                              f"({[p.symbol for p in status.positions]}): the live part "
                              "starts a fresh simulated exchange")
        out = repo / FIXTURE_DIR
        out.mkdir(exist_ok=True)
        data = cfg.root / cfg.run.data_dir
        archive = out / FIXTURE_DATA
        with tarfile.open(archive, "w:xz") as tar:
            tar.add(data, arcname=DEMO_DATA_DIR, filter=_tar_filter)
        manifest = {"inputs": fixture_inputs(repo, scenario), "demo_day": day.isoformat(),
                    "clock": scenario.clock, "schema": len(MIGRATIONS),
                    "built_by": "tickr demo --build-fixture"}
        (out / FIXTURE_MANIFEST).write_text(json.dumps(manifest, indent=2) + "\n", "utf-8")
        log(f"demo: fixture {archive} ({archive.stat().st_size} bytes) for {day.isoformat()}")
        return archive


def read_fixture(repo: Path, scenario: Scenario) -> dict[str, object]:
    """The manifest, refused when the fixture is missing or stale."""
    path = repo / FIXTURE_DIR / FIXTURE_MANIFEST
    try:
        manifest = json.loads(path.read_text("utf-8"))
    except (OSError, ValueError) as exc:
        raise DemoRefused(f"no demo fixture at {path}: {FIXTURE_REBUILD}") from exc
    if manifest.get("inputs") != fixture_inputs(repo, scenario):
        raise DemoRefused(f"the demo fixture is stale (the scenario, the demo code, the API "
                          f"contract or a strategy changed): {FIXTURE_REBUILD}")
    if not isinstance(manifest.get("schema"), int) or manifest["schema"] > len(MIGRATIONS):
        raise DemoRefused(f"the demo fixture's database is newer than this code: "
                          f"{FIXTURE_REBUILD}")
    if manifest.get("clock") != scenario.clock:
        raise DemoRefused(f"the demo fixture ends at {manifest.get('clock')}, not at "
                          f"[demo] clock {scenario.clock}: {FIXTURE_REBUILD}")
    return manifest


def run_demo(repo: Path, scenario: Scenario, today: date, *, live: bool = True,
             log: Callable[[str], None] = print) -> int:
    """With `live`: start from the committed fixture (the day up to `clock`, replayed by
    `build_fixture`), serve the API at once on the demo's clock and run the real bot in real
    time from `clock` with the live loop until `live_until`. Without `live`: replay the past
    sessions and the demo day fast up to `clock` (tests, and what the fixture holds). Refused
    before anything is touched while another demo runs on the same folder."""
    with running_lock(repo, scenario):
        if not live:
            return replay(repo, scenario, today, log)[2]
        return _start_live(repo, scenario, log)


def _start_live(repo: Path, scenario: Scenario, log: Callable[[str], None]) -> int:
    manifest = read_fixture(repo, scenario)
    config_path = build_folder(repo, scenario)
    cfg = config_module.load(config_path)
    day = date.fromisoformat(str(manifest["demo_day"]))
    calendar = SessionCalendar.load((cfg.root / cfg.session.calendar_file).parent)
    days = sessions(calendar, day, 1 + cfg.market_data.history_sessions)
    session = Session(config_path, scenario, calendar, day, days[:-1], True)
    world = session.world(scenario.today, scenario.listed, start=scenario.clock)
    try:  # bound first: requests queue on the port and are answered in a second or two
        # GET /config shows the demo's copy; the Lab replay stays off as before.
        server = make_server(replace(settings_from_config(cfg), config_file=config_path),
                             world.clock)
    except OSError as exc:
        raise DemoRefused(f"cannot listen on port {cfg.api.port}: {exc}") from exc
    serving: threading.Thread | None = None
    try:
        data = cfg.root / cfg.run.data_dir
        with tarfile.open(repo / FIXTURE_DIR / FIXTURE_DATA, "r:xz") as tar:
            tar.extractall(data.parent, filter="data")
        # The normal forward migrations, as for real data, before anything reads it.
        Store.open(data / DATABASE_FILE,
                   busy_timeout_seconds=cfg.run.database_busy_timeout_seconds).close()
        _schedule(session, world, cfg, after=scenario.clock)
        world.at(ny(day, scenario.live_until), _stop_self)
        server.chart.use(world.feed)  # GET /chart serves the simulated world
        serving = threading.Thread(target=server.serve_forever, daemon=True)
        serving.start()
        world.go_live()
        log(f"demo: serving on http://{server.settings.host}:{server.server_port}/ , live "
            f"from New York {scenario.clock} on {day.isoformat()} until {scenario.live_until}"
            "; Ctrl-C stops it")
        return session.run(world)
    finally:
        if serving is not None:
            server.shutdown()
        server.server_close()


def main(repo: Path, scenario_path: Path, *, build: bool = False) -> int:
    from tickr.demo.scenario import ScenarioError, load

    # Flushed at once: under launchd stdout is a file, and a buffered line would show up only
    # when the process ends (or in the next demo's log), never while it runs.
    def log(line: str) -> None:
        print(line, flush=True)

    try:
        scenario = load(scenario_path)
        if build:
            build_fixture(repo, scenario, ny_date_of(cli.NOW()), log)
            return 0
        return run_demo(repo, scenario, ny_date_of(cli.NOW()), log=log)
    except (ScenarioError, DemoRefused, config_module.ConfigError, OSError) as exc:
        print(f"demo refused: {exc}", file=sys.stderr)
        return 2
