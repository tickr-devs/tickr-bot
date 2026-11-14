"""SIGTERM / SIGINT -> a stop request the main loop reads.

The handler does no work: it only records that a stop was requested and by which signal. No
locks, no I/O, no logging, no broker calls (a handler can interrupt the main thread anywhere,
including inside a lock it holds). The main loop polls `StopRequest.requested`, stops new entries,
cancels working entries, leaves resting stops in place and returns normally (spec section 11).
"""

from __future__ import annotations

import signal
from collections.abc import Callable, Iterable
from types import FrameType

# Signals that ask the bot to stop: launchd sends SIGTERM; SIGINT is Ctrl-C in a terminal.
STOP_SIGNALS: tuple[signal.Signals, ...] = (signal.SIGTERM, signal.SIGINT)

Handler = Callable[[int, FrameType | None], object] | int | signal.Handlers | None


class StopRequest:
    """Set once by a signal handler; read by the main loop. Never cleared within a process."""

    def __init__(self) -> None:
        self._signal: int | None = None

    @property
    def requested(self) -> bool:
        return self._signal is not None

    @property
    def signal_name(self) -> str | None:
        """Name of the first signal that requested the stop (for the journal), or None."""
        if self._signal is None:
            return None
        return signal.Signals(self._signal).name

    def _handle(self, signum: int, frame: FrameType | None) -> None:
        # Plain attribute assignment only: keep the first signal, ignore repeats.
        if self._signal is None:
            self._signal = signum


def install_stop_handler(
    request: StopRequest, signals: Iterable[signal.Signals] = STOP_SIGNALS
) -> dict[signal.Signals, Handler]:
    """Route `signals` to `request`. Returns the previous handlers for `restore_handlers`.

    Must be called from the main thread (a Python rule for signal.signal).
    """
    previous: dict[signal.Signals, Handler] = {}
    for sig in signals:
        previous[sig] = signal.signal(sig, request._handle)
    return previous


def restore_handlers(previous: dict[signal.Signals, Handler]) -> None:
    for sig, handler in previous.items():
        signal.signal(sig, handler if handler is not None else signal.SIG_DFL)
