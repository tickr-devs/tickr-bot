"""Run the local API: `python -m tickr.api [--config config/bot.toml] [serve|pair|devices|revoke]`.

`serve` opens the local port and, when `[api] remote_hosts_file` names a host, the phone-facing
`[api] remote_port` (a paired device's bearer token always required). `pair --name <device>`
leaves a pairing link with a one-time code in a 0600 file under data_dir for the owner to move to
the device; the device redeems the code once, at POST /pair on the remote port, for its own token,
beside the devices already paired (one of the same name is replaced). `devices` lists the paired
devices and `revoke --name <device>` revokes one and drops its push tokens. Only SHA-256
digests are kept; nothing secret is printed.

With `[chart] webull_enabled`, and once the chart probe C1 is recorded as passed for the
environment (`python -m tickr.probes chart-client`), `serve` builds the chart's own Webull
market-data client in a background thread once serving has begun (app key and secret from the
environment, never the
account id; it may wait for a token confirmation like the bot). Status and commands (stop,
emergency) never wait for it: until it is ready GET /chart answers 503 "starting", and if the
build fails it answers 503 with the cause.

At its start `serve` checks which credential variables it holds (names only): any beyond what its
launchd job is meant to give it (`runtime.launchd.api_credential_variables`: none with the chart
off, the active environment's app key and secret with it on) raise the
`api-credentials-unexpected` alert (the alerts feed file) and a line in api.log naming them; the API
still serves. Its plist is then stale or edited: render and reinstall it (docs/operations.md).
"""

from __future__ import annotations

import argparse
import os
import sys
import threading
from collections.abc import Callable, Mapping
from pathlib import Path

from tickr import config as config_module
from tickr.api import contract
from tickr.api.devices import DeviceFileError, valid_name
from tickr.api.server import (
    ApiServer,
    list_devices,
    make_remote_server,
    make_server,
    pair,
    pair_simulator,
    revoke_device,
    settings_from_config,
    unpair_simulator,
)
from tickr.broker.base import BrokerError
from tickr.data.base import MarketData
from tickr.runtime.alerts import (
    FEED,
    Alerter,
    AlertKind,
    AlertOutcome,
    Channels,
    FeedNotifier,
    Notifier,
    feed_path,
)
from tickr.runtime.launchd import api_credential_variables
from tickr.runtime.push_tokens import StoreError

SECONDS_PER_MINUTE = 60
# Alert title prefix, the same as the runtime commands' (runtime/__main__.py TITLE).
ALERT_TITLE = "Tickr"
CHART_STARTING = "chart market data is starting: its Webull client is still being built"


def _webull_chart_source(cfg: config_module.Config) -> MarketData:
    from tickr.broker.webull import chart_market_data_from_config

    return chart_market_data_from_config(cfg)


def _chart_gate(cfg: config_module.Config) -> str | None:
    from tickr.probes import chart_client_gate

    return chart_client_gate(cfg)


def attach_chart_source(
    server: ApiServer, cfg: config_module.Config,
    build: Callable[[config_module.Config], MarketData] = _webull_chart_source,
    gate: Callable[[config_module.Config], str | None] = _chart_gate,
) -> threading.Thread | None:
    """Give GET /chart its Webull source when `[chart] webull_enabled`; else leave it off. The
    client is built in a daemon thread (returned) so nothing else the API serves waits for it;
    GET /chart answers 503 while it builds and, if the build fails, 503 with the cause.

    No client is built until the chart probe C1 is recorded as passed for this environment
    (`gate`): this process would be a second Webull client next to the bot, which is what C1
    tests. Until then GET /chart answers 503 with that cause; checked once, at start."""
    if not cfg.chart.webull_enabled:
        return None
    try:
        refused = gate(cfg)
    except Exception as exc:  # noqa: BLE001 - the chart never takes the API down at start
        refused = f"the chart probe C1 gate failed ({type(exc).__name__})"
    if refused is not None:
        cause = f"chart market data is off: {refused}"
        server.chart.use(None, cause)
        print(f"tickr api: GET /chart is off: {cause}", file=sys.stderr)
        return None
    server.chart.use(None, CHART_STARTING)

    def build_source() -> None:
        try:
            source = build(cfg)
        except (BrokerError, config_module.ConfigError) as exc:  # our own texts: no secrets
            cause = f"chart market data unavailable: {type(exc).__name__}: {exc}"
            server.chart.use(None, cause)
            print(f"tickr api: GET /chart is off: {cause}", file=sys.stderr)
            return
        except Exception as exc:  # noqa: BLE001 - SDK text may carry a key or token: type only
            cause = f"chart market data unavailable: client build failed ({type(exc).__name__})"
            server.chart.use(None, cause)
            print(f"tickr api: GET /chart is off: {cause}", file=sys.stderr)
            return
        server.chart.use(source)
        print("tickr api: GET /chart is served from Webull market data", file=sys.stderr)

    thread = threading.Thread(target=build_source, name="chart-source", daemon=True)
    thread.start()
    return thread


