"""GET /alerts and PUT /alerts/routing: the notification settings (backlog T14).

Where each alert kind goes is the owner's routing file (`runtime/alert_routing.py`); whether a
channel works on this machine comes from the latest preflight's checks (`alert-channel` for
the Mac notification, that and `alert-recipient` for iMessage). Names only: the iMessage
address is never read here.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from tickr.api import contract as api
from tickr.api.system import NO_PREFLIGHT, morning_check
from tickr.config import Config
from tickr.runtime.alert_routing import (
    LOCKED,
    RouteChannel,
    Routing,
    RoutingStore,
    group,
    routing_path,
)
from tickr.runtime.alerts import AlertKind
from tickr.runtime.preflight import CheckName, record_path

# The preflight checks each channel needs to pass; push has no delivery in the bot yet.
NEEDS: dict[RouteChannel, tuple[str, ...]] = {
    RouteChannel.MAC: (CheckName.ALERT_CHANNEL.value,),
    RouteChannel.IMESSAGE: (CheckName.ALERT_CHANNEL.value, CheckName.ALERT_RECIPIENT.value),
}


@dataclass(frozen=True)
class AlertsSettings:
    routing: RoutingStore  # [alerts] routing_file under data_dir, the one writer
    preflight_record: Path  # [runtime] preflight_result_file under data_dir

    @classmethod
    def from_config(cls, config: Config) -> AlertsSettings:
        return cls(RoutingStore(routing_path(config)), record_path(config))


def _channels(record: Path) -> tuple[api.AlertChannelV1, ...]:
    morning, cause = morning_check(record)
    passed = {} if morning is None else {c.name: c.passed for c in morning.checks}
    rows = []
    for channel in RouteChannel:
        needs = NEEDS.get(channel)
        if needs is None:
            rows.append(api.AlertChannelV1(channel, False, api.AlertChannelCause.NOT_BUILT))
        elif morning is None:
            missing = (api.AlertChannelCause.NO_PREFLIGHT if cause == NO_PREFLIGHT
                       else api.AlertChannelCause.PREFLIGHT_UNREADABLE)
            rows.append(api.AlertChannelV1(channel, False, missing))
        elif all(passed.get(name) is True for name in needs):
            rows.append(api.AlertChannelV1(channel, True, None))
        else:
            rows.append(api.AlertChannelV1(channel, False, api.AlertChannelCause.CHECK_FAILED))
    return tuple(rows)


def _kinds(routing: Routing) -> tuple[api.AlertKindV1, ...]:
    return tuple(
        api.AlertKindV1(kind.value, group(kind), kind in LOCKED, api.AlertRoutesV1(
            mac=routing.is_on(kind, RouteChannel.MAC),
            imessage=routing.is_on(kind, RouteChannel.IMESSAGE),
            push=routing.is_on(kind, RouteChannel.PUSH)))
        for kind in AlertKind)


def alerts_document(settings: AlertsSettings, routing: Routing | None = None) -> api.AlertsV1:
    """The settings now; `routing` when the caller just wrote it."""
    routing = settings.routing.read() if routing is None else routing
    return api.AlertsV1(_channels(settings.preflight_record), _kinds(routing), routing.cause)
