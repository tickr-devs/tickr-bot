"""A broken replay.toml, calendar or capture file never puts a local path in what the apps read.

Their errors are built in the replay, research and calendar layers with the file's full path
for the command line; the API shows them by type name unless the class is on
tickr.api.causes.REFUSALS (see tests/test_refusal_texts.py).
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from types import SimpleNamespace

import pytest

from tickr.api import jobs, observer_results
from tickr.api.after import Closes
from tickr.api.lab_replay import LabReplay, LabReplayRefused
from tickr.api.observer_results import NoInputs, SimulationPaths

from .samples import T0

DAY = date(2026, 11, 13)


def no_path(text: str, tmp_path: Path) -> None:
    for leaked in (str(tmp_path), tmp_path.name, "/tmp", "replay.toml", "research.toml",
                   "Errno"):
        assert leaked not in text, text


def paths(tmp_path: Path, **overrides) -> SimulationPaths:
    fields = {"capture": tmp_path / "capture", "replay_settings": tmp_path / "replay.toml",
              "calendar": tmp_path / "calendar", "session": SimpleNamespace(),
              "history_sessions": 1} | overrides
    return SimulationPaths(**fields)


@pytest.mark.parametrize("text", ["this is = not [toml", "unknown_key = 1\n", "\xff\xfe"])
def test_a_broken_replay_toml_is_refused_by_type_name(tmp_path, text):
    (tmp_path / "replay.toml").write_text(text, "latin-1")
    with pytest.raises(NoInputs) as refused:
        observer_results.replay_inputs(paths(tmp_path))
    no_path(str(refused.value), tmp_path)
    assert str(refused.value).startswith("replay settings unreadable: ")


def test_a_missing_replay_toml_is_refused_by_type_name(tmp_path):
    with pytest.raises(NoInputs) as refused:
        observer_results.replay_inputs(paths(tmp_path))
    no_path(str(refused.value), tmp_path)


@pytest.mark.parametrize("calendar", ["gone", "empty"])
def test_a_broken_calendar_folder_is_refused_without_its_path(tmp_path, calendar):
    folder = tmp_path / "calendar" if calendar == "empty" else tmp_path / "gone"
    folder.mkdir(exist_ok=True)
    with pytest.raises(NoInputs) as refused:
        observer_results.load_calendar(paths(tmp_path, calendar=folder))
    no_path(str(refused.value), tmp_path)
    assert isinstance(Closes(folder).close(DAY), str)
    no_path(Closes(folder).close(DAY), tmp_path)


def test_jobs_name_a_broken_calendar_without_its_path(tmp_path):
    settings = SimpleNamespace(calendar=tmp_path / "gone", last_entry_minutes_before_close=1,
                               flatten_minutes_before_close=1, half_days_enabled=True)
    times, cause = jobs._session(settings, T0)  # noqa: SLF001
    assert times is None and cause
    no_path(cause, tmp_path)


def test_the_lab_replay_refuses_in_a_broken_calendar_without_its_path(tmp_path):
    lab = object.__new__(LabReplay)
    lab.settings = SimpleNamespace(calendar=tmp_path / "gone")
    with pytest.raises(LabReplayRefused) as refused:
        lab._refuse_in_session(T0)  # noqa: SLF001
    no_path(str(refused.value), tmp_path)


def test_a_bad_capture_file_is_refused_without_its_path(tmp_path):
    from tickr.data.capture import BARS_FILE, CapturedBarsDays

    folder = tmp_path / "capture" / DAY.isoformat()
    folder.mkdir(parents=True)
    (folder / BARS_FILE).write_text("not json\n", "utf-8")
    with pytest.raises(NoInputs) as refused:
        observer_results.day_bars(CapturedBarsDays(), paths(tmp_path), DAY, date(2026, 11, 16))
    no_path(str(refused.value), tmp_path)
    assert "line" in str(refused.value) or BARS_FILE in str(refused.value)
