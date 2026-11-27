"""Settings version (backlog S3): platform settings plus what decides entries, one version
however the values are written; anything it cannot compute or that conflicts is refused."""

from __future__ import annotations

import dataclasses
import json
import tomllib
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from tickr import cli, config, settings_version
from tickr.observers.errors import ObserverError
from tickr.observers.loader import (
    LoadedStrategy,
    SettingsVersionError,
    read_values,
    with_settings_version,
)
from tickr.runtime.alerts import AlertKind
from tickr.runtime.service import STRATEGY_NOT_LOADED
from tickr.settings_schema import PLATFORM, RECORDS
from tickr.settings_version import (
    SettingsVersionRefused,
    compute,
    for_active,
    for_observer,
    platform_part,
    strategy_part,
)
from tickr.store import SettingsVersionConflict, Store

ROOT = Path(__file__).resolve().parent.parent
CFG = config.load(ROOT / "config" / "bot.toml")
PLATFORM_VALUES = platform_part(CFG)

BASE = """
[strategy]
name = "price-action-v3"
version = "2"

[indicators]
fast_ema_period = 9
minimum_relative_volume = 1.5
refuse_when_relative_volume_unavailable = true
sessions = [1, 2.0]

[sizing]
planned_loss_usd = 50
"""


def version(text: str, platform=PLATFORM_VALUES) -> str:
    return for_observer(platform, tomllib.loads(text)).value


def test_key_order_and_number_formatting_do_not_change_it():
    reordered = """
[sizing]
planned_loss_usd = 50.0

[indicators]
sessions = [1.0, 2]
refuse_when_relative_volume_unavailable = true
minimum_relative_volume = 1.50
fast_ema_period = 9.0

[strategy]
version = "2"
name = "price-action-v3"
"""
    assert version(BASE) == version(reordered)
    assert version(BASE.replace("1.5", "15e-1")) == version(BASE)
    assert len(version(BASE)) == 64


def test_the_strategy_version_name_is_outside_it_and_every_value_is_inside():
    same = version(BASE)
    assert version(BASE.replace('version = "2"', 'version = "renamed"')) == same
    for old, new in (("1.5", "1.75"), ("= 9", "= 10"), ("true", "false"),
                     ('"price-action-v3"', '"price-action-v2"'), ("[1, 2.0]", "[2, 1]")):
        assert version(BASE.replace(old, new)) != same, (old, new)


def test_a_number_and_its_text_differ_and_a_boolean_is_not_a_number():
    def of(value: object) -> str:
        return compute({}, {"a": {"x": value}}).value

    assert of(1) != of("1")
    assert of(True) != of(1)
    assert of(0.0) == of(-0.0) == of(0)


@pytest.mark.parametrize("bad", [date(2026, 11, 25), float("nan"), float("inf")])
def test_a_value_without_a_canonical_form_is_refused(bad):
    with pytest.raises(SettingsVersionRefused, match="a.x") as exc:
        compute({}, {"a": {"x": bad}})
    assert exc.value.cause == "no_canonical_form"


def test_strategy_values_without_their_identity_table_are_refused_not_hashed_empty():
    with pytest.raises(SettingsVersionRefused) as exc:
        strategy_part({})
    assert exc.value.cause == "strategy_values_missing"


def test_the_platform_part_is_every_platform_key_of_the_schema_with_its_resolved_value():
    keys = {s.key for s in RECORDS if s.owner == PLATFORM}
    assert set(PLATFORM_VALUES) == keys
    assert PLATFORM_VALUES["risk.max_daily_loss_usd"] == CFG.risk.max_daily_loss_usd
    risk = dataclasses.replace(CFG.risk, max_daily_loss_usd=CFG.risk.max_daily_loss_usd + 1)
    changed = platform_part(dataclasses.replace(CFG, risk=risk))
    assert version(BASE, changed) != version(BASE)
    # The same value however it is written in the file (resolved, not the raw TOML).
    risk = dataclasses.replace(CFG.risk, max_daily_loss_usd=Decimal(
        f"{CFG.risk.max_daily_loss_usd}.000"))
    assert version(BASE, platform_part(dataclasses.replace(CFG, risk=risk))) == version(BASE)


def test_a_platform_key_outside_the_schema_does_not_count():
    orders = dataclasses.replace(CFG.orders, **{
        f.name: getattr(CFG.orders, f.name) + 1 for f in dataclasses.fields(CFG.orders)
        if isinstance(getattr(CFG.orders, f.name), int)
        and not isinstance(getattr(CFG.orders, f.name), bool)})
    assert platform_part(dataclasses.replace(CFG, orders=orders)) == PLATFORM_VALUES


