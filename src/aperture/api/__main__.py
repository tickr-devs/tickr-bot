"""Run the local API: `python -m aperture.api [--config config/bot.toml] [serve|pair]`.

`serve` opens the local port and, when `[api] remote_hosts` is set, the phone-facing
`[api] remote_port` (bearer token always required). `pair` makes a new bearer token for the phone,
keeps only its SHA-256 under data_dir, and leaves the pairing link in a 0600 file there for the
owner to move to the phone. The token is never printed. A new pairing replaces the old one.

With `[chart] webull_enabled`, and once the chart probe C1 is recorded as passed for the
environment (`python -m aperture.probes chart-client`), `serve` builds the chart's own Webull
market-data client in a background thread once serving has begun (app key and secret from the
environment, never the
account id; it may wait for a token confirmation like the bot). Status and commands (stop,
emergency) never wait for it: until it is ready GET /chart answers 503 "starting", and if the
build fails it answers 503 with the cause.
"""

from __future__ import annotations

import argparse
import sys
import threading
from collections.abc import Callable
from pathlib import Path

from aperture import config as config_module
from aperture.api.server import (
    ApiServer,
    make_remote_server,
    make_server,
    pair,
    settings_from_config,
)
from aperture.broker.base import BrokerError
from aperture.data.base import MarketData

CHART_STARTING = "chart market data is starting: its Webull client is still being built"


def _webull_chart_source(cfg: config_module.Config) -> MarketData:
    from aperture.broker.webull import chart_market_data_from_config

    return chart_market_data_from_config(cfg)


def _chart_gate(cfg: config_module.Config) -> str | None:
    from aperture.probes import chart_client_gate

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
        print(f"aperture api: GET /chart is off: {cause}", file=sys.stderr)
        return None
    server.chart.use(None, CHART_STARTING)

    def build_source() -> None:
        try:
            source = build(cfg)
        except (BrokerError, config_module.ConfigError) as exc:  # our own texts: no secrets
            cause = f"chart market data unavailable: {type(exc).__name__}: {exc}"
            server.chart.use(None, cause)
            print(f"aperture api: GET /chart is off: {cause}", file=sys.stderr)
            return
        except Exception as exc:  # noqa: BLE001 - SDK text may carry a key or token: type only
            cause = f"chart market data unavailable: client build failed ({type(exc).__name__})"
            server.chart.use(None, cause)
            print(f"aperture api: GET /chart is off: {cause}", file=sys.stderr)
            return
        server.chart.use(source)
        print("aperture api: GET /chart is served from Webull market data", file=sys.stderr)

    thread = threading.Thread(target=build_source, name="chart-source", daemon=True)
    thread.start()
    return thread


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m aperture.api")
    parser.add_argument("--config", type=Path, default=Path("config/bot.toml"),
                        help="path to bot.toml")
    parser.add_argument("command", nargs="?", choices=("serve", "pair"), default="serve",
                        help="serve the API (default) or pair a phone")
    args = parser.parse_args(argv)
    try:
        cfg = config_module.load(args.config)
        settings = settings_from_config(cfg)
        if args.command == "pair":
            link_file = pair(settings)
    except config_module.ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 2
    if args.command == "pair":
        print("Paired a new phone token; any earlier phone token no longer works.\n"
              f"The pairing link is in {link_file} (readable by you only).\n"
              "Move it to the iPhone by AirDrop (not Notes or Messages, which sync to iCloud) and\n"
              "open it there, or paste it in Settings > Pair with Mac. The API deletes the file\n"
              "when the phone first connects with it.")
        return 0
    server = make_server(settings)
    print(f"aperture api on http://{settings.host}:{server.server_port}/", file=sys.stderr)
    remote = None
    if settings.remote_hosts:
        remote = make_remote_server(server)
        threading.Thread(target=remote.serve_forever, daemon=True).start()
        print(f"aperture api for the phone on http://{settings.host}:{remote.server_port}/ "
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
