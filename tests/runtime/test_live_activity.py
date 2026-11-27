"""The Live Activity publisher (runtime/live_activity.py): events from a sequence of status
snapshots, the push's shape and headers, token routing, and that the bot never loads it.

The network is the fake transport of test_apns (`Wire`); every key is generated in the test."""

from __future__ import annotations

import ast
import json
import re
import subprocess
import sys
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from api.samples import POSITION, T0, account, status

from tickr import config as config_module
from tickr.api import contract
from tickr.api.contract import EntryBlock, Fence, Freshness, PushTokenKind, StopState
from tickr.runtime import live_activity
from tickr.runtime.alerts import APNS_PATH, ApnsClient
from tickr.runtime.live_activity import ActivityState, Event, Publisher
from tickr.runtime.push_tokens import PushToken, PushTokenStore

from .test_apns import APNS, AT, IOS, MACOS, Clock, Wire, environ, key  # noqa: F401

ROOT = Path(__file__).resolve().parents[2]
BOT = ROOT / "config" / "bot.toml"
ALERT_TOKEN = "a1" * 32
START_TOKEN = "c3" * 32
UPDATE_TOKEN = "d4" * 32
MAC_UPDATE_TOKEN = "e5" * 32
TOPIC = "bot.tickr.ios.push-type.liveactivity"
PENDING = replace(POSITION, stop=replace(POSITION.stop, state=StopState.PENDING))
OTHER = replace(POSITION, symbol="EFGH", stop=replace(POSITION.stop, price=Decimal("7.5")))
CALM = EntryBlock("own_trade_open", "The bot already has a trade open.", attention=False)


class Now:
    def __init__(self) -> None:
        self.at = T0

    def __call__(self):
        return self.at


@pytest.fixture
def cfg(tmp_path):
    base = config_module.load(BOT)
    alerts = replace(base.alerts, apns_key_id_variable=APNS.apns_key_id_variable,
                     apns_team_id_variable=APNS.apns_team_id_variable,
                     apns_key_path_variable=APNS.apns_key_path_variable)
    return replace(base, root=tmp_path, alerts=alerts,
                   live_activity=replace(base.live_activity, enabled=True))


@pytest.fixture
def store(cfg):
    store = PushTokenStore(cfg.root / cfg.run.data_dir / cfg.alerts.push_tokens_file)
    for device, kind, token, bundle, platform in (
            ("phone", PushTokenKind.ALERT, ALERT_TOKEN, "bot.tickr.ios", IOS),
            ("phone", PushTokenKind.ACTIVITY_START, START_TOKEN, "bot.tickr.ios", IOS),
            ("phone", PushTokenKind.ACTIVITY_UPDATE, UPDATE_TOKEN, "bot.tickr.ios", IOS),
            ("local", PushTokenKind.ACTIVITY_UPDATE, MAC_UPDATE_TOKEN, "bot.tickr.mac", MACOS)):
        store.register(device, PushToken(kind, token, "development", bundle, platform, AT))
    return store


class World:
    """The bot's files as the publisher reads them, a clock, the wire and a journal."""

    def __init__(self, cfg, store, environ, wire=None) -> None:  # noqa: F811
        self.cfg = cfg
        self.store = store
        self.environ = environ
        self.now = Now()
        self.wire = wire or Wire()
        self.journal: list[dict] = []
        self.data = cfg.root / cfg.run.data_dir
        self.publisher = self.new_publisher()

    def new_publisher(self) -> Publisher:
        client = ApnsClient(self.cfg.alerts, self.store, self.environ, self.wire, Clock())
        return Publisher(self.cfg, client, lambda record, body: self.journal.append(body),
                         self.now)

    def look(self, after: float = 1, account_doc=None, **overrides):
        """Advance the clock, write the bot's files, take one look; the event and the pushes."""
        self.now.at += timedelta(seconds=after)
        overrides.setdefault("freshness", Freshness(self.now.at, None,
                                                    contract.StreamHealth.FRESH))
        positions = overrides.setdefault("positions", ())
        overrides.setdefault("position", positions[0] if positions else None)
        overrides.setdefault("entry_block", CALM)
        overrides.setdefault("fences", ())
        contract.write_status(self.data / self.cfg.api.status_file,
                              status(self.now.at, **overrides))
        contract.write_account(self.data / self.cfg.api.account_file,
                               account_doc or account(self.now.at))
        before = len(self.wire.calls)
        event = self.publisher.step()
        return event, self.wire.calls[before:]


@pytest.fixture
def world(cfg, store, environ):  # noqa: F811
    return World(cfg, store, environ)


def _aps(call) -> dict:
    return call[3]["aps"]


def _token(call) -> str:
    return call[1].removeprefix(APNS_PATH)


# ---------------------------------------------------------------- events