def test_a_schema_key_the_configuration_lacks_is_refused():
    broken = dataclasses.replace(CFG, risk=object())
    with pytest.raises(SettingsVersionRefused) as exc:
        platform_part(broken)
    assert exc.value.cause == "platform_setting_unknown"


def test_with_the_model_off_the_active_strategy_values_are_the_model_part():
    values = read_values(ROOT, CFG.strategy.active)
    assert for_active(CFG, values) == for_observer(PLATFORM_VALUES, values)


def model_on(tmp_path: Path, bundle: str) -> config.Config:
    model = dataclasses.replace(CFG.model, enabled=True, bundle=bundle)
    return dataclasses.replace(CFG, model=model, root=tmp_path)


@pytest.mark.parametrize("bundle,cause", [("", "bundle_not_configured"),
                                          ("l1-x", "manifest_missing")])
def test_with_the_model_on_a_missing_bundle_is_refused_never_an_empty_hash(tmp_path, bundle,
                                                                          cause):
    with pytest.raises(SettingsVersionRefused, match=cause) as exc:
        for_active(model_on(tmp_path, bundle), read_values(ROOT, CFG.strategy.active))
    assert exc.value.cause == "model_bundle"


def test_shipped_variants_differ_from_their_base():
    base = version_of_folder("price-action-v3")
    assert version_of_folder("price-action-v3-relvol-1.5") != base


def version_of_folder(folder: str) -> str:
    return for_observer(PLATFORM_VALUES, read_values(ROOT, folder)).value


@pytest.fixture
def store(tmp_path):
    store = Store.open(tmp_path / "t.sqlite3", busy_timeout_seconds=0)
    yield store
    store.close()


def test_a_version_recorded_with_other_contents_is_refused(store):
    store.record_settings_version("aaa", "{1}")
    store.record_settings_version("aaa", "{1}")  # the same contents: fine
    store.record_settings_version("bbb", "{2}")
    with pytest.raises(SettingsVersionConflict, match="aaa"):
        store.record_settings_version("aaa", "{3}")
    rows = store.query("SELECT settings_version, contents FROM settings_versions "
                       "ORDER BY settings_version")
    assert [tuple(r) for r in rows] == [("aaa", "{1}"), ("bbb", "{2}")]


class Named:
    prior_sessions_needed = 0
    name, version = "s", "1"


def test_with_settings_version_records_it_or_refuses_with_a_typed_cause(store):
    loaded = LoadedStrategy("folder", Named(), tomllib.loads(BASE))
    good = for_observer(PLATFORM_VALUES, loaded.values)
    tagged = with_settings_version(loaded, lambda: good, store)
    assert tagged.settings_version == good.value
    (row,) = store.query("SELECT contents FROM settings_versions")
    assert row[0] == good.contents
    with pytest.raises(SettingsVersionError) as exc:
        with_settings_version(loaded, lambda: settings_version.SettingsVersion(
            good.value, "other"), store)
    assert exc.value.cause == "settings_version_conflict"
    assert isinstance(exc.value, ObserverError)  # the bot's entries-off path takes it
    with pytest.raises(SettingsVersionError) as exc:
        with_settings_version(LoadedStrategy("folder", Named()),
                              lambda: for_observer(PLATFORM_VALUES, {}), store)
    assert exc.value.cause == "strategy_values_missing"


class Watch:
    def __init__(self) -> None:
        self.alerts: list[tuple[AlertKind, str]] = []

    def alert(self, kind: AlertKind, text: str) -> None:
        self.alerts.append((kind, text))


def test_a_refused_settings_version_turns_entries_off_loudly_never_silently(store):
    """Fable on #769: a conflicting or missing settings version must not silently switch
    entries off. It is journaled with its typed cause and alerted to the owner."""
    run_id = store.start_run(environment="sandbox", config_digest="d", strategy="s",
                             strategy_version="1", code_version="c")
    watch = Watch()
    exc = SettingsVersionError("folder", "settings_version_conflict", "recorded before")
    cause = cli._entries_off(store, watch, run_id, exc)
    assert "settings_version_conflict" in cause
    (row,) = store.query("SELECT body FROM journal WHERE kind = ?", (STRATEGY_NOT_LOADED,))
    assert json.loads(row[0]) == {"cause": cause,
                                  "settings_version_refused": "settings_version_conflict"}
    assert watch.alerts == [(AlertKind.STRATEGY_NOT_LOADED, f"entries off for this run: {cause}")]
