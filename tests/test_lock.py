from __future__ import annotations

import pytest

from aperture.lock import AlreadyRunning, SingleInstanceLock


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
