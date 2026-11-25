"""The owner's alert routing: which alert kinds go to which channel (backlog T14, W11).

A preference about where to be told, never a trading setting: it lives in the data folder
(`[alerts] routing_file` under `[run] data_dir`), written by PUT /alerts/routing and read by
every alerting process on each alert. It only ever turns a (kind, channel) cell off:

- a missing file means every kind on every channel;
- a file that cannot be read, or holds anything but known switchable cells, means the same
  (an alert is never dropped because of this file) and its cause is shown at GET /alerts;
- critical kinds (`LOCKED`) are on everywhere whatever the file says, and a write that turns
  one off is refused (`RoutingRefused`, cause `locked-kind`).

Routable channels are the owner-facing ones (`RouteChannel`); the alerts feed file (a Claude
session on the Mac) is not routable and always gets every alert. A channel without a delivery
in this code yet (`push`) can be switched and stored, and has no effect until it exists.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from tickr.api.contract import AlertChannel as RouteChannel
from tickr.api.contract import AlertGroup, write_atomic
from tickr.config import AlertsConfig, Config
from tickr.runtime.alerts import IMESSAGE, MACOS, AlertKind
from tickr.runtime.watch import ALERT_AT_ONCE, FENCE_ALERTS

ROUTING_SCHEMA_VERSION = 1


# The delivery (its name in `alerts.Channels`) behind each routable channel; push has none yet.
DELIVERY: Mapping[RouteChannel, str] = {RouteChannel.MAC: MACOS,
                                        RouteChannel.IMESSAGE: IMESSAGE}


# Kinds that need the owner now: never switched off on any channel. Every kind an
# alert-at-once fence raises (watch.ALERT_AT_ONCE: operator fences and unreadable orders) is
# added below, so a new such fence is locked without touching this list.
_CRITICAL = frozenset({
    AlertKind.UNPROTECTED_POSITION,
    AlertKind.STOP_REJECTED,
    AlertKind.EXIT_REFUSED,
    AlertKind.UNKNOWN_ORDER_STATUS,  # order conflicts and orders in an unknown state
    AlertKind.FOREIGN_EXPOSURE,
    AlertKind.BROKER_READS_FAILING,
    AlertKind.STREAM_LOST_WITH_POSITION,
    AlertKind.NOT_FLAT_AFTER_FLATTEN,
    AlertKind.BOT_CRASHED,
    AlertKind.BOT_DID_NOT_START,
    AlertKind.BOT_DID_NOT_STOP,
    AlertKind.BOT_DOWN,
    AlertKind.BOT_DOWN_WITH_OPEN_TRADE,
    AlertKind.PREFLIGHT_FAILED,
    AlertKind.TOKEN_NEEDS_CONFIRMATION,
    AlertKind.WATCHDOG_NOT_FLAT,
    AlertKind.WATCHDOG_CANNOT_CHECK,
    AlertKind.WATCHDOG_RESTARTED_BOT,
    AlertKind.WATCHDOG_RESTART_REFUSED,
    AlertKind.WATCHDOG_RESTART_FAILED,
    AlertKind.API_CREDENTIALS_UNEXPECTED,
})
LOCKED: frozenset[AlertKind] = _CRITICAL | frozenset(
    FENCE_ALERTS[fence] for fence in ALERT_AT_ONCE if fence in FENCE_ALERTS)

# Group of every kind that is not locked (locked kinds are `critical`).
_GROUPS: Mapping[AlertKind, AlertGroup] = {
    AlertKind.DAILY_CEILING_REACHED: AlertGroup.TRADING,
    AlertKind.FEES_UNKNOWN: AlertGroup.TRADING,
    AlertKind.FEES_CORRECTED: AlertGroup.TRADING,
    AlertKind.OBSERVERS_STOPPED: AlertGroup.DAY,
    AlertKind.DISK_LOW: AlertGroup.DAY,
}


def group(kind: AlertKind) -> AlertGroup:
    return AlertGroup.CRITICAL if kind in LOCKED else _GROUPS[kind]


class RefusalCause(StrEnum):
    UNKNOWN_KIND = "unknown-kind"
    UNKNOWN_CHANNEL = "unknown-channel"
    LOCKED_KIND = "locked-kind"
    FILE_UNREADABLE = "routing-file-unreadable"


class RoutingRefused(Exception):
    """A routing write that is refused; `cause` is one `RefusalCause`."""

    def __init__(self, cause: RefusalCause, text: str) -> None:
        super().__init__(f"{cause.value}: {text}")
        self.cause = cause


@dataclass(frozen=True)
class Routing:
    """The cells turned off. `cause` is set when the file could not be used (then nothing is
    off)."""

    off: frozenset[tuple[AlertKind, RouteChannel]]
    cause: str | None = None

    def is_on(self, kind: AlertKind, channel: RouteChannel) -> bool:
        return kind in LOCKED or (kind, channel) not in self.off

    def skipped(self, kind: AlertKind) -> frozenset[str]:
        """Delivery names (`alerts.Channels`) to skip for `kind`."""
        return frozenset(DELIVERY[channel] for channel in DELIVERY
                         if not self.is_on(kind, channel))


EVERYTHING_ON = Routing(frozenset())


def _parse(text: str) -> frozenset[tuple[AlertKind, RouteChannel]]:
    """The off cells of a routing file; ValueError naming the first problem."""
    body = json.loads(text)
    if not isinstance(body, dict) or set(body) != {"schema_version", "off"}:
        raise ValueError("keys must be exactly schema_version and off")
    if body["schema_version"] != ROUTING_SCHEMA_VERSION:
        raise ValueError(f"schema_version {body['schema_version']!r}")
    if not isinstance(body["off"], dict):
        raise ValueError("off must be an object")
    off: set[tuple[AlertKind, RouteChannel]] = set()
    for name, channels in body["off"].items():
        kind = AlertKind(name)  # ValueError for an unknown kind
        if kind in LOCKED:
            raise ValueError(f"{name} is locked on")
        if not isinstance(channels, list):
            raise ValueError(f"{name}: channels must be a list")
        for channel in channels:
            off.add((kind, RouteChannel(channel)))
    return frozenset(off)


def _text(off: frozenset[tuple[AlertKind, RouteChannel]]) -> str:
    by_kind: dict[str, list[str]] = {}
    for kind, channel in sorted(off):
        by_kind.setdefault(kind.value, []).append(channel.value)
    return json.dumps({"schema_version": ROUTING_SCHEMA_VERSION, "off": by_kind},
                      indent=2, sort_keys=True) + "\n"


def read_routing(path: Path) -> Routing:
    """The routing in `path`: everything on when the file is missing; everything on with the
    cause when it cannot be read or is out of shape (refused, never partly used). Never
    raises: any error at all (a deeply nested file makes json raise RecursionError) means
    everything on."""
    try:
        text = path.read_text("utf-8")
    except FileNotFoundError:
        return EVERYTHING_ON
    except Exception as exc:  # noqa: BLE001 - any failure means every channel, never a drop
        return Routing(frozenset(), f"{RefusalCause.FILE_UNREADABLE.value}: "
                                    f"{type(exc).__name__}")
    try:
        return Routing(_parse(text))
    except (ValueError, TypeError) as exc:
        return Routing(frozenset(), f"{RefusalCause.FILE_UNREADABLE.value}: {exc}")
    except Exception as exc:  # noqa: BLE001 - see the docstring
        return Routing(frozenset(), f"{RefusalCause.FILE_UNREADABLE.value}: "
                                    f"{type(exc).__name__}")


def routing_path(config: Config) -> Path:
    return config.root / config.run.data_dir / config.alerts.routing_file


def routing_path_in(data_dir: Path, alerts: AlertsConfig) -> Path:
    return data_dir / alerts.routing_file


def skipped_by(path: Path) -> Callable[[AlertKind], frozenset[str]]:
    """`Alerter`'s routing: the file is read on each alert, so a change applies at once. A
    locked kind is decided before any file access (it skips nothing), and any error for the
    other kinds skips nothing too: this never raises."""
    def skipped(kind: AlertKind) -> frozenset[str]:
        if kind in LOCKED:
            return frozenset()
        try:
            return read_routing(path).skipped(kind)
        except Exception:  # noqa: BLE001 - any failure means every channel, never a drop
            return frozenset()
    return skipped


class RoutingStore:
    """Changes one cell at a time, atomically, one writer at a time in this process (the API
    is the only writer)."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.Lock()

    def read(self) -> Routing:
        return read_routing(self.path)

    def set(self, kind_name: str, channel_name: str, on: bool) -> Routing:
        """Turn one cell on or off and return the new routing; `RoutingRefused` with one cause
        for an unknown kind or channel, a locked kind turned off, or a file that cannot be
        read (it is never overwritten: the owner's other choices would be lost)."""
        try:
            kind = AlertKind(kind_name)
        except ValueError:
            raise RoutingRefused(RefusalCause.UNKNOWN_KIND,
                                 f"{kind_name!r} is not an alert kind") from None
        try:
            channel = RouteChannel(channel_name)
        except ValueError:
            raise RoutingRefused(RefusalCause.UNKNOWN_CHANNEL,
                                 f"{channel_name!r} is not a channel") from None
        if kind in LOCKED and not on:
            raise RoutingRefused(RefusalCause.LOCKED_KIND,
                                 f"{kind.value} needs the owner at once and cannot be "
                                 "turned off")
        with self._lock:
            current = self.read()
            if current.cause is not None:
                raise RoutingRefused(RefusalCause.FILE_UNREADABLE,
                                     f"{self.path.name} cannot be used ({current.cause}); "
                                     "delete it to start over with every alert on")
            cell = (kind, channel)
            off = current.off - {cell} if on else current.off | {cell}
            if off != current.off:
                write_atomic(self.path, _text(off))
            return Routing(off)
