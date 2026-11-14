"""Run the bot as a child of the launchd job and report how it ended.

launchd stops a job with SIGTERM to the job's process. The supervisor's handler only records the
request (see signals.py); the supervision loop then forwards one SIGTERM to the bot and waits for
it to finish its own clean shutdown. If the bot is still alive `stop_alert_after_seconds` after
the forwarded SIGTERM, `on_stop_overdue` is called once (the caller alerts "bot did not stop")
and the supervisor keeps waiting: launchd's SIGKILL at ExitTimeOut hits the supervisor first,
so this is the last moment anyone can still tell the owner. The supervisor never restarts the
bot: a non-zero exit is reported as a crash (spec section 11: no automatic relaunch after a crash).
"""

from __future__ import annotations

import signal
import subprocess
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from decimal import Decimal

from aperture.runtime.signals import StopRequest

Spawn = Callable[[Sequence[str]], subprocess.Popen[bytes]]
Clock = Callable[[], float]


@dataclass(frozen=True)
class ChildExit:
    return_code: int
    stop_forwarded: bool

    @property
    def clean(self) -> bool:
        return self.return_code == 0

    def describe(self) -> str:
        if self.return_code < 0:
            how = f"killed by signal {signal.Signals(-self.return_code).name}"
        else:
            how = f"exited with code {self.return_code}"
        return f"bot {how}" + (" after a stop request" if self.stop_forwarded else "")


def _spawn(command: Sequence[str]) -> subprocess.Popen[bytes]:
    return subprocess.Popen(list(command))


def supervise(
    command: Sequence[str],
    *,
    poll_seconds: Decimal,
    stop: StopRequest,
    stop_alert_after_seconds: Decimal,
    on_stop_overdue: Callable[[], None],
    spawn: Spawn = _spawn,
    clock: Clock = time.monotonic,
) -> ChildExit:
    """Start `command`, forward at most one SIGTERM when `stop` is requested, wait for the end."""
    if not command:
        raise ValueError("no bot command given")
    child = spawn(command)
    forwarded_at: float | None = None
    overdue_reported = False
    timeout = float(poll_seconds)
    while True:
        try:
            return ChildExit(child.wait(timeout=timeout), forwarded_at is not None)
        except subprocess.TimeoutExpired:
            pass
        if stop.requested and forwarded_at is None:
            child.send_signal(signal.SIGTERM)
            forwarded_at = clock()
        elif (forwarded_at is not None and not overdue_reported
              and clock() - forwarded_at >= float(stop_alert_after_seconds)):
            overdue_reported = True
            on_stop_overdue()
