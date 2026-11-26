"""The config the platform tests run on: the shipped `config/bot.toml` with the strategy and risk
values those tests were written for pinned.

The owner's goal (2026-11-17) is that switching the live strategy, or its limits, is a change to
config alone. The platform tests (rehearsals, admission, the daily room, the demo) are written
around price-action-v3's breakout bars and the 50 / 300 / 2500 USD limits, so they read the
shipped file with those few values pinned; every other key stays the shipped one, so the tests
still run on the real config. Tests of the shipped config itself (tests/test_config.py,
preflight of the real file) read config/bot.toml directly.
"""

from __future__ import annotations

import functools
import re
import tempfile
from pathlib import Path

from tickr import config

ROOT = Path(__file__).resolve().parents[1]
SHIPPED = ROOT / "config" / "bot.toml"

# (section, key) -> TOML value the platform tests assume.
PINNED: dict[tuple[str, str], str] = {
    ("strategy", "active"): '"price-action-v3"',
    ("strategy", "observers"): '["price-action-v3-relvol-1.5", "price-action-v3-relvol-1.75"]',
    ("risk", "planned_loss_usd"): "50",
    ("risk", "max_daily_loss_usd"): "300",
    ("risk", "position_notional_cap_usd"): "2500",
    # The rehearsals and the demo script one trade at a time; several slots have their own
    # tests (tests/runtime/test_several_positions.py).
    ("risk", "max_open_positions"): "1",
}

# (section, key) -> TOML value of the Mac host, the launchd jobs' machine before the server
# cutover (2026-11-26): the shipped config/bot.toml holds the server's values, and the tests of
# the Mac's launchd paths (kickstart, power assertions, the Tickr app) set these on top of it.
MAC_HOST: dict[tuple[str, str], str] = {
    ("api", "start_bot_command"): '["/bin/launchctl", "kickstart", "gui/{uid}/local.tickr.bot"]',
    ("api", "system_services"): '"mac"',
    ("api", "sleep_check_command"): '["/usr/bin/pmset", "-g", "assertions"]',
    ("runtime", "watchdog_restart_command"):
        '["/bin/launchctl", "kickstart", "gui/{uid}/local.tickr.bot"]',
    ("alerts", "mac_app"): '"/Applications/Tickr.app"',
}

_SECTION = re.compile(r"^\[([A-Za-z0-9_.]+)\]\s*$")


def pinned_text(text: str) -> str:
    """`text` (a bot.toml) with every PINNED key set to its pinned value; each must be there."""
    return set_keys(text, PINNED)


def mac_text(text: str) -> str:
    """`text` (a bot.toml) with the Mac host's values (MAC_HOST); each key must be there."""
    return set_keys(text, MAC_HOST)


def write_mac(path: Path) -> Path:
    """Write the shipped config with the Mac host's values to `path`."""
    path.write_text(mac_text(SHIPPED.read_text("utf-8")), "utf-8")
    return path


def set_keys(text: str, values: dict[tuple[str, str], str]) -> str:
    """`text` (a bot.toml) with every (section, key) of `values` set; each must be there."""
    section, out, seen = "", [], set()
    for line in text.splitlines(keepends=True):
        header = _SECTION.match(line)
        if header:
            section = header.group(1)
        key = line.split("=", 1)[0].strip() if "=" in line and not line.lstrip().startswith(
            "#") else None
        value = values.get((section, key)) if key is not None else None
        if value is not None:
            line = f"{key} = {value}\n"
            seen.add((section, key))
        out.append(line)
    missing = set(values) - seen
    if missing:
        raise AssertionError(f"keys not in the config: {sorted(missing)}")
    return "".join(out)


def write_baseline(path: Path) -> Path:
    """Write the baseline config to `path` (a `config/bot.toml` of a copied repo)."""
    path.write_text(pinned_text(SHIPPED.read_text("utf-8")), "utf-8")
    return path


@functools.cache
def baseline_path() -> Path:
    """A baseline config file whose relative paths resolve against the repo (load with
    `root=ROOT`, or use `load_baseline`)."""
    folder = Path(tempfile.mkdtemp(prefix="tickr-baseline-"))
    return write_baseline(folder / "bot.toml")


def load_baseline() -> config.Config:
    return config.load(baseline_path(), root=ROOT)
