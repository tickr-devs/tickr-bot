"""`python -m tickr.runtime <command>`: the commands the launchd jobs run.

- `preflight`: run the checks, print the result as one JSON line, alert on failure. The last
  check makes the Webull token usable and alerts token-needs-confirmation while it waits.
- `run-bot -- <bot command...>`: preflight again (a failure alerts "bot did not start"), then
  run the bot under supervision; alert if it crashes. Never relaunches.
- `watchdog`: the separate end-of-day watchdog (runtime/watchdog.py): read-only broker checks
  after the flatten time, "bot down with an open trade" during the session, and near the
  flatten time one start a day of a down bot job so the bot flattens (it never sends orders).
  Exit 0 all clear, 1 something alerted, 2 config invalid, 3 no session today.
- `broker-flat`: one read-only broker read of positions and open orders (JSON), the proof
  `release` and `rollback` require just before they switch.
- `require-flat`: `broker-flat` plus the database's open trades; exit 0 only when flat (the
  server deploy's gate, ops/systemd/deploy-server.sh).
- `alert <kind> <text>`: show one alert (used to test notifications on the Mac).
- `render-launchd`: write filled-in plists from ops/launchd into a folder; installs nothing.
- `render-systemd`: write the systemd units and timers from ops/systemd (Linux server); installs
  nothing.
- `request-restart --cause <word>`: the server's watchdog restart command: write the restart
  request file the host's tickr-bot-restart.path watches (runtime/restartrequest.py).
- `release [--commit REF]` / `rollback`: switch what the launchd jobs run to a pinned release
  (runtime/release.py); refused unless the bot is flat.

Output lines are JSON objects with a "record" field; launchd writes them to the job's log file.
The bot process itself journals its alerts in the database; these commands run outside the bot
(single-writer rule), so their record is the log line.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import tomllib
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from tickr import config as config_module
from tickr.api import contract
from tickr.broker.base import Broker, BrokerError
from tickr.calendar import CalendarRefused
from tickr.runtime import launchd, restartrequest, systemd, watchdog
from tickr.runtime import release as release_module
from tickr.runtime.alert_routing import routing_path, skipped_by
from tickr.runtime.alerts import (
    Alerter,
    AlertKind,
    Routing,
    default_notifier,
    feed_path,
    recipient_missing,
    route_all,
    token_pending_text,
)
from tickr.runtime.preflight import (
    ExitCode,
    PreflightCause,
    PreflightJob,
    PreflightResult,
    TokenProbe,
    record_path,
    record_text,
    run_preflight,
)
from tickr.runtime.signals import StopRequest, install_stop_handler, restore_handlers
from tickr.runtime.supervisor import supervise
from tickr.store import DATABASE_FILE

TITLE = "Tickr"
TEMPLATE_DIR = Path("ops") / "launchd"
SYSTEMD_TEMPLATE_DIR = Path("ops") / "systemd"
# The server's file layout (docs/deploy-server.md): no clone; the folder of bot.toml,
# compose.yaml and ports.env, and the env file the secrets unit writes (a path only, never read
# here).
SERVER_ETC = Path("/srv/tickr/etc")
SERVER_COMPOSE = SERVER_ETC / "compose.yaml"
SERVER_ENV_FILE = Path("/run/tickr/env")
SERVER_DATA = Path("/srv/tickr/data")
SERVER_CONFIG = "bot.toml"
SERVER_USER = "tickr"
# The zone of the [runtime] times: the exchange's.
SERVER_ZONE = "America/New_York"


def _emit(record: str, body: dict[str, object]) -> None:
    print(json.dumps({"record": record, **body}, sort_keys=True), flush=True)


def _alerts_config(config_path: Path) -> config_module.AlertsConfig | None:
    """[alerts] even when the rest of the config is invalid; None if [alerts] itself is not."""
    try:
        data = tomllib.loads(config_path.read_text("utf-8"))
        return config_module.build_section("alerts", data.get("alerts"),
                                           config_module.AlertsConfig)
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError, config_module.ConfigError):
        return None


def _data_dir(config_path: Path) -> Path | None:
    """[run] data_dir even when the rest of the config is invalid; None when [run] itself is
    not readable."""
    try:
        data = tomllib.loads(config_path.read_text("utf-8"))
        run = config_module.build_section("run", data.get("run"), config_module.RunConfig)
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError, config_module.ConfigError):
        return None
    return config_path.resolve().parent.parent / run.data_dir


def _feed(config_path: Path, alerts: config_module.AlertsConfig) -> Path | None:
    """The alerts feed file even when the rest of the config is invalid; None when [run]
    itself is not readable (the Mac notification and iMessage still go out)."""
    data = _data_dir(config_path)
    return None if data is None else data / alerts.feed_file


def _routing(config_path: Path, alerts: config_module.AlertsConfig) -> Routing:
    """The owner's alert routing; every kind on every channel when [run] is not readable."""
    data = _data_dir(config_path)
    return route_all if data is None else skipped_by(data / alerts.routing_file)