def test_start_at_the_first_fill_then_updates_only_on_change(world):
    assert world.look() == (None, [])  # flat: nothing to show
    event, calls = world.look(positions=(PENDING,))
    assert event is Event.START
    assert [_token(c) for c in calls] == [START_TOKEN]  # only the start token
    host, _, headers, body = calls[0]
    assert headers["apns-topic"] == TOPIC
    assert headers["apns-push-type"] == "liveactivity"
    assert headers["apns-priority"] == "5"
    aps = body["aps"]
    assert aps["event"] == "start"
    assert aps["timestamp"] == int(world.now.at.timestamp())
    assert aps["attributes-type"] == world.cfg.live_activity.attributes_type
    assert aps["attributes"] == {}
    assert "dismissal-date" not in aps
    assert aps["content-state"] == {
        "state": "open", "day_pnl_cents": -167, "stops_confirmed_at": None,
        "reason_code": None, "alarm_symbol": None,
        "positions": [{"symbol": "ABCD", "last": "12.40", "stop": "11.90", "target": None,
                       "unrealized_cents": 60}]}
    # Nothing changed: no push, however often it looks.
    assert world.look(positions=(PENDING,)) == (None, [])
    assert world.look(positions=(PENDING,)) == (None, [])


def test_the_stop_confirmed_is_an_update_and_its_time_rides_along(world):
    world.look(positions=(PENDING,))
    event, calls = world.look(positions=(POSITION,))
    assert event is Event.UPDATE
    assert [_token(c) for c in calls] == [UPDATE_TOKEN]  # the iOS app's update token only
    assert calls[0][2]["apns-priority"] == "5"
    assert "attributes" not in _aps(calls[0])
    confirmed = int(world.now.at.timestamp())
    assert _aps(calls[0])["content-state"]["stops_confirmed_at"] == confirmed
    # A later broker read only moves stops_confirmed_at: not an event.
    assert world.look(positions=(POSITION,)) == (None, [])


def test_a_pause_and_the_resume_are_updates_with_the_entry_block_code(world):
    world.look(positions=(POSITION,))
    block = EntryBlock("entry_stop", "Entries stopped by the operator.")
    event, calls = world.look(positions=(POSITION,), entry_block=block)
    assert event is Event.UPDATE
    state = _aps(calls[0])["content-state"]
    assert (state["state"], state["reason_code"]) == ("paused", "entry_stop")
    event, calls = world.look(positions=(POSITION,))
    assert (event, _aps(calls[0])["content-state"]["state"]) == (Event.UPDATE, "open")
    assert _aps(calls[0])["content-state"]["reason_code"] is None


def test_an_unconfirmed_stop_alarms_once_the_bot_alerts_it_at_priority_10(world):
    world.look(positions=(PENDING,))
    after = world.cfg.alerts.fence_alert_after_seconds
    since = world.now.at
    fence = Fence("unprotected:t1", since, "verified stop covers 0 of 10", "unprotected")
    # Set, but younger than [alerts] fence_alert_after_seconds: the bot does not alert yet.
    assert world.look(positions=(PENDING,), fences=(fence,)) == (None, [])
    event, calls = world.look(float(after), positions=(PENDING,), fences=(fence,))
    assert event is Event.UPDATE
    assert calls[0][2]["apns-priority"] == "10"
    state = _aps(calls[0])["content-state"]
    assert (state["state"], state["reason_code"], state["alarm_symbol"]) == (
        "alarm", "unprotected", "ABCD")
    # An operator fence (unprotected-timeout) alarms at once.
    world.look(positions=(POSITION,))
    timeout = Fence("unprotected-timeout:t1", world.now.at, "no verified stop for 30 s; exiting",
                    "unprotected_exiting")
    event, calls = world.look(positions=(PENDING,), fences=(timeout,))
    assert _aps(calls[0])["content-state"]["reason_code"] == "unprotected_exiting"


def test_a_fill_and_a_close_with_a_position_left_are_updates(world):
    world.look(positions=(POSITION,))
    event, calls = world.look(positions=(POSITION, OTHER))
    assert event is Event.UPDATE
    shown = _aps(calls[0])["content-state"]["positions"]
    assert [(p["symbol"], p["stop"], p["last"]) for p in shown] == [
        ("ABCD", "11.90", "12.40"), ("EFGH", "7.5", None)]  # no broker row: null, not a guess
    event, _ = world.look(positions=(OTHER,), result_today_net_usd=Decimal("3.005"))
    assert event is Event.UPDATE


