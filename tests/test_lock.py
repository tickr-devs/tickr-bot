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


def test_held_legacy_lock_refuses_and_releases_own(tmp_path):
    legacy = tmp_path / "old.lock"
    with SingleInstanceLock(legacy):  # a bot of the pre-rename code
        with pytest.raises(AlreadyRunning, match="old.lock"):
            SingleInstanceLock(tmp_path / "new.lock", legacy).acquire()
        with SingleInstanceLock(tmp_path / "new.lock"):
            pass  # the refused attempt did not keep its own lock


def test_unheld_legacy_lock_does_not_block_and_is_kept(tmp_path):
    legacy = tmp_path / "old.lock"
    legacy.write_text("99999999")
    with SingleInstanceLock(tmp_path / "new.lock", legacy):
        assert legacy.exists()
    with SingleInstanceLock(tmp_path / "new.lock", tmp_path / "missing.lock"):
        pass