def _alert(config_path: Path, environment: str | None, kind: AlertKind, text: str) -> None:
    alerts = _alerts_config(config_path)
    if alerts is None:
        _emit("alert", {"kind": kind.value, "text": text, "delivery": "no-alerts-config",
                        "error": None, "at": datetime.now(UTC).isoformat()})
        return
    title = f"{TITLE} {environment}" if environment else TITLE
    alerter = Alerter(alerts, title, default_notifier(alerts, feed=_feed(config_path, alerts)),
                      routing=_routing(config_path, alerts))
    _emit("alert", alerter.alert(kind, text).journal_body())
    for outcome in alerter.flush():  # background channels (iMessage) before this process exits
        _emit("alert", outcome.journal_body())


def _token_probe(config_path: Path) -> TokenProbe:
    """Preflight check 6 over the Webull adapter: alerts token-needs-confirmation when the SDK
    starts waiting for the owner; returns the cause when the token is not usable."""
    def probe(cfg: config_module.Config) -> str | None:
        wait = cfg.environment.token_check_duration_seconds

        def pending() -> None:
            _alert(config_path, cfg.environment_name, AlertKind.TOKEN_NEEDS_CONFIRMATION,
                   token_pending_text(wait))

        try:
            from tickr.broker.webull import check_token  # optional dependency
            check_token(cfg, pending)
        except BrokerError as exc:  # AdapterConfigError: names and causes only, no values
            return str(exc)
        except Exception as exc:  # noqa: BLE001 - no usable token; the type is the cause
            return type(exc).__name__
        return None
    return probe


# Replaced in tests: builds the preflight token probe for a config path.
TOKEN_PROBE = _token_probe


def _preflight(config_path: Path, failure: AlertKind, job: PreflightJob) -> PreflightResult:
    started = datetime.now(UTC)
    result = run_preflight(config_path, now=started, environ=os.environ,
                           token_probe=TOKEN_PROBE(config_path))
    _emit("preflight", result.journal_body())
    _record(result, job, started)
    if result.cause is not None:
        # A refused strategy alone does not stop the bot job: say "preflight failed", not
        # "bot did not start".
        kind = failure if result.blocks_start else AlertKind.PREFLIGHT_FAILED
        _alert(config_path, result.environment, kind, result.summary())
    return result


def _record(result: PreflightResult, job: PreflightJob, started: datetime) -> None:
    """The preflight's record for GET /system. Only with a loaded config (it names the data
    folder) that exists: the record never creates the data folder, whose absence is itself a
    preflight failure. It is for display only: any failure is printed and changes nothing else,
    the job goes on (a bot start never waits on it)."""
    cfg = result.config
    if cfg is None:
        return
    try:
        if not (cfg.root / cfg.run.data_dir).is_dir():
            return
        contract.write_atomic(record_path(cfg),
                              record_text(result, job, started, datetime.now(UTC)))
    except Exception as exc:  # noqa: BLE001 - a display record never stops the job
        _emit("preflight-record-failed", {"error": type(exc).__name__})


