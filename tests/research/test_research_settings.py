from __future__ import annotations

import tomllib
from pathlib import Path

import pytest
from research_helpers import HERE

from aperture.config import uncommented_keys
from aperture.research.errors import ResearchSettingsError
from aperture.research.settings import KEYS, load_research_settings, settings_from_mapping

ROOT = HERE.parent.parent
CONFIG = ROOT / "config" / "research.toml"


@pytest.mark.parametrize("path", [CONFIG, HERE / "research_settings.toml"], ids=str)
def test_every_key_is_commented(path: Path):
    assert uncommented_keys(path.read_text("utf-8")) == []


def test_repository_settings_load():
    settings = load_research_settings(CONFIG)
    assert settings.horizons_minutes == (1, 5, 15, 30, 60)
    assert settings.atr_period >= 1


def _table(**changes):
    table = tomllib.loads(CONFIG.read_text("utf-8"))["research"]
    table.update(changes)
    return {"research": table}


@pytest.mark.parametrize(
    ("changes", "cause"),
    [
        ({"horizons_minutes": [5, 1]}, "strictly increase"),
        ({"horizons_minutes": []}, "non-empty list"),
        ({"horizons_minutes": [0, 5]}, "at least 1"),
        ({"atr_period": 1.5}, "must be an integer"),
        ({"session_timezone": "Mars/Base"}, "unknown"),
        ({"surprise": 1}, r"unknown=\['surprise'\]"),
    ],
)
def test_bad_settings_refused(changes, cause):
    with pytest.raises(ResearchSettingsError, match=cause):
        settings_from_mapping(_table(**changes))


def test_missing_key_refused():
    table = _table()
    del table["research"]["atr_period"]
    with pytest.raises(ResearchSettingsError, match=r"missing=\['atr_period'\]"):
        settings_from_mapping(table)
    assert "atr_period" in KEYS
