"""`python -m aperture.runtime <command>`: the commands the launchd jobs run.

- `preflight`: run the checks, print the result as one JSON line, alert on failure.
- `run-bot -- <bot command...>`: preflight again (a failure alerts "bot did not start"), then
  run the bot under supervision; alert if it crashes. Never relaunches.
- `alert <kind> <text>`: show one alert (used to test notifications on the Mac).
- `render-launchd`: write filled-in plists from ops/launchd into a folder; installs nothing.

Output lines are JSON objects with a "record" field; launchd writes them to the job's log file.
The bot process itself journals its alerts in the database; these commands run outside the bot
(single-writer rule), so their record is the log line.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tomllib
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from aperture import config as config_module
from aperture.runtime import launchd
from aperture.runtime.alerts import Alerter, AlertKind, default_notifier
from aperture.runtime.preflight import ExitCode, PreflightResult, run_preflight
from aperture.runtime.signals import StopRequest, install_stop_handler, restore_handlers
from aperture.runtime.supervisor import supervise

TITLE = "Aperture"
TEMPLATE_DIR = Path("ops") / "launchd"


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


def _alert(config_path: Path, environment: str | None, kind: AlertKind, text: str) -> None:
    alerts = _alerts_config(config_path)
    if alerts is None:
        _emit("alert", {"kind": kind.value, "text": text, "delivery": "no-alerts-config",
                        "error": None, "at": datetime.now(UTC).isoformat()})
        return
    title = f"{TITLE} {environment}" if environment else TITLE
    outcome = Alerter(alerts, title, default_notifier(alerts)).alert(kind, text)
    _emit("alert", outcome.journal_body())


def _preflight(config_path: Path, failure: AlertKind) -> PreflightResult:
    result = run_preflight(config_path, now=datetime.now(UTC), environ=os.environ)
    _emit("preflight", result.journal_body())
    if result.cause is not None:
        _alert(config_path, result.environment, failure, result.summary())
    return result


def _run_bot(config_path: Path, command: list[str]) -> int:
    if command[:1] == ["--"]:
        command = command[1:]
    if not command:
        print("run-bot: give the bot command after --", file=sys.stderr)
        return int(ExitCode.CONFIG_INVALID)
    result = _preflight(config_path, AlertKind.BOT_DID_NOT_START)
    if result.exit_code is not ExitCode.READY or result.config is None:
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


def _render(args: argparse.Namespace) -> int:
    cfg = config_module.load(args.config)
    paths = launchd.Paths(
        repo=cfg.root, python=args.python, config=args.config.resolve(),
        env_file=args.env_file, log_dir=args.log_dir)
    written = launchd.render_all(cfg.root / TEMPLATE_DIR, args.out,
                                 launchd.values(cfg.runtime, paths))
    for path in written:
        print(path)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m aperture.runtime")
    parser.add_argument("--config", type=Path, default=Path("config/bot.toml"),
                        help="path to bot.toml")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("preflight", help="may the bot start today?")
    run = sub.add_parser("run-bot", help="preflight, then run the bot command after --")
    run.add_argument("bot_command", nargs=argparse.REMAINDER)
    alert = sub.add_parser("alert", help="show one alert")
    alert.add_argument("kind", choices=[k.value for k in AlertKind])
    alert.add_argument("text")
    render = sub.add_parser("render-launchd", help="write plists for the owner to install")
    render.add_argument("--python", type=Path, required=True, help="absolute venv python")
    render.add_argument("--env-file", type=Path, required=True,
                        help="absolute path of the credentials file sourced by the jobs")
    render.add_argument("--log-dir", type=Path, required=True, help="absolute log folder")
    render.add_argument("--out", type=Path, required=True, help="output folder")
    args = parser.parse_args(argv)
    try:
        if args.command == "preflight":
            return int(_preflight(args.config, AlertKind.PREFLIGHT_FAILED).exit_code)
        if args.command == "run-bot":
            return _run_bot(args.config, args.bot_command)
        if args.command == "alert":
            _alert(args.config, None, AlertKind(args.kind), args.text)
            return 0
        if args.command == "render-launchd":
            return _render(args)
    except config_module.ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return int(ExitCode.CONFIG_INVALID)
    return int(ExitCode.CONFIG_INVALID)  # pragma: no cover


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
