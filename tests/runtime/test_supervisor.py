from __future__ import annotations

import os
import signal
import subprocess
import sys
from decimal import Decimal

from aperture.runtime.signals import StopRequest, install_stop_handler, restore_handlers
from aperture.runtime.supervisor import ChildExit, supervise

POLL = Decimal("0.05")
NO_ALERT = {"stop_alert_after_seconds": Decimal(60), "on_stop_overdue": lambda: None}
# A child that exits 0 on SIGTERM and otherwise waits for a long time.
CLEAN_ON_TERM = (
    "import signal, sys, time\n"
    "signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))\n"
    "print('ready', flush=True)\n"
    "time.sleep(60)\n"
    "sys.exit(9)\n"
)


def _python(code: str) -> list[str]:
    return [sys.executable, "-c", code]


def test_exit_code_is_reported():
    ended = supervise(_python("import sys; sys.exit(4)"), poll_seconds=POLL, **NO_ALERT,
                      stop=StopRequest())
    assert ended == ChildExit(4, False)
    assert not ended.clean and ended.describe() == "bot exited with code 4"


def test_clean_exit():
    ended = supervise(_python("pass"), poll_seconds=POLL, **NO_ALERT, stop=StopRequest())
    assert ended.clean


def test_stop_request_forwards_one_sigterm():
    stop = StopRequest()
    sent = []

    def spawn(command):
        child = subprocess.Popen(command, stdout=subprocess.PIPE)
        assert child.stdout.readline() == b"ready\n"
        original = child.send_signal

        def send(sig):
            sent.append(sig)
            original(sig)

        child.send_signal = send
        stop._handle(signal.SIGTERM, None)
        return child

    ended = supervise(_python(CLEAN_ON_TERM), poll_seconds=POLL, **NO_ALERT, stop=stop, spawn=spawn)
    assert ended == ChildExit(0, True) and ended.clean
    assert sent == [signal.SIGTERM]


def test_real_sigterm_to_supervisor_reaches_child():
    stop = StopRequest()
    previous = install_stop_handler(stop)

    def spawn(command):
        child = subprocess.Popen(command, stdout=subprocess.PIPE)
        assert child.stdout.readline() == b"ready\n"
        os.kill(os.getpid(), signal.SIGTERM)  # what launchd does to the job process
        return child

    try:
        ended = supervise(_python(CLEAN_ON_TERM), poll_seconds=POLL, **NO_ALERT, stop=stop,
                          spawn=spawn)
    finally:
        restore_handlers(previous)
    assert ended.clean and ended.stop_forwarded


def test_killed_child_described():
    assert ChildExit(-9, False).describe() == "bot killed by signal SIGKILL"


class FakeChild:
    """Stays alive for `alive_polls` waits, then exits with `code`."""

    def __init__(self, alive_polls: int, code: int) -> None:
        self.alive_polls = alive_polls
        self.code = code
        self.signals: list[int] = []

    def wait(self, timeout: float) -> int:
        if self.alive_polls > 0:
            self.alive_polls -= 1
            raise subprocess.TimeoutExpired("bot", timeout)
        return self.code

    def send_signal(self, sig: int) -> None:
        self.signals.append(sig)


class StepClock:
    def __init__(self, step: float) -> None:
        self.now = 0.0
        self.step = step

    def __call__(self) -> float:
        self.now += self.step
        return self.now


def test_bot_that_ignores_sigterm_alerts_once_and_supervisor_keeps_waiting():
    stop = StopRequest()
    stop._handle(signal.SIGTERM, None)
    child = FakeChild(alive_polls=50, code=0)
    overdue = []
    ended = supervise(["bot"], poll_seconds=POLL, stop=stop,
                      stop_alert_after_seconds=Decimal(90),
                      on_stop_overdue=lambda: overdue.append(True),
                      spawn=lambda _: child, clock=StepClock(10.0))
    assert overdue == [True]
    assert child.signals == [signal.SIGTERM]
    assert child.alive_polls == 0 and ended == ChildExit(0, True)


def test_no_overdue_alert_when_bot_stops_in_time():
    stop = StopRequest()
    stop._handle(signal.SIGTERM, None)
    overdue = []
    ended = supervise(["bot"], poll_seconds=POLL, stop=stop,
                      stop_alert_after_seconds=Decimal(90),
                      on_stop_overdue=lambda: overdue.append(True),
                      spawn=lambda _: FakeChild(alive_polls=5, code=0), clock=StepClock(10.0))
    assert overdue == [] and ended.clean


def test_no_overdue_alert_without_stop_request():
    overdue = []
    supervise(["bot"], poll_seconds=POLL, stop=StopRequest(),
              stop_alert_after_seconds=Decimal(1), on_stop_overdue=lambda: overdue.append(True),
              spawn=lambda _: FakeChild(alive_polls=20, code=0), clock=StepClock(10.0))
    assert overdue == []