def unexpected_credentials(cfg: config_module.Config,
                           environ: Mapping[str, str]) -> tuple[str, ...]:
    """NAMES of the credential variables set in `environ` that the API job must not have."""
    allowed = set(api_credential_variables(cfg))
    return tuple(sorted(name for name in config_module.credential_variables(cfg) - allowed
                        if environ.get(name)))


def _api_notifier(cfg: config_module.Config) -> Notifier | None:
    """The alerts feed file only: the API runs without any credential variable, so it has no
    Telegram token or chat id (the alert is also a line in api.log and on stderr)."""
    return Channels((), ((FEED, FeedNotifier(feed_path(cfg.root, cfg.run.data_dir, cfg.alerts))),))


def _log_outcomes(outcomes: list[AlertOutcome]) -> None:
    for outcome in outcomes:
        body = outcome.journal_body()
        print(f"tickr api: alert {body['kind']} {body['delivery']}"
              + (f" ({body['error']})" if body["error"] else ""), file=sys.stderr)


def auth_alert(cfg: config_module.Config,
               notifier: Callable[[config_module.Config], Notifier | None] = _api_notifier,
               ) -> Callable[[str], None]:
    """The remote port's `remote-auth-blocked` alert (ApiServer.auth_alert): its channels run in
    the background, so the request never waits for them; each outcome is one line in api.log.
    The text names the client address and the budget, never a token or code."""
    alerter = Alerter(cfg.alerts, f"{ALERT_TITLE} {cfg.environment_name}", notifier(cfg))
    lock = threading.Lock()  # request threads share one alerter

    def send(text: str) -> None:
        with lock:
            _log_outcomes([*alerter.completed(),
                           alerter.alert(AlertKind.REMOTE_AUTH_BLOCKED, text)])

    return send