def test_flat_ends_at_priority_10_and_a_later_fill_starts_again(world):
    world.look(positions=(POSITION,))
    event, calls = world.look()
    assert event is Event.END
    aps = _aps(calls[0])
    assert [_token(c) for c in calls] == [UPDATE_TOKEN]
    assert calls[0][2]["apns-priority"] == "10"
    assert aps["event"] == "end"
    assert aps["content-state"]["state"] == "flat"
    assert aps["content-state"]["positions"] == []
    assert "dismissal-date" not in aps  # linger 0: the system default
    assert world.look() == (None, [])
    event, calls = world.look(positions=(POSITION,))
    assert (event, [_token(c) for c in calls]) == (Event.START, [START_TOKEN])


def test_end_lingers_for_the_configured_seconds(cfg, store, environ):  # noqa: F811
    linger = replace(cfg, live_activity=replace(cfg.live_activity, end_linger_seconds=900))
    world = World(linger, store, environ)
    world.look(positions=(POSITION,))
    _, calls = world.look()
    assert _aps(calls[0])["dismissal-date"] == int(world.now.at.timestamp()) + 900


def test_a_periodic_refresh_only_when_configured_and_due(cfg, store, environ):  # noqa: F811
    world = World(cfg, store, environ)
    world.look(positions=(POSITION,))
    moved = account(world.now.at, positions=(contract.AccountPosition(
        "ABCD", Decimal("10"), Decimal("12.34"), Decimal("12.50"), None, Decimal("1.60")),))
    # Off (0): a price change alone is never pushed.
    assert world.look(3600, account_doc=replace(moved, read_at=world.now.at + timedelta(
        seconds=3600)), positions=(POSITION,)) == (None, [])
    refresh = replace(cfg, live_activity=replace(cfg.live_activity, pnl_refresh_seconds=60))
    world = World(refresh, store, environ)
    world.now.at = T0 + timedelta(days=1)
    world.look(positions=(POSITION,))
    late = world.now.at + timedelta(seconds=59)
    assert world.look(59, account_doc=replace(moved, read_at=late),
                      positions=(POSITION,)) == (None, [])
    late += timedelta(seconds=1)
    event, calls = world.look(1, account_doc=replace(moved, read_at=late), positions=(POSITION,))
    assert event is Event.UPDATE
    assert _aps(calls[0])["content-state"]["positions"][0]["last"] == "12.50"
    assert _aps(calls[0])["content-state"]["positions"][0]["unrealized_cents"] == 160


def test_a_stale_or_missing_status_pushes_nothing(world):
    world.look(positions=(POSITION,))
    (world.data / world.cfg.api.status_file).unlink()
    assert world.publisher.step() is None
    world.look(positions=())
    world.now.at += timedelta(seconds=float(world.cfg.api.status_stale_seconds) + 1)
    assert world.publisher.step() is None  # flat, but too old to end on


def test_a_restarted_publisher_neither_starts_again_nor_repeats(world):
    world.look(positions=(POSITION,))
    world.publisher = world.new_publisher()
    assert world.look(positions=(POSITION,)) == (None, [])
    event, _ = world.look()
    assert event is Event.END


def test_a_gone_token_is_dropped_and_never_retried(cfg, store, environ):  # noqa: F811
    wire = Wire({UPDATE_TOKEN: (410, b'{"reason":"Unregistered"}')})
    world = World(cfg, store, environ, wire)
    world.look(positions=(POSITION,))
    _, calls = world.look(positions=(POSITION, OTHER))
    assert [_token(c) for c in calls] == [UPDATE_TOKEN]  # one attempt
    assert "HTTP 410 Unregistered (token removed)" in world.journal[-1]["failures"][0]
    assert UPDATE_TOKEN not in {t.token for t in store.read().values()}
    assert world.look(positions=(POSITION,)) == (Event.UPDATE, [])


def test_a_signed_out_devices_activity_tokens_are_never_pushed_and_are_dropped(
        cfg, store, environ):  # noqa: F811
    """A signed-in device that stopped refreshing: its Live Activity tokens are no recipients
    either, from the same `ApnsClient.devices` the alert push reads."""
    ended = datetime.fromtimestamp(Clock().now, UTC)
    for kind, token in ((PushTokenKind.ACTIVITY_START, "c7" * 32),
                        (PushTokenKind.ACTIVITY_UPDATE, "d8" * 32)):
        store.register("tablet", PushToken(kind, token, "development", "bot.tickr.ios", IOS,
                                           AT, ended))
    world = World(cfg, store, environ)
    _, calls = world.look(positions=(PENDING,))
    assert [_token(c) for c in calls] == [START_TOKEN]
    _, calls = world.look(positions=(POSITION,))
    assert [_token(c) for c in calls] == [UPDATE_TOKEN]
    assert "tablet" not in {device for device, _ in store.read()}
    assert world.journal[-1]["failures"] == []


