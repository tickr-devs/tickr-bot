from __future__ import annotations

import os

import pytest

from tickr.lock import AlreadyRunning, SingleInstanceLock, recorded_holder


def test_second_instance_refused(tmp_path):
    path = tmp_path / "run" / "bot.lock"
    with SingleInstanceLock(path):
        with pytest.raises(AlreadyRunning):
            SingleInstanceLock(path).acquire()
    with SingleInstanceLock(path):
        pass  # released lock can be taken again


def test_stale_file_does_not_block(tmp_path):
    path = tmp_path / "bot.lock"
    path.write_text("99999999")
    with SingleInstanceLock(path):
        assert path.exists()


def test_recorded_holder(tmp_path):
    path = tmp_path / "bot.lock"
    assert recorded_holder(path) is None
    with SingleInstanceLock(path):
        assert recorded_holder(path) == os.getpid()
        # reading never takes the lock: acquiring from elsewhere still fails
        with pytest.raises(AlreadyRunning):
            SingleInstanceLock(path).acquire()
    assert recorded_holder(path) is None  # cleared on clean release
    path.write_text("99999999")  # crashed bot, process gone
    assert recorded_holder(path) is None