def _run_bot(config_path: Path, command: list[str]) -> int:
    if command[:1] == ["--"]:
        command = command[1:]
    if not command:
        print("run-bot: give the bot command after --", file=sys.stderr)
        return int(ExitCode.CONFIG_INVALID)
    result = _preflight(config_path, AlertKind.BOT_DID_NOT_START, PreflightJob.BOT_START)
    if result.blocks_start or result.config is None:
        return int(result.exit_code)
    stop = StopRequest()
    previous = install_stop_handler(stop)
    try:
        runtime = result.config.runtime

        def overdue() -> None:
            _alert(config_path, result.environment, AlertKind.BOT_DID_NOT_STOP,
                   f"bot still running {runtime.stop_alert_after_seconds} s after SIGTERM; "
                   f"launchd kills it at {runtime.bot_exit_timeout_seconds} s: check orders "
                   "and position")

        ended = supervise(command, poll_seconds=runtime.supervisor_poll_seconds, stop=stop,
                          stop_alert_after_seconds=Decimal(runtime.stop_alert_after_seconds),
                          on_stop_overdue=overdue)
    finally:
        restore_handlers(previous)
    _emit("bot-exit", {"return_code": ended.return_code, "stop_signal": stop.signal_name,
                       "clean": ended.clean})
    if not ended.clean:
        _alert(config_path, result.environment, AlertKind.BOT_CRASHED, ended.describe())
    if ended.clean:
        return 0
    return ended.return_code if ended.return_code > 0 else int(ExitCode.FAILED)


def _webull_broker(cfg: config_module.Config, on_token_pending: Callable[[], None]) -> Broker:
    from tickr.broker.webull import broker_from_config  # optional dependency
    return broker_from_config(cfg, on_token_pending=on_token_pending)


def _utc_now() -> datetime:
    return datetime.now(UTC)


# Replaced in tests: the watchdog's broker factory, clock, sleep and command runner (its one
# restart a day of the bot job). The watchdog only ever holds the broker through
# watchdog.ReadOnlyBroker.
WATCHDOG_BROKER: Callable[[config_module.Config, Callable[[], None]], Broker] = _webull_broker
WATCHDOG_NOW: Callable[[], datetime] = _utc_now
WATCHDOG_SLEEP: Callable[[float], None] = time.sleep
WATCHDOG_RUNNER: watchdog.Runner = watchdog.run_command
WATCHDOG_DISK_FREE: watchdog.DiskFree = watchdog.disk_free


ONE_DAY = timedelta(days=1)


def _hold_daily(again: Callable[[], None]) -> None:
    """Never ends: `again` once a day, at the hour of the first alert. That hour is when the
    owner deployed the config, so the repeat comes while the owner is awake, never at midnight; an
    invalid config has no time setting to trust."""
    while True:
        time.sleep(ONE_DAY.total_seconds())
        again()


# What the watchdog does after alerting an invalid config, instead of ending: the alert again
# once a day (replaced in tests).
WATCHDOG_HOLD: Callable[[Callable[[], None]], None] = _hold_daily
# New York days the watchdog runs before it returns; None (the job) = until it is stopped.
WATCHDOG_DAYS: int | None = None


def _watchdog(config_path: Path) -> int:
    from tickr.cli import data_paths

    cfg = config_module.load(config_path)
    launchd.check_timings(cfg)
    database, lock_path = data_paths(cfg)
    journal = watchdog.Journal(cfg.root / cfg.run.data_dir / cfg.runtime.watchdog_journal_file)
    notifier = default_notifier(cfg.alerts,
                                feed=feed_path(cfg.root, cfg.run.data_dir, cfg.alerts))
    alerter = Alerter(cfg.alerts, f"{TITLE} {cfg.environment_name}", notifier,
                      clock=WATCHDOG_NOW, routing=skipped_by(routing_path(cfg)))
    if notifier is None:
        # No channel on this machine (not macOS): every alert is journaled "no-notifier" only.
        # Recorded once at the start so the journal says why nobody was told; the exit code is
        # then never 0, so the host scheduler shows a failed job.
        journal.write(watchdog.CHANNEL_REFUSED, {"cause": PreflightCause.ALERT_CHANNEL_MISSING,
                                                 "platform": sys.platform})
    missing = recipient_missing(cfg.alerts, os.environ)
    if missing is not None:
        # Recorded, and every iMessage attempt fails with the same cause; the watchdog still
        # runs, because the Mac notification is the remaining channel.
        journal.write(watchdog.CHANNEL_REFUSED, {"cause": PreflightCause.ALERT_RECIPIENT_MISSING,
                                                 "variable": missing})

    def token_pending() -> None:
        sentinel.alert(AlertKind.TOKEN_NEEDS_CONFIRMATION,
                    token_pending_text(cfg.environment.token_check_duration_seconds))

    status_path = cfg.root / cfg.run.data_dir / cfg.api.status_file
    sentinel = watchdog.Sentinel(cfg, alerter=alerter, journal=journal, lock_path=lock_path,
                                 status_path=status_path, data_dir=cfg.root / cfg.run.data_dir,
                                 free=WATCHDOG_DISK_FREE)
    code = int(ExitCode.READY)
    days = 0
    while WATCHDOG_DAYS is None or days < WATCHDOG_DAYS:
        days += 1
        code = _watchdog_day(cfg, sentinel, alerter, journal, database, lock_path,
                             status_path, token_pending)
    if notifier is None and code == ExitCode.READY:
        return int(ExitCode.FAILED)
    return code


