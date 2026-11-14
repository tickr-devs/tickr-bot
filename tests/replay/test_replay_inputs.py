from __future__ import annotations

import json
import re
from decimal import Decimal
from pathlib import Path

import pytest
from conftest import HERE

from aperture.replay.bars import load_bars
from aperture.replay.errors import BarFileError, SettingsError
from aperture.replay.settings import KEYS, load_settings, settings_from_mapping

GOOD = {
    "symbol": "ABC",
    "start": "2026-11-10T15:00:00+00:00",
    "open": "10.00",
    "high": "10.05",
    "low": "9.95",
    "close": "10.01",
    "volume": "1200",
}


def write(tmp_path: Path, *records: dict | str) -> Path:
    path = tmp_path / "bars.jsonl"
    lines = [r if isinstance(r, str) else json.dumps(r) for r in records]
    path.write_text("\n".join(lines) + "\n")
    return path


def test_loads_decimal_bars(tmp_path):
    second = dict(GOOD, start="2026-11-10T15:01:00Z")
    bars = load_bars([write(tmp_path, GOOD, "", second)])
    (abc,) = bars.values()
    assert len(abc) == 2
    assert abc[0].close == Decimal("10.01")
    assert abc[0].start.utcoffset().total_seconds() == 0


@pytest.mark.parametrize(
    ("change", "cause"),
    [
        ({"close": 10.01}, "close must be a string"),
        ({"close": "abc"}, "close is not a decimal"),
        ({"close": "NaN"}, "close is not finite"),
        ({"start": "2026-11-10T15:00:00"}, "must be UTC"),
        ({"start": "2026-11-10T10:00:00-05:00"}, "must be UTC"),
        ({"start": "2026-11-10T15:00:30+00:00"}, "whole minute"),
        ({"high": "9.00"}, "high is below"),
        ({"low": "10.02"}, "low is above"),
        ({"volume": "-1"}, "volume must not be negative"),
        ({"open": "0"}, "prices must be positive"),
        ({"symbol": ""}, "symbol"),
        ({"extra": "1"}, "unknown=['extra']"),
    ],
)
def test_bad_bar_is_refused_with_cause(tmp_path, change, cause):
    with pytest.raises(BarFileError, match=re.escape(cause)) as err:
        load_bars([write(tmp_path, dict(GOOD, **change))])
    assert err.value.line == 1


def test_missing_key_refused(tmp_path):
    record = {k: v for k, v in GOOD.items() if k != "volume"}
    with pytest.raises(BarFileError, match="missing=\\['volume'\\]"):
        load_bars([write(tmp_path, record)])


def test_duplicate_or_out_of_order_refused(tmp_path):
    with pytest.raises(BarFileError, match="is not after") as err:
        load_bars([write(tmp_path, GOOD, GOOD)])
    assert err.value.line == 2


def test_invalid_json_refused(tmp_path):
    with pytest.raises(BarFileError, match="invalid JSON"):
        load_bars([write(tmp_path, "{not json")])


def _settings_table() -> dict:
    import tomllib

    return tomllib.loads((HERE / "replay_settings.toml").read_text())


def test_settings_load(settings):
    assert settings.entry_wait_bars == 2
    assert settings.slippage(Decimal("100")) == Decimal("0.1")
    assert settings.slippage(Decimal("1")) == Decimal("0.01")


@pytest.mark.parametrize(
    ("key", "value", "cause"),
    [
        ("entry_wait_bars", 0, "at least 1"),
        ("entry_wait_bars", True, "integer"),
        ("slippage_percent", -1, "negative"),
        ("slippage_percent", "0.1", "number"),
    ],
)
def test_bad_setting_refused(key, value, cause):
    data = _settings_table()
    data["replay"][key] = value
    with pytest.raises(SettingsError, match=cause):
        settings_from_mapping(data)


def test_missing_and_unknown_setting_refused():
    data = _settings_table()
    del data["replay"]["entry_wait_bars"]
    data["replay"]["surprise"] = 1
    with pytest.raises(
        SettingsError, match="missing=\\['entry_wait_bars'\\] unknown=\\['surprise'\\]"
    ):
        settings_from_mapping(data)


def _commented_keys(text: str) -> tuple[set[str], set[str]]:
    lines = text.splitlines()
    keys, commented = set(), set()
    for i, line in enumerate(lines):
        match = re.match(r"^([a-z_]+)\s*=", line)
        if match:
            keys.add(match.group(1))
            if i and lines[i - 1].startswith("# "):
                commented.add(match.group(1))
    return keys, commented


def test_test_settings_every_key_commented():
    keys, commented = _commented_keys((HERE / "replay_settings.toml").read_text())
    assert keys == commented == KEYS


def test_config_replay_toml_is_valid_and_every_key_commented():
    path = HERE.parent.parent / "config" / "replay.toml"
    keys, commented = _commented_keys(path.read_text())
    assert keys == commented == KEYS
    load_settings(path)
