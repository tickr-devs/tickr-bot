"""The observers config/bot.toml ships with load, and each differs from the active strategy
only in the values its header names (a variant is a full copy with a new version)."""

from __future__ import annotations

import tomllib
from pathlib import Path

from tickr import config
from tickr.observers.loader import STRATEGIES_DIR, STRATEGY_FILE, load_observers

ROOT = Path(__file__).resolve().parents[2]
CFG = config.load(ROOT / "config" / "bot.toml")


def _values(folder: str) -> dict:
    return tomllib.loads((ROOT / STRATEGIES_DIR / folder / STRATEGY_FILE).read_text("utf-8"))


def _flat(values: dict) -> dict[tuple[str, str], object]:
    return {(section, key): value for section, table in values.items()
            for key, value in table.items()}


def test_shipped_observers_load_with_distinct_identities():
    loaded = load_observers(CFG)
    assert [o.folder for o in loaded] == list(CFG.strategy.observers)
    assert loaded


def test_relative_volume_variants_change_only_that_value_and_the_version():
    active = _flat(_values(CFG.strategy.active))
    for folder in CFG.strategy.observers:
        variant = _flat(_values(folder))
        changed = {k for k in active.keys() | variant.keys() if active.get(k) != variant.get(k)}
        assert changed == {("strategy", "version"), ("indicators", "minimum_relative_volume")}
        assert variant[("indicators", "minimum_relative_volume")] < active[
            ("indicators", "minimum_relative_volume")]