def check_credentials(cfg: config_module.Config, environ: Mapping[str, str] = os.environ,
                      notifier: Callable[[config_module.Config], Notifier | None] = _api_notifier,
                      ) -> threading.Thread | None:
    """Alert `api-credentials-unexpected` when the API holds a credential variable its job must
    not give it. The alert is sent in a daemon thread (returned) so serving never waits for it;
    each outcome is one line in api.log. Names only, never a value."""
    names = unexpected_credentials(cfg, environ)
    if not names:
        return None
    text = (f"the API holds {', '.join(names)}, which its launchd job must not give it: render "
            "and reinstall the API plist (docs/operations.md, The API job and the Webull keys)")
    print(f"tickr api: {AlertKind.API_CREDENTIALS_UNEXPECTED.value}: {text}", file=sys.stderr)
    alerter = Alerter(cfg.alerts, f"{ALERT_TITLE} {cfg.environment_name}", notifier(cfg))

    def send() -> None:
        _log_outcomes([alerter.alert(AlertKind.API_CREDENTIALS_UNEXPECTED, text),
                       *alerter.flush()])

    thread = threading.Thread(target=send, name="credentials-alert", daemon=True)
    thread.start()
    return thread


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m tickr.api")
    parser.add_argument("--config", type=Path, default=Path("config/bot.toml"),
                        help="path to bot.toml")
    parser.add_argument("command", nargs="?",
                        choices=("serve", "pair", "devices", "revoke", "pair-simulator",
                                 "unpair-simulator"),
                        default="serve",
                        help="serve the API (default), pair a device, list the paired devices, "
                             "revoke one, or give the iOS Simulator on this Mac its own token "
                             "(unpair-simulator revokes it)")
    parser.add_argument("--name", help="the device to pair or revoke (pair, revoke)")
    args = parser.parse_args(argv)
    if args.command in ("pair", "revoke") and (args.name is None or not valid_name(args.name)):
        parser.error(f"{args.command} needs --name: letters, digits, space, dot, dash and "
                     "underscore, starting and ending with a letter or digit")
    try:
        cfg = config_module.load(args.config)
        settings = settings_from_config(cfg, args.config)
        if args.command == "pair":
            link_file = pair(settings, args.name)
        if args.command == "devices":
            paired = list_devices(settings)
        if args.command == "revoke":
            revoked = revoke_device(settings, args.name)
        if args.command == "pair-simulator":
            token_file = pair_simulator(settings)
        if args.command == "unpair-simulator":
            unpair_simulator(settings)
    except StoreError as exc:  # revoked all the same; only the push tokens are kept
        print(f"The token is revoked, but its push tokens could not be dropped: {exc}; delete "
              "that file (each app registers again at its next launch).", file=sys.stderr)
        return 1
    except config_module.ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 2
    except (DeviceFileError, OSError) as exc:
        print(f"the paired devices are unreadable: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    if args.command == "devices":
        if not paired:
            print("No device is paired; run `python -m tickr.api pair --name <device>`.")
        for device in paired:
            last = "never" if device.last_used_at is None \
                else contract.format_time(device.last_used_at)
            print(f"{device.name}\tpaired {contract.format_time(device.created_at)}\t"
                  f"last used {last}")
        return 0
    if args.command == "revoke":
        if not revoked:
            print(f"No device is named {args.name!r}; `devices` lists them.", file=sys.stderr)
            return 1
        print(f"The token of {args.name!r} is revoked; the other devices are unchanged.")
        return 0
    if args.command == "pair-simulator":
        print("The iOS Simulator has a new token of its own; the phone's token is unchanged.\n"
              f"It is in {token_file} (readable by you only) with the address it opens. Put\n"
              "that path in apps/ios/Config/Local.xcconfig as TICKR_SIMULATOR_TOKEN_FILE; a\n"
              "Simulator build reads it at launch.")
        return 0
    if args.command == "unpair-simulator":
        print("The iOS Simulator's token is revoked; the phone's token is unchanged.")
        return 0
    if args.command == "pair":
        minutes = settings.pairing_code_ttl_seconds // SECONDS_PER_MINUTE
        print(f"Started pairing {args.name!r}; the devices already paired keep working (one\n"
              f"named {args.name!r} keeps its token until this code is redeemed, then loses it).\n"
              f"The pairing link is in {link_file} (readable by you only). It holds a one-time\n"
              f"code, valid for {minutes} min and for one device only; the API must be running.\n"
              "Move it to the iPhone by AirDrop and open it there, or paste it in Settings >\n"
              "Pair with Mac. Once the phone has paired, the link is worthless and the API\n"
              "deletes the file.")
        return 0
    try:
        check_credentials(cfg)
    except config_module.ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 2
    server = make_server(settings, warm=True)
    # risk-command-used: shared by the remote listener made below.
    server.alerter = Alerter(cfg.alerts, f"{ALERT_TITLE} {cfg.environment_name}",
                             _api_notifier(cfg))
    print(f"tickr api on http://{settings.host}:{server.server_port}/", file=sys.stderr)
    remote = None
    if settings.remote_hosts:
        remote = make_remote_server(server)
        remote.auth_alert = auth_alert(cfg)
        threading.Thread(target=remote.serve_forever, daemon=True).start()
        print(f"tickr api for the phone on http://{settings.host}:{remote.server_port}/ "
              "(bearer token required; point tailscale serve here)", file=sys.stderr)
    attach_chart_source(server, cfg)  # in the background: serving does not wait for it
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        if remote is not None:
            remote.shutdown()
            remote.server_close()
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
