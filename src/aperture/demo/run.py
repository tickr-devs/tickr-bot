"""`aperture demo`: fill a separate folder with a made-up trading day and serve it to the apps.

The real bot (`aperture run`, cli.main) runs on a simulated exchange (`aperture.demo.world`)
instead of Webull: the broker, the market data, the clock, `sleep` and the notifier are
replaced, everything else is the code the owner runs. So the database, the status and account files,
the capture and decision files are exactly what the bot writes on a real day.

Steps:
1. `<root_dir>` (default `demo/`) is rebuilt: a copy of config/ and strategies/ whose bot.toml
   runs as environment `[demo] environment`, with the demo's API port and observers. Its
   data_dir lands inside that folder, never in the real one.
2. Each past session runs from `bot_start` to `past_stop` on a fast clock.
3. The demo day (today if it is a session, else the latest session before it) runs fast up to
   `clock`, then in real time with the local API on `[demo] api_port`, until Ctrl-C.
"""

from __future__ import annotations

import os
import re
import shutil
import signal
import sys
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

from aperture import cli
from aperture import config as config_module
from aperture.api import contract as api
from aperture.api.server import ApiServer, make_server, settings_from_config
from aperture.calendar import CalendarRefused, SessionCalendar, ny_date_of
from aperture.demo.scenario import DemoCommand, Scenario, SymbolPlan
from aperture.demo.world import World, day_bars, flat_bars, ny, textured

MARKER = ".aperture-demo"  # marks a folder this command built and may delete
COPIED = ("config", "strategies")
COMMAND_SOURCE = "demo"
ONE_DAY = timedelta(days=1)
DEMO_DATA_DIR = "data"  # inside the demo folder, whatever the real bot.toml says
# Broker hosts and credential variable names in the demo copy point nowhere (RFC 2606 .invalid).
HOST_LINE = re.compile(r'^(\w+_host) = ".*"$', re.MULTILINE)
VARIABLE_LINE = re.compile(r'^(\w+_variable) = ".*"$', re.MULTILINE)
UNREACHABLE_HOST = "demo.invalid"
UNSET_VARIABLE_PREFIX = "APERTURE_DEMO_NEVER_SET_"


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


def build_folder(repo: Path, scenario: Scenario) -> Path:
    """(Re)build the demo folder and return the path of its bot.toml."""
    base = config_module.load(repo / "config" / "bot.toml")
    root = (repo / scenario.root_dir).resolve()
    if root == repo.resolve() or repo.resolve().is_relative_to(root):
        raise DemoRefused(f"[demo] root_dir {scenario.root_dir!r} would replace the repo")
    if root == (base.root / base.run.data_dir).resolve():
        raise DemoRefused("[demo] root_dir is the real data_dir")
    if root.exists():
        if not (root / MARKER).is_file():
            raise DemoRefused(f"{root} exists and was not made by `aperture demo`; not touching it")
        shutil.rmtree(root)
    root.mkdir(parents=True)
    (root / MARKER).write_text("built by `aperture demo`; deleted and rebuilt by it\n", "utf-8")
    for part in COPIED:
        shutil.copytree(repo / part, root / part)
    path = root / "config" / "bot.toml"
    text = path.read_text("utf-8")
    table = _environment_table(text, _open_environment(base))
    text = _set_line(text, "environment", f'"{scenario.environment}"')
    text = _set_line(text, "port", str(scenario.api_port))
    text = _set_line(text, "observers", "[" + ", ".join(f'"{o}"' for o in scenario.observers)
                     + "]")
    text = _set_line(text, "data_dir", f'"{DEMO_DATA_DIR}"')
    renamed = table.split("\n", 1)
    text += f"\n# Copy for `aperture demo`.\n[environments.{scenario.environment}]\n{renamed[1]}"
    path.write_text(_unreachable(text), "utf-8")
    cfg = config_module.load(path)  # refuse here, not half way through a day
    if not (cfg.root / cfg.run.data_dir).resolve().is_relative_to(root):
        raise DemoRefused("the demo data_dir is outside the demo folder")
    return path