def _watchdog_day(cfg: config_module.Config, sentinel: watchdog.Sentinel, alerter: Alerter,
                  journal: watchdog.Journal, database: Path, lock_path: Path, status_path: Path,
                  token_pending: Callable[[], None]) -> int:
    """One New York day of the watchdog: its broker checks on a session day and the sentinel's
    checks until the day ends (on a day without a session, the sentinel's only)."""
    now = WATCHDOG_NOW()
    until = watchdog.next_ny_midnight(now)
    try:
        day = watchdog.market_day(cfg, now)
        guard = watchdog.Watchdog(
            cfg, day, alerter=alerter, journal=journal, clock=WATCHDOG_NOW,
            sleep=WATCHDOG_SLEEP, lock_path=lock_path, database=database,
            status_path=status_path, runner=WATCHDOG_RUNNER, sentinel=sentinel,
            connect=lambda: watchdog.ReadOnlyBroker(WATCHDOG_BROKER(cfg, token_pending)))
    except CalendarRefused as exc:
        if exc.cause in watchdog.CLOSED_CAUSES:
            journal.write(watchdog.START, {"no_session": exc.cause.value})
            code = int(ExitCode.NO_SESSION)
        else:
            sentinel.alert_once("calendar_refused",
                                f"{watchdog.ny_day(now).isoformat()} {exc.cause.value}",
                                AlertKind.WATCHDOG_CANNOT_CHECK,
                                f"calendar refused today ({exc.cause.value}): check the "
                                "Webull app yourself after the flatten time")
            code = int(ExitCode.FAILED)
        watchdog.watch_until(sentinel, until, clock=WATCHDOG_NOW, sleep=WATCHDOG_SLEEP)
        return code
    return guard.run(until)


def _render(args: argparse.Namespace) -> int:
    cfg = config_module.load(args.config)
    launchd.check_timings(cfg)
    launchd.check_env_file(args.env_file)
    launchd.check_lab_folder(cfg)
    paths = launchd.Paths(  # the config's root is the folder above its own folder (config.load)
        release=args.release, config=args.config.resolve().relative_to(cfg.root),
        env_file=args.env_file, log_dir=args.log_dir)
    written = launchd.render_all(cfg.root / TEMPLATE_DIR, args.out,
                                 launchd.values(cfg.runtime, paths,
                                               launchd.api_credential_variables(cfg)))
    for path in written:
        print(path)
    return 0


def _render_systemd(args: argparse.Namespace) -> int:
    cfg = config_module.load(args.config)
    launchd.check_timings(cfg)
    launchd.check_lab_folder(cfg)
    paths = systemd.Paths(compose=args.compose, config_host=args.etc / SERVER_CONFIG,
                          env_file=args.env_file, data=args.data, user=args.user,
                          image=args.image)
    try:
        compose_text = args.compose.read_text("utf-8")
    except OSError as exc:
        raise config_module.ConfigError(
            f"cannot read the compose file {args.compose}: {type(exc).__name__}") from exc
    filled = systemd.values(cfg, paths, args.timezone, compose_text)
    for path in systemd.render_all(cfg, cfg.root / SYSTEMD_TEMPLATE_DIR, args.out, filled):
        print(path)
    return 0


REQUEST_RESTART = "request-restart"


