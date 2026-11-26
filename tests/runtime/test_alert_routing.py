"""The owner's alert routing (backlog T14): a preference that can only turn switchable kinds
off, never a critical one, and never drops an alert because the file is missing or bad."""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from tickr.api.contract import AlertGroup
from tickr.engine.core import OPERATOR_FENCES, Fence
from tickr.runtime.alert_routing import (
    EVERYTHING_ON,
    LOCKED,
    RefusalCause,
    RouteChannel,
    RoutingRefused,
    RoutingStore,
    group,
    read_routing,
    skipped_by,
)
from tickr.runtime.alerts import FEED, IMESSAGE, MACOS, Alerter, AlertKind, Channels, Delivery
from tickr.runtime.watch import ALERT_AT_ONCE, FENCE_ALERTS

from .test_alerts import CONFIG, Recorder

T0 = datetime(2026, 11, 16, 14, 30, tzinfo=UTC)


def test_every_alert_at_once_fence_and_order_conflict_is_locked():
    assert {FENCE_ALERTS[f] for f in ALERT_AT_ONCE if f in FENCE_ALERTS} <= LOCKED
    assert FENCE_ALERTS[Fence.ORDER_CONFLICT] in LOCKED
    assert {FENCE_ALERTS[f] for f in OPERATOR_FENCES if f in FENCE_ALERTS} <= LOCKED
    assert {AlertKind.UNPROTECTED_POSITION, AlertKind.BOT_DOWN,
            AlertKind.BOT_DOWN_WITH_OPEN_TRADE} <= LOCKED


def test_every_kind_has_a_group_and_only_locked_kinds_are_critical():
    for kind in AlertKind:
        assert (group(kind) is AlertGroup.CRITICAL) == (kind in LOCKED)


def test_a_missing_file_means_everything_on(tmp_path):
    routing = read_routing(tmp_path / "none.json")
    assert routing == EVERYTHING_ON
    assert all(routing.is_on(k, c) for k in AlertKind for c in RouteChannel)


@pytest.mark.parametrize("text", [
    "not json",
    json.dumps({"schema_version": 2, "off": {}}),
    json.dumps({"schema_version": 1, "off": {"no-such-kind": ["mac"]}}),
    json.dumps({"schema_version": 1, "off": {"disk-low": ["fax"]}}),
    json.dumps({"schema_version": 1, "off": {"unprotected-position": ["mac"]}}),
    json.dumps({"schema_version": 1, "off": {"disk-low": "mac"}}),
    json.dumps({"schema_version": 1}),
])
def test_a_bad_file_means_everything_on_with_its_cause(tmp_path, text):
    path = tmp_path / "routing.json"
    path.write_text(text, "utf-8")
    routing = read_routing(path)
    assert routing.off == frozenset()
    assert routing.cause is not None and routing.cause.startswith("routing-file-unreadable")


def test_a_cell_turned_off_and_on_again(tmp_path):
    store = RoutingStore(tmp_path / "data" / "routing.json")
    routing = store.set("disk-low", "imessage", False)
    assert not routing.is_on(AlertKind.DISK_LOW, RouteChannel.IMESSAGE)
    assert routing.is_on(AlertKind.DISK_LOW, RouteChannel.MAC)
    assert store.read() == routing
    assert json.loads(store.path.read_text("utf-8")) == {
        "schema_version": 1, "off": {"disk-low": ["imessage"]}}
    assert store.set("disk-low", "imessage", True) == EVERYTHING_ON
    assert store.read() == EVERYTHING_ON


@pytest.mark.parametrize(("kind", "channel", "on", "cause"), [
    ("unprotected-position", "imessage", False, RefusalCause.LOCKED_KIND),
    ("unknown-order-status", "mac", False, RefusalCause.LOCKED_KIND),
    ("bot-down", "mac", False, RefusalCause.LOCKED_KIND),
    ("no-such-kind", "mac", False, RefusalCause.UNKNOWN_KIND),
    ("disk-low", "fax", False, RefusalCause.UNKNOWN_CHANNEL),
])
def test_refused_writes_name_one_cause_and_write_nothing(tmp_path, kind, channel, on, cause):
    store = RoutingStore(tmp_path / "routing.json")
    with pytest.raises(RoutingRefused) as refused:
        store.set(kind, channel, on)
    assert refused.value.cause is cause
    assert not store.path.exists()


def test_a_locked_kind_turned_on_is_accepted_and_stores_nothing(tmp_path):
    store = RoutingStore(tmp_path / "routing.json")
    assert store.set("unprotected-position", "mac", True) == EVERYTHING_ON
    assert not store.path.exists()


def test_an_unreadable_file_is_never_overwritten(tmp_path):
    store = RoutingStore(tmp_path / "routing.json")
    store.path.write_text("{broken", "utf-8")
    with pytest.raises(RoutingRefused) as refused:
        store.set("disk-low", "mac", False)
    assert refused.value.cause is RefusalCause.FILE_UNREADABLE
    assert store.path.read_text("utf-8") == "{broken"


def _alerter(tmp_path, mac, phone, feed):
    channels = Channels(((MACOS, mac), (IMESSAGE, phone), (FEED, feed)))
    return Alerter(CONFIG, "Tickr", channels, lambda: T0,
                   routing=skipped_by(tmp_path / "routing.json"))