def test_a_start_with_no_start_token_is_not_sent_journaled_once_and_tried_again(world):
    world.store.remove("phone", PushTokenKind.ACTIVITY_START)
    assert world.look(positions=(PENDING,)) == (None, [])
    assert not world.publisher._sent  # never marked live: a later start is not lost
    assert not (world.data / world.cfg.live_activity.state_file).exists()
    assert world.journal == [{"event": "start", "state": "open", "sent": 0,
                              "failures": [live_activity.NO_START_TOKEN]}]
    assert world.look(positions=(PENDING,)) == (None, [])
    assert len(world.journal) == 1  # once per empty spell, not per look
    world.store.register("phone", PushToken(PushTokenKind.ACTIVITY_START, START_TOKEN,
                                            "development", "bot.tickr.ios", IOS, AT))
    event, calls = world.look(positions=(PENDING,))
    assert event is Event.START
    assert [_token(c) for c in calls] == [START_TOKEN]
    assert world.look(positions=(OTHER,))[0] is Event.UPDATE  # live from here on


def test_a_start_token_of_another_app_only_is_no_start_token(cfg, store, environ):  # noqa: F811
    store.remove("phone", PushTokenKind.ACTIVITY_START)
    store.register("mac", PushToken(PushTokenKind.ACTIVITY_START, START_TOKEN, "development",
                                    "bot.tickr.mac", MACOS, AT))
    world = World(cfg, store, environ)
    assert world.look(positions=(PENDING,)) == (None, [])
    assert world.journal[-1]["failures"] == [live_activity.NO_START_TOKEN]


def test_the_content_state_has_exactly_the_agreed_fields():
    content = live_activity.ContentState(ActivityState.OPEN, 0, (), None, None, None)
    assert set(content.jsonable()) == {"state", "day_pnl_cents", "positions",
                                       "stops_confirmed_at", "reason_code", "alarm_symbol"}
    position = live_activity.ActivityPosition("A", Decimal("1E+1"), None, None, None)
    assert contract.to_jsonable(position)["last"] == "10"  # a decimal string, never a float


def test_cents_round_half_even():
    assert [live_activity.cents(Decimal(v)) for v in ("1.005", "1.015", "-1.67")] == [
        100, 102, -167]


# ---------------------------------------------------------------- config and isolation


def test_shipped_off_and_events_only():
    live = config_module.load(BOT).live_activity
    assert (live.enabled, live.pnl_refresh_seconds, live.end_linger_seconds) == (False, 0, 0)


def _copy(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "config" / "bot.toml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, "utf-8")
    return path


@pytest.mark.parametrize(("extra", "match"), [
    ([(r'(?m)^apns_key_id_variable = "\S+"', 'apns_key_id_variable = ""'),
      (r'(?m)^apns_team_id_variable = "\S+"', 'apns_team_id_variable = ""'),
      (r'(?m)^apns_key_path_variable = "\S+"', 'apns_key_path_variable = ""')],
     "enabled needs APNs"),
    ([(r'(?m)^app_bundle_id = "\S+"', 'app_bundle_id = "bot.tickr.widget"')],
     "app_bundle_id must be one of"),
])
def test_enabled_is_refused_without_apns_for_its_app(tmp_path, extra, match):
    text = re.sub(r"(?m)^enabled = false$", "enabled = true",
                  BOT.read_text("utf-8").split("[live_activity]", 1)[1], count=1)
    text = BOT.read_text("utf-8").split("[live_activity]", 1)[0] + "[live_activity]" + text
    assert config_module.load(_copy(tmp_path / "on", text)).live_activity.enabled
    for pattern, value in extra:
        text = re.sub(pattern, value, text, count=1)
    with pytest.raises(config_module.ConfigError, match=match):
        config_module.load(_copy(tmp_path, text))


def test_only_the_watchdog_entry_imports_the_publisher():
    importers = []
    for path in (ROOT / "src" / "tickr").rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text("utf-8"))):
            names = ([a.name for a in node.names] if isinstance(node, ast.Import) else
                     [f"{node.module}.{a.name}" for a in node.names]
                     if isinstance(node, ast.ImportFrom) and node.module else [])
            if any("live_activity" in name for name in names):
                importers.append(path.relative_to(ROOT / "src" / "tickr").as_posix())
    assert importers == ["runtime/__main__.py"]
    source = (ROOT / "src" / "tickr" / "runtime" / "__main__.py").read_text("utf-8")
    watchdog = next(n for n in ast.walk(ast.parse(source))
                    if isinstance(n, ast.FunctionDef) and n.name == "_watchdog")
    assert "live_activity" in ast.unparse(watchdog)  # imported inside the watchdog only


def test_the_bot_never_loads_the_publisher():
    code = ("import sys, tickr.engine.core, tickr.runtime.service, tickr.runtime.__main__; "
            "print('tickr.runtime.live_activity' in sys.modules)")
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                            check=True, cwd=ROOT)
    assert json.loads(result.stdout.strip().lower()) is False