def _unreachable(text: str) -> str:
    """Every broker host and credential variable name in the copy points nowhere, so even
    `aperture --config demo/config/bot.toml run` outside the demo can never reach an account."""
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

    def world(self, plans: tuple[SymbolPlan, ...]) -> World:
        s = self.scenario
        market_day = self.calendar.day(self.day)
        world = World(ny(self.day, s.bot_start), market_day, s.half_spread, s.quote_size,
                      s.provider)
        for plan in plans:
            history = [b for d in self.history_days for b in textured(flat_bars(
                plan.symbol, self.calendar.day(d), plan.base, s.history_volume), s.texture)]
            today = day_bars(plan.symbol, market_day, plan.base, plan.volume, plan.legs, s.wick)
            world.add_symbol(plan.symbol, textured(today, s.texture), history, (plan.screener,))
        return world

    def run(self, world: World) -> int:
        """`aperture run` on `world` until a stop signal (scheduled or Ctrl-C)."""
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
              server: ApiServer | None, log: Callable[[str], None]) -> int:
    """Run the demo day: fast up to `clock`, then live with the API (or, without a server,
    stop there)."""
    scenario = session.scenario
    inbox = cfg.root / cfg.run.data_dir / cfg.api.commands_dir / api.INBOX
    for command in scenario.commands:
        world.at(ny(session.day, command.at), _command_writer(inbox, world, command))
    for fault in scenario.faults:  # one kind so far: STOP_UNVERIFIED, checked by the loader
        world.at(ny(session.day, fault.at),
                 lambda symbol=fault.symbol: world.exchange.unverify_stops(symbol))

    if server is None:
        world.at(ny(session.day, scenario.clock), _stop_self)
    else:
        live = server

        def go_live() -> None:
            world.go_live()
            live.clock = world.clock
            live.chart.use(world.feed)  # GET /chart serves the simulated world
            threading.Thread(target=live.serve_forever, daemon=True).start()
            log(f"demo: live on http://{live.settings.host}:{live.server_port}/ "
                f"(New York {scenario.clock} on {session.day.isoformat()}); Ctrl-C stops it")

        world.at(ny(session.day, scenario.clock), go_live)
    log(f"demo: session {session.day.isoformat()} (the demo day)")
    try:
        return session.run(world)
    finally:
        if server is not None:
            server.shutdown()


def run_demo(repo: Path, scenario: Scenario, today: date, *, live: bool = True,
             log: Callable[[str], None] = print) -> int:
    """Build the folder, run the past sessions, then the demo day. With `live` False the demo
    day stops at `clock` instead of going on in real time (tests)."""
    config_path = build_folder(repo, scenario)
    cfg = config_module.load(config_path)
    calendar = SessionCalendar.load((cfg.root / cfg.session.calendar_file).parent)
    history_count = cfg.market_data.history_sessions
    days = sessions(calendar, today, len(scenario.past) + 1 + history_count)
    server = None
    if live:  # bound now, so a busy port is refused before any session runs
        try:
            server = make_server(settings_from_config(cfg))
        except OSError as exc:
            raise DemoRefused(f"cannot listen on port {scenario.api_port}: {exc}") from exc
    try:
        for n, plans in enumerate((*scenario.past, scenario.today)):
            index = history_count + n
            session = Session(config_path, scenario, calendar, days[index],
                              days[index - history_count:index])
            world = session.world(plans)
            if n == len(scenario.past):
                return _demo_day(session, world, cfg, server, log)
            log(f"demo: session {session.day.isoformat()} (past)")
            world.at(ny(session.day, scenario.past_stop), _stop_self)
            code = session.run(world)
            if code != 0:
                raise DemoRefused(f"the bot ended {session.day} with exit code {code}")
    finally:
        if server is not None:
            server.server_close()
    raise AssertionError("unreachable: the loop ends on the demo day")


def main(repo: Path, scenario_path: Path) -> int:
    from aperture.demo.scenario import ScenarioError, load

    try:
        scenario = load(scenario_path)
        return run_demo(repo, scenario, ny_date_of(cli.NOW()))
    except (ScenarioError, DemoRefused, config_module.ConfigError, OSError) as exc:
        print(f"demo refused: {exc}", file=sys.stderr)
        return 2