def _request_restart(config_path: Path, cause: str) -> int:
    """The server's watchdog restart command: write the restart request file for the host's
    tickr-bot-restart.path (runtime/restartrequest.py). Exit 0 when written, 1 with the one
    cause when refused (nothing written)."""
    cfg = config_module.load(config_path)
    at = datetime.now(UTC)
    try:
        restartrequest.write_request(restartrequest.request_path(cfg), at, cause)
    except restartrequest.RestartRequestRefused as exc:
        _emit(REQUEST_RESTART, {"requested": False, "cause": exc.cause.value,
                                "detail": exc.detail})
        return int(ExitCode.FAILED)
    _emit(REQUEST_RESTART, {"requested": True, "requested_at": at.isoformat(), "why": cause})
    return 0


# Replaced in tests: the broker factory of `broker-flat`; it is only ever held through
# watchdog.ReadOnlyBroker (positions and open orders, nothing that sends).
FLAT_BROKER: Callable[[config_module.Config, Callable[[], None]], Broker] = _webull_broker


def _flat_read(cfg: config_module.Config) -> tuple[dict[str, object], int]:
    """One read-only broker read: the body of the `broker-flat` record and its exit code."""

    def pending() -> None:
        _emit("token-pending", {"wait_seconds": cfg.environment.token_check_duration_seconds})

    try:
        broker = watchdog.ReadOnlyBroker(FLAT_BROKER(cfg, pending))
        positions = sorted(p.symbol for p in broker.positions())
        orders = sorted(o.symbol for o in broker.open_orders())
    except BrokerError as exc:  # typed causes: names only, no values
        return {"error": str(exc) or type(exc).__name__}, int(ExitCode.FAILED)
    except Exception as exc:  # noqa: BLE001 - any failed read refuses; the type is the cause
        return {"error": type(exc).__name__}, int(ExitCode.FAILED)
    return {"error": None, "positions": positions, "open_orders": orders}, 0


def _broker_flat(config_path: Path) -> int:
    """One read-only broker read for `release`: the symbols of every position and open order,
    or the cause the read failed. Exit 0 when read (flat or not), 1 when the read failed."""
    body, code = _flat_read(config_module.load(config_path))
    _emit(release_module.BROKER_FLAT, body)
    return code


REQUIRE_FLAT = "require-flat"


def _require_flat(config_path: Path) -> int:
    """The server deploy's gate: exit 0 only when a fresh broker read (the same read as
    `broker-flat`) shows no position and no open order and the database has no open trade.
    Any failed read, any position, order or open trade: exit 1 with the one cause."""
    cfg = config_module.load(config_path)
    body, _ = _flat_read(cfg)
    cause: str | None = None
    if body["error"]:
        cause = f"broker read failed: {body['error']}"
    elif body["positions"] or body["open_orders"]:
        cause = f"not flat: positions {body['positions']}, open orders {body['open_orders']}"
    else:
        count = watchdog.open_trades(cfg.root / cfg.run.data_dir / DATABASE_FILE)
        if count:
            cause = f"not flat: the database has {count} open trade(s)"
    _emit(REQUIRE_FLAT, {"flat": cause is None, "cause": cause})
    return 0 if cause is None else int(ExitCode.FAILED)


# Replaced in tests: the command runner of `release` and `rollback`.
RELEASE_RUNNER: release_module.Runner = release_module.run_process