def test_the_alerter_skips_the_channels_routed_off_and_says_so(tmp_path):
    mac, phone, feed = Recorder(), Recorder(), Recorder()
    alerter = _alerter(tmp_path, mac, phone, feed)
    RoutingStore(tmp_path / "routing.json").set("disk-low", "imessage", False)
    outcome = alerter.alert(AlertKind.DISK_LOW, "12 MB free")
    assert outcome.delivery is Delivery.SHOWN
    assert outcome.journal_body()["routed_off"] == ["imessage"]
    assert len(mac.shown) == 1 and phone.shown == [] and len(feed.shown) == 1


def test_routed_off_everywhere_is_recorded_and_not_counted_as_shown(tmp_path):
    mac, phone = Recorder(), Recorder()
    channels = Channels(((MACOS, mac), (IMESSAGE, phone)))
    alerter = Alerter(CONFIG, "Tickr", channels, lambda: T0,
                      routing=skipped_by(tmp_path / "routing.json"))
    store = RoutingStore(tmp_path / "routing.json")
    store.set("disk-low", "imessage", False)
    store.set("disk-low", "mac", False)
    outcome = alerter.alert(AlertKind.DISK_LOW, "12 MB free")
    assert outcome.delivery is Delivery.ROUTED_OFF
    assert outcome.journal_body()["routed_off"] == ["macos", "imessage"]
    assert mac.shown == [] and phone.shown == []
    store.set("disk-low", "mac", True)  # applies at the next alert, not after a restart
    assert alerter.alert(AlertKind.DISK_LOW, "12 MB free").delivery is Delivery.SHOWN
    assert len(mac.shown) == 1


def test_a_critical_alert_goes_everywhere_whatever_the_file_says(tmp_path):
    mac, phone, feed = Recorder(), Recorder(), Recorder()
    alerter = _alerter(tmp_path, mac, phone, feed)
    (tmp_path / "routing.json").write_text(json.dumps({
        "schema_version": 1, "off": {"unprotected-position": ["mac", "imessage"]}}), "utf-8")
    outcome = alerter.alert(AlertKind.UNPROTECTED_POSITION, "ABCD")
    assert outcome.delivery is Delivery.SHOWN and "routed_off" not in outcome.journal_body()
    assert len(mac.shown) == len(phone.shown) == len(feed.shown) == 1


DEEP = "[" * 200000  # json.loads raises RecursionError, not ValueError

CORRUPT = {
    "bad-json": "{broken",
    "wrong-types": json.dumps({"schema_version": "1", "off": [["disk-low", "mac"]]}),
    "wrong-inner-types": json.dumps({"schema_version": 1, "off": {"disk-low": [["mac"], {}]}}),
    "nested-garbage": json.dumps({"schema_version": 1, "off": {"disk-low": [[[[[None]]]]]}}),
    "deep-nesting": DEEP,
    "not-utf8": b"\xff\xfe\x00garbage",
}


def _corrupt(tmp_path, shape, monkeypatch):
    path = tmp_path / "routing.json"
    if shape == "directory":
        path.mkdir()
    elif shape == "permission-denied":
        path.write_text("{}", "utf-8")
        real = type(path).read_text

        def refuse(self, *args, **kwargs):
            if self == path:
                raise PermissionError(13, "Permission denied")
            return real(self, *args, **kwargs)
        monkeypatch.setattr(type(path), "read_text", refuse)
    else:
        text = CORRUPT[shape]
        if isinstance(text, bytes):
            path.write_bytes(text)
        else:
            path.write_text(text, "utf-8")


@pytest.mark.parametrize("shape", [*CORRUPT, "directory", "permission-denied"])
@pytest.mark.parametrize("kind", [AlertKind.UNPROTECTED_POSITION, AlertKind.DISK_LOW])
def test_a_corrupt_routing_file_delivers_everywhere_and_never_raises(tmp_path, monkeypatch,
                                                                    shape, kind):
    _corrupt(tmp_path, shape, monkeypatch)
    routing = read_routing(tmp_path / "routing.json")
    assert routing.off == frozenset() and routing.cause is not None
    mac, phone, feed = Recorder(), Recorder(), Recorder()
    outcome = _alerter(tmp_path, mac, phone, feed).alert(kind, "ABCD")
    assert outcome.delivery is Delivery.SHOWN and "routed_off" not in outcome.journal_body()
    assert len(mac.shown) == len(phone.shown) == len(feed.shown) == 1


def test_a_locked_kind_never_touches_the_routing_file(tmp_path, monkeypatch):
    from tickr.runtime import alert_routing

    def opened(_path):
        raise AssertionError("the routing file was read for a locked kind")
    monkeypatch.setattr(alert_routing, "read_routing", opened)
    route = skipped_by(tmp_path / "routing.json")
    for kind in LOCKED:
        assert route(kind) == frozenset()
    assert route(AlertKind.DISK_LOW) == frozenset()  # the read raised: nothing skipped


def test_a_routing_callable_that_raises_skips_nothing():
    mac, phone = Recorder(), Recorder()

    def broken(_kind):
        raise RecursionError("deep")
    alerter = Alerter(CONFIG, "Tickr", Channels(((MACOS, mac), (IMESSAGE, phone))),
                      lambda: T0, routing=broken)
    assert alerter.alert(AlertKind.DISK_LOW, "x").delivery is Delivery.SHOWN
    assert len(mac.shown) == len(phone.shown) == 1
