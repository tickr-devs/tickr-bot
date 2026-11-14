"""Run the real `aperture run` (cli.main) against a `World` on a fake clock.

Everything below the adapters is production code: config, store, engine, market runtime,
discovery, history, the active strategy, admission, alerts, status and backups. Only the broker,
the market data source, the clock, `sleep` and the notifier are replaced.
"""

from __future__ import annotations

import json
import os
import shutil
import signal
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from aperture import cli, config
from aperture.api import contract as api
from aperture.runtime import service
from aperture.store import JournalRow, Store
from rehearsal.world import World, flat_session, ny

ROOT = Path(__file__).resolve().parents[2]
DAY = date(2026, 11, 16)  # a Monday; the five sessions before it are Nov 9-13
PRIOR_SESSIONS = [date(2026, 11, d) for d in (9, 10, 11, 12, 13)]
QUIET_VOLUME = Decimal(3000)


def history(symbol: str, price: Decimal) -> list:
    return [b for d in PRIOR_SESSIONS for b in flat_session(symbol, d, price, QUIET_VOLUME)]


class Notifier:
    def __init__(self, world: World) -> None:
        self.world = world
        self.shown: list[tuple[datetime, str, str]] = []

    def notify(self, title: str, text: str) -> None:
        self.shown.append((self.world.now, title, text))


class Rehearsal:
    def __init__(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                 start: str = "09:20") -> None:
        for part in ("config", "strategies"):
            shutil.copytree(ROOT / part, tmp_path / part)
        self.bot_toml = tmp_path / "config" / "bot.toml"
        self.world = World(ny(DAY, start))
        self.notifier = Notifier(self.world)
        monkeypatch.setattr(cli, "RUN_ADAPTERS", lambda cfg, on_error, on_token: cli.RunAdapters(
            self.world.exchange, self.world.feed, self._stream_start, self._stream_stop))
        monkeypatch.setattr(cli, "NOW", self.world.clock)
        monkeypatch.setattr(cli, "SLEEP", self.world.sleep)
        monkeypatch.setattr(cli, "MONOTONIC", self.world.mono)
        monkeypatch.setattr(cli, "NOTIFIER", lambda alerts: self.notifier)
        self.config = config.load(self.bot_toml)
        self.paths = service.ServicePaths.from_config(self.config)
        self.database, _ = cli.data_paths(self.config)

    def _stream_start(self) -> None:
        self.world.stream_up = True
        self.world.feed.transport(True, self.world.now, "connected")

    def _stream_stop(self) -> None:
        self.world.stream_up = False

    # --- driving -------------------------------------------------------------------------

    def run_until(self, clock: str) -> int:
        """`aperture run` from the world's current time until SIGTERM at `clock` (New York)."""
        self.world.at(ny(DAY, clock), lambda: os.kill(os.getpid(), signal.SIGTERM))
        return cli.main(["--config", str(self.bot_toml), "run"])

    def bot_down_until(self, clock: str, step_seconds: float = 3.0) -> None:
        """The bot is not running: the market (and resting orders at the broker) moves on."""
        target = ny(DAY, clock)
        while self.world.now < target:
            self.world.advance(step_seconds)

    def at(self, clock: str, action: Any) -> None:
        self.world.at(ny(DAY, clock), action)

    def command(self, kind: api.CommandKind) -> None:
        """What the local API does on a POST: write the command into the bot's inbox."""
        api.write_command(self.paths.inbox, api.new_command(kind, self.world.now, "rehearsal"))

    # --- reading -------------------------------------------------------------------------

    def store(self) -> Store:
        return Store.open(self.database, busy_timeout_seconds=0)

    def journal(self, kind: str | None = None) -> list[JournalRow]:
        store = self.store()
        try:
            return store.journal(kind) if kind is not None else store.journal()
        finally:
            store.close()

    def trades(self) -> list[dict[str, Any]]:
        store = self.store()
        try:
            return [dict(r) for r in store.query("SELECT * FROM trades ORDER BY opened_at")]
        finally:
            store.close()

    def status(self) -> api.StatusV1:
        return api.read_status(self.paths.status)

    def decisions(self) -> list[dict[str, Any]]:
        folder = (self.config.root / self.config.run.data_dir / self.config.observers.decisions_dir
                  / self.config.environment_name / DAY.isoformat())
        path = folder / "decisions.jsonl"
        return [json.loads(line) for line in path.read_text("utf-8").splitlines()]

    def fills(self, symbol: str) -> list[tuple[datetime, str, str, Decimal, Decimal]]:
        return [(at, side, kind, qty, price) for at, s, side, kind, qty, price
                in self.world.exchange.fills if s == symbol]


def minutes(n: int) -> timedelta:
    return timedelta(minutes=n)