def _release(args: argparse.Namespace) -> int:
    cfg = config_module.load(args.config)
    try:
        ctx = release_module.context(cfg, args.config, args.releases, args.env_file,
                                     RELEASE_RUNNER, os.environ,
                                     lambda line: print(line, flush=True))
        if args.command == "release":
            release_module.release(ctx, args.commit)
        else:
            release_module.rollback(ctx)
    except release_module.ReleaseRefused as exc:
        print(f"refused: {exc}; the running release is unchanged", file=sys.stderr)
        return int(ExitCode.FAILED)
    except release_module.ReleaseFailed as exc:
        print(f"failed after the switch: {exc}", file=sys.stderr)
        return int(ExitCode.FAILED)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m tickr.runtime")
    parser.add_argument("--config", type=Path, default=Path("config/bot.toml"),
                        help="path to bot.toml")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("preflight", help="may the bot start today?")
    sub.add_parser("watchdog", help="read-only end-of-day checks, separate from the bot")
    sub.add_parser(release_module.BROKER_FLAT,
                   help="read positions and open orders once (read-only), for release")
    sub.add_parser(REQUIRE_FLAT, help="exit 0 only when the broker and the database show flat")
    run = sub.add_parser("run-bot", help="preflight, then run the bot command after --")
    run.add_argument("bot_command", nargs=argparse.REMAINDER)
    alert = sub.add_parser("alert", help="show one alert")
    alert.add_argument("kind", choices=[k.value for k in AlertKind])
    alert.add_argument("text")
    render = sub.add_parser("render-launchd", help="write plists for the owner to install")
    render.add_argument("--release", type=Path, required=True,
                        help="absolute folder the jobs run from: <releases>/live on the Mac "
                             "(never resolved: the jobs resolve it when they start)")
    render.add_argument("--env-file", type=Path, required=True,
                        help="absolute path of the credentials file sourced by the jobs")
    render.add_argument("--log-dir", type=Path, required=True, help="absolute log folder")
    render.add_argument("--out", type=Path, required=True, help="output folder")
    units = sub.add_parser("render-systemd", help="write systemd units for a Linux server")
    units.add_argument("--out", type=Path, required=True,
                       help="output folder: the units, the timers and ports.env")
    units.add_argument("--compose", type=Path, default=SERVER_COMPOSE,
                       help="absolute host path of the compose.yaml the units run (read here "
                            "for the bot's stop grace)")
    units.add_argument("--etc", type=Path, default=SERVER_ETC,
                       help="server folder of bot.toml (and of ports.env once installed)")
    units.add_argument("--env-file", type=Path, default=SERVER_ENV_FILE,
                       help="host path of the credentials file compose mounts (path only)")
    units.add_argument("--data", type=Path, default=SERVER_DATA,
                       help="host data folder compose mounts as the containers' data folder")
    units.add_argument("--user", default=SERVER_USER, help="service account of the units")
    units.add_argument("--image", required=True,
                       help="image reference every compose service runs (TICKR_IMAGE in "
                            "limits.env): the release image <registry>/tickr-bot:<commit>")
    units.add_argument("--timezone", default=SERVER_ZONE, help="IANA zone of the [runtime] times")
    request = sub.add_parser(REQUEST_RESTART,
                             help="server: ask the host to restart the bot container once")
    request.add_argument("--cause", required=True, help="one word, logged by the host unit")
    for name, text in (("release", "run a commit of origin/main from now on (flat only)"),
                       ("rollback", "run the previous release again (flat only)")):
        cmd = sub.add_parser(name, help=text)
        cmd.add_argument("--releases", type=Path, required=True,
                         help="absolute folder of the releases and the live link")
        cmd.add_argument("--env-file", type=Path, required=True,
                         help="absolute path of the credentials file the jobs source")
        if name == "release":
            cmd.add_argument("--commit", default=release_module.DEFAULT_COMMIT,
                             help="commit or ref on origin/main (default: origin/main)")
    args = parser.parse_args(argv)
    try:
        if args.command == "preflight":
            return int(_preflight(args.config, AlertKind.PREFLIGHT_FAILED,
                                  PreflightJob.PREFLIGHT).exit_code)
        if args.command == "run-bot":
            return _run_bot(args.config, args.bot_command)
        if args.command == "watchdog":
            return _watchdog(args.config)
        if args.command == release_module.BROKER_FLAT:
            return _broker_flat(args.config)
        if args.command == REQUIRE_FLAT:
            return _require_flat(args.config)
        if args.command == "alert":
            _alert(args.config, None, AlertKind(args.kind), args.text)
            return 0
        if args.command == "render-launchd":
            return _render(args)
        if args.command == "render-systemd":
            return _render_systemd(args)
        if args.command == REQUEST_RESTART:
            return _request_restart(args.config, args.cause)
        if args.command in ("release", "rollback"):
            return _release(args)
    except config_module.ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        if args.command == "watchdog":
            text = (f"watchdog not running: config invalid ({exc}); fix the config, then "
                    "restart the watchdog job")

            def again() -> None:
                _alert(args.config, None, AlertKind.WATCHDOG_CANNOT_CHECK, text)

            again()
            # launchd starts a watchdog that ends again (KeepAlive): wait instead, so a
            # config that stays invalid alerts once a day, not on every relaunch.
            WATCHDOG_HOLD(again)
        return int(ExitCode.CONFIG_INVALID)
    return int(ExitCode.CONFIG_INVALID)  # pragma: no cover


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
