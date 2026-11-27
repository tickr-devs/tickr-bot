"""The harness's disk and memory guard: the machine's own readers answer, and a conversion
folder's peak counts the files the converter removes before its end."""

from __future__ import annotations

import os
from decimal import Decimal

from tickr.model import headroom


def test_the_machine_readers_answer(tmp_path):
    assert headroom.available_memory() > 0
    assert headroom.peak_rss() > 0
    assert headroom.free_bytes(tmp_path) > 0


def test_a_file_removed_inside_a_conversion_counts_in_its_peak(tmp_path):
    limits = headroom.Limits(disk_reserve=1, memory_reserve=1, duckdb_memory=headroom.GB,
                             probe_rows=1000, poll_seconds=float(Decimal(3600)))
    room = headroom.Headroom(tmp_path, limits, free=lambda p: 10**15, memory=lambda: 10**15)
    with room.conversion("kind", 100) as folder:
        (folder / "big").write_bytes(b"x" * 5000)
        os.remove(folder / "big")  # no poll comes in an hour: the removal is sampled
        (folder / "small").write_bytes(b"x" * 10)
    assert room.measured["kind"] == (100, 50.0)
    assert not folder.exists()


def test_memory_falling_below_the_reserve_during_a_conversion_refuses_at_its_end(tmp_path):
    """The check before a conversion passes; memory sampled while it runs falls below the
    reserve, so the conversion refuses when it ends (its result is not used)."""
    import threading

    import pytest

    from tickr.model.errors import ModelRefused

    readings = iter([10**12])  # the check before the conversion
    low = threading.Event()

    def memory():
        return next(readings, 5 if low.is_set() else 10**12)

    limits = headroom.Limits(disk_reserve=1, memory_reserve=100, duckdb_memory=headroom.GB,
                             probe_rows=1000, poll_seconds=float(Decimal("0.01")))
    room = headroom.Headroom(tmp_path, limits, free=lambda p: 10**15, memory=memory)
    with pytest.raises(ModelRefused) as refused, room.conversion("kind", 10):
        low.set()
        threading.Event().wait(float(Decimal("0.1")))  # several samples while it runs
    assert refused.value.cause == "memory_insufficient"
    assert "during the conversion" in refused.value.detail
