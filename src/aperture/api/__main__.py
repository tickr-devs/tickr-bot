"""Run the local API: `python -m aperture.api [--config config/bot.toml] [serve|pair]`.

`serve` opens the local port and, when `[api] remote_hosts` is set, the phone-facing
`[api] remote_port` (bearer token always required). `pair` makes a new bearer token for the phone,
keeps only its SHA-256 under data_dir, and leaves the pairing link in a 0600 file there for the
owner to move to the phone. The token is never printed. A new pairing replaces the old one.

With `[chart] webull_enabled`, `serve` builds the chart's own Webull market-data client first
(app key and secret from the environment, never the account id; it may wait for a token
confirmation like the bot). If that fails, GET /chart answers 503 with the cause and everything
else is served as usual.
"""

from __future__ import annotations

import argparse
import sys
import threading
from pathlib import Path

from aperture import config as config_module
from aperture.api.server import (
    ApiServer,
    make_remote_server,
    make_server,
    pair,
    settings_from_config,
)


def attach_chart_source(server: ApiServer, cfg: config_module.Config) -> None:
    """Give GET /chart its Webull source when `[chart] webull_enabled`; else leave it off."""
    if not cfg.chart.webull_enabled:
        return
    from aperture.broker.base import BrokerError
    from aperture.broker.webull import chart_market_data_from_config

    try:
        server.chart.use(chart_market_data_from_config(cfg))
    except BrokerError as exc:  # AdapterConfigError: credentials, token, budget slice
        server.chart.use(None, f"chart market data unavailable: {exc}")
        print(f"aperture api: GET /chart is off: {exc}", file=sys.stderr)


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
    attach_chart_source(server, cfg)
    print(f"aperture api on http://{settings.host}:{server.server_port}/", file=sys.stderr)
    remote = None
    if settings.remote_hosts:
        remote = make_remote_server(server)
        threading.Thread(target=remote.serve_forever, daemon=True).start()
        print(f"aperture api for the phone on http://{settings.host}:{remote.server_port}/ "
              "(bearer token required; point tailscale serve here)", file=sys.stderr)
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
