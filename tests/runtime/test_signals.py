from __future__ import annotations

import os
import signal

from aperture.runtime.signals import StopRequest, install_stop_handler, restore_handlers


def test_sigterm_sets_flag_and_nothing_else():
    stop = StopRequest()
    previous = install_stop_handler(stop)
    try:
        assert not stop.requested and stop.signal_name is None
        os.kill(os.getpid(), signal.SIGTERM)
        assert stop.requested
        assert stop.signal_name == "SIGTERM"
        os.kill(os.getpid(), signal.SIGINT)  # repeats keep the first signal
        assert stop.signal_name == "SIGTERM"
    finally:
        restore_handlers(previous)
    assert signal.getsignal(signal.SIGTERM) == previous[signal.SIGTERM]


def test_handler_restored_to_default():
    stop = StopRequest()
    previous = install_stop_handler(stop, [signal.SIGUSR1])
    assert signal.getsignal(signal.SIGUSR1) == stop._handle
    restore_handlers(previous)
    assert signal.getsignal(signal.SIGUSR1) == signal.SIG_DFL
