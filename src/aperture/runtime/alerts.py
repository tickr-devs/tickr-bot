"""Owner alerts (spec section 11): a macOS notification now, phone push in the app stage.

`Alerter.alert(kind, text)` never raises and never retries. It returns an `AlertOutcome` that the
caller always writes to its journal, whether the notification was shown, suppressed as a repeat,
or could not be delivered. Repeats of the same kind and text inside the configured window are
suppressed so that a condition that persists does not flood the owner.

Alert text must never contain a secret value: callers pass variable NAMES, never contents.
"""

from __future__ import annotations

import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from typing import Protocol

from aperture.config import AlertsConfig

# Protocol constants of macOS: the AppleScript runner and a script that takes its title and
# text as arguments, so alert text is never parsed as AppleScript.
OSASCRIPT = "/usr/bin/osascript"
_APPLESCRIPT = (
    "on run argv",
    "display notification (item 2 of argv) with title (item 1 of argv)",
    "end run",
)
DARWIN = "darwin"


class AlertKind(StrEnum):
    """Every alert the owner can receive (spec section 11). Values are stable journal names."""

    UNPROTECTED_POSITION = "unprotected-position"
    STOP_REJECTED = "stop-rejected"
    # A closing order was refused; the stop is back and the operator must look.
    EXIT_REFUSED = "exit-refused"
    UNKNOWN_ORDER_STATUS = "unknown-order-status"
    FOREIGN_EXPOSURE = "foreign-exposure"
    BROKER_READS_FAILING = "broker-reads-failing"
    STREAM_LOST_WITH_POSITION = "stream-lost-with-position"
    DAILY_CEILING_REACHED = "daily-ceiling-reached"
    BOT_CRASHED = "bot-crashed"
    BOT_DID_NOT_START = "bot-did-not-start"
    # Added to the spec list by review of #13: SIGTERM forwarded, bot still running.
    BOT_DID_NOT_STOP = "bot-did-not-stop"
    PREFLIGHT_FAILED = "preflight-failed"
    TOKEN_NEEDS_CONFIRMATION = "token-needs-confirmation"


class Delivery(StrEnum):
    SHOWN = "shown"
    SUPPRESSED_REPEAT = "suppressed-repeat"
    NO_NOTIFIER = "no-notifier"
    FAILED = "failed"


class NotifyError(Exception):
    """The notification could not be shown; the message says why."""


class Notifier(Protocol):
    def notify(self, title: str, text: str) -> None:
        """Show one notification or raise NotifyError."""


Runner = Callable[..., subprocess.CompletedProcess[bytes]]


class MacNotifier:
    """macOS notification through osascript. Title and text go in argv, never into the script."""

    def __init__(self, timeout_seconds: Decimal, runner: Runner = subprocess.run) -> None:
        self._timeout = float(timeout_seconds)
        self._runner = runner

    def notify(self, title: str, text: str) -> None:
        argv = [OSASCRIPT]
        for line in _APPLESCRIPT:
            argv += ["-e", line]
        argv += [title, text]
        try:
            result = self._runner(argv, capture_output=True, timeout=self._timeout, check=False)
        except (OSError, subprocess.SubprocessError) as exc:
            raise NotifyError(f"osascript did not run: {type(exc).__name__}") from exc
        if result.returncode != 0:
            raise NotifyError(f"osascript exited with code {result.returncode}")


def default_notifier(config: AlertsConfig, platform: str = sys.platform) -> Notifier | None:
    """The notifier for this machine, or None where there is none (CI, Linux)."""
    if platform == DARWIN:
        return MacNotifier(config.notification_timeout_seconds)
    return None


@dataclass(frozen=True)
class AlertOutcome:
    """What happened to one alert. The caller journals `journal_body()`."""

    kind: AlertKind
    text: str
    at: datetime
    delivery: Delivery
    error: str | None

    def journal_body(self) -> dict[str, str | None]:
        return {
            "kind": self.kind.value,
            "text": self.text,
            "at": self.at.isoformat(),
            "delivery": self.delivery.value,
            "error": self.error,
        }


def _utc_now() -> datetime:
    return datetime.now(UTC)


class Alerter:
    """Shows alerts with repeat suppression. One instance per process."""

    def __init__(
        self,
        config: AlertsConfig,
        title: str,
        notifier: Notifier | None,
        clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        self._window = timedelta(seconds=float(config.dedupe_window_seconds))
        self._title = title
        self._notifier = notifier
        self._clock = clock
        self._last_shown: dict[tuple[AlertKind, str], datetime] = {}

    def alert(self, kind: AlertKind, text: str) -> AlertOutcome:
        if not isinstance(kind, AlertKind):
            raise TypeError(f"kind must be an AlertKind, got {kind!r}")
        now = self._clock()
        key = (kind, text)
        last = self._last_shown.get(key)
        if last is not None and now - last < self._window:
            return AlertOutcome(kind, text, now, Delivery.SUPPRESSED_REPEAT, None)
        if self._notifier is None:
            return AlertOutcome(kind, text, now, Delivery.NO_NOTIFIER, None)
        try:
            self._notifier.notify(f"{self._title}: {kind.value}", text)
        except NotifyError as exc:
            # Not marked as shown: the next occurrence tries again (never an automatic retry).
            return AlertOutcome(kind, text, now, Delivery.FAILED, str(exc))
        self._last_shown[key] = now
        return AlertOutcome(kind, text, now, Delivery.SHOWN, None)
