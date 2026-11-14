"""Run the local API: `python -m aperture.api [--config config/bot.toml]`."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from aperture import config as config_module
from aperture.api.server import make_server, settings_from_config


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m aperture.api")
    parser.add_argument("--config", type=Path, default=Path("config/bot.toml"),
                        help="path to bot.toml")
    args = parser.parse_args(argv)
    try:
        settings = settings_from_config(config_module.load(args.config))
    except config_module.ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 2
    server = make_server(settings)
    print(f"aperture api on http://{settings.host}:{server.server_port}/", file=sys.stderr)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
