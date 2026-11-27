from __future__ import annotations

import re
import tomllib
from dataclasses import replace
from decimal import Decimal
from pathlib import Path

import pytest

from tickr import settings_schema as schema
from tickr.settings_schema import Applies, Cause, Kind

ROOT = Path(__file__).resolve().parent.parent
FILES = {
    schema.PLATFORM: ROOT / "config" / "bot.toml",
    schema.PA3: ROOT / "strategies" / schema.PA3 / "strategy.toml",
}
SCHEMA_FILE = ROOT / "config" / "settings_schema.toml"
SETTINGS = schema.load(SCHEMA_FILE)
_TABLE = re.compile(r"^\[([A-Za-z0-9_.\-]+)\]\s*$")
_KEY = re.compile(r"^([A-Za-z0-9_]+)\s*=(.*)$")


def _current(owner: str) -> dict:
    return schema.flatten(tomllib.loads(FILES[owner].read_text("utf-8")))


def _comments(text: str) -> dict[str, str]:
    """Dotted key -> its comment: the block of comment lines right above it plus an inline one."""
    found: dict[str, str] = {}
    table, block = "", []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            block.append(stripped.lstrip("# "))
            continue
        if header := _TABLE.match(stripped):
            table = header.group(1)
        elif key := _KEY.match(stripped):
            inline = key.group(2).split("#", 1)[1] if "#" in key.group(2) else ""
            found[f"{table}.{key.group(1)}"] = " ".join([*block, inline])
        block = [] if not stripped or not stripped.startswith("#") else block
    return found


IDS = [f"{s.owner}:{s.key}" for s in SETTINGS]


@pytest.mark.parametrize("spec", SETTINGS, ids=IDS)
def test_every_setting_exists_in_its_file_with_its_kind(spec):
    value = _current(spec.owner)[spec.key]
    expected = {Kind.BOOL: (bool,), Kind.INT: (int,), Kind.DECIMAL: (int, float)}[spec.kind]
    assert isinstance(value, expected)
    assert spec.kind is Kind.BOOL or not isinstance(value, bool)


# Keys whose comment names the unit after its first sentence, with the reason.
UNIT_LATER = {
    "indicators.warmup_period_multiple": "the unit is its own second sentence",
    "liquidity.max_spread_ratio": "the [liquidity] table comment comes first",
}


@pytest.mark.parametrize("spec", SETTINGS, ids=IDS)
def test_every_setting_comment_names_its_unit(spec):
    comment = _comments(FILES[spec.owner].read_text("utf-8"))[spec.key].lower()
    if spec.key not in UNIT_LATER:
        comment = comment.split(". ", 1)[0]
    assert any(word in comment for word in schema.UNIT_WORDS[spec.unit]), (spec.unit, comment)


@pytest.mark.parametrize("spec", SETTINGS, ids=IDS)
def test_every_record_is_complete(spec):
    assert spec.en.strip() and spec.es.strip() and spec.group.strip()
    assert (spec.minimum is None) == (spec.kind is Kind.BOOL) == (spec.step is None)
    assert spec.owner == schema.PLATFORM or spec.applies is Applies.WHEN_FLAT


def test_keys_are_unique_and_the_moving_sizing_keys_stay_out():
    keys = [(s.owner, s.key) for s in SETTINGS]
    assert len(keys) == len(set(keys))
    assert not {"risk.planned_loss_usd", "risk.position_notional_cap_usd"} & {k for _, k in keys}
    assert not [k for _, k in keys if k.startswith("sizing.")]


def test_rules_name_schema_keys_on_the_left_and_existing_keys_on_the_right():
    for rule in schema.RULES:
        assert rule.left in schema.settings_of(SETTINGS, rule.owner)
        assert rule.right in _current(rule.owner)
        assert rule.en.strip() and rule.es.strip()


@pytest.mark.parametrize("owner", sorted(FILES))
def test_the_shipped_values_pass(owner):
    current = _current(owner)
    draft = {key: current[key] for key in schema.settings_of(SETTINGS, owner)}
    assert schema.validate(SETTINGS, owner, draft, current) == []


def _causes(owner: str, draft: dict) -> list[tuple[str, Cause]]:
    return [(r.key, r.cause) for r in schema.validate(SETTINGS, owner, draft, _current(owner))]


P = schema.PLATFORM


@pytest.mark.parametrize(("draft", "expected"), [
    ({"risk.surprise": 1}, [("risk.surprise", Cause.UNKNOWN_KEY)]),
    ({"risk.max_open_positions": True}, [("risk.max_open_positions", Cause.WRONG_TYPE)]),
    ({"risk.max_open_positions": 2.0}, [("risk.max_open_positions", Cause.WRONG_TYPE)]),
    ({"risk.max_quote_age_seconds": "5"}, [("risk.max_quote_age_seconds", Cause.WRONG_TYPE)]),
    ({"risk.max_quote_age_seconds": float("nan")},
     [("risk.max_quote_age_seconds", Cause.WRONG_TYPE)]),
    ({"risk.symbol_cooldown_after_loss_enabled": 1},
     [("risk.symbol_cooldown_after_loss_enabled", Cause.WRONG_TYPE)]),
    ({"risk.max_working_entries": 0}, [("risk.max_working_entries", Cause.BELOW_MINIMUM)]),
    ({"risk.max_quote_age_seconds": 0}, [("risk.max_quote_age_seconds", Cause.BELOW_MINIMUM)]),
    ({"risk.max_clock_skew_seconds": 0}, []),
    ({"risk.max_quote_age_seconds": 2.55}, [("risk.max_quote_age_seconds", Cause.OFF_STEP)]),
    ({"risk.max_quote_age_seconds": Decimal("2.5")}, []),
    ({"risk.min_daily_room_usd": 5000}, [("risk.min_daily_room_usd", Cause.RULE_BROKEN)]),
    ({"risk.max_daily_loss_usd": 10}, [("risk.min_daily_room_usd", Cause.RULE_BROKEN)]),
    ({"risk.max_daily_loss_usd": 0}, [("risk.max_daily_loss_usd", Cause.BELOW_MINIMUM)]),
    ({"risk.max_open_positions": 10_000}, [("risk.max_open_positions", Cause.RULE_BROKEN)]),
])
def test_platform_draft_refusals(draft, expected):
    assert _causes(P, draft) == expected


def test_strategy_rule_and_a_missing_rule_value():
    assert _causes(schema.PA3, {"indicators.fast_ema_period": 20}) == [
        ("indicators.fast_ema_period", Cause.RULE_BROKEN)]
    refusals = schema.validate(SETTINGS, P, {"risk.max_open_positions": 2}, {})
    assert [(r.key, r.cause) for r in refusals] == [
        ("market_data.stream_reserved_symbols", Cause.VALUE_MISSING)]


def test_loosening_protection_waits_for_the_next_session_and_needs_face_id():
    specs = schema.settings_of(SETTINGS, P)
    ceiling = specs["risk.max_daily_loss_usd"]
    assert schema.change_effect(ceiling, 5000, 6000) == schema.ChangeEffect(
        Applies.NEXT_SESSION, needs_face_id=True)
    assert schema.change_effect(ceiling, 5000, 4000) == schema.ChangeEffect(
        Applies.AT_ONCE, needs_face_id=False)
    cooldown = specs["risk.symbol_cooldown_after_loss_enabled"]
    assert schema.change_effect(cooldown, True, False).needs_face_id
    assert not schema.change_effect(cooldown, False, True).needs_face_id
    ema = schema.settings_of(SETTINGS, schema.PA3)["indicators.fast_ema_period"]
    assert not ema.needs_face_id
    assert schema.change_effect(ema, 9, 12) == schema.ChangeEffect(
        Applies.WHEN_FLAT, needs_face_id=False)


def _schema_file(tmp_path: Path, text: str) -> Path:
    target = tmp_path / "settings_schema.toml"
    target.write_text(text, "utf-8")
    return target


@pytest.mark.parametrize(("change", "match"), [
    (lambda t: t.replace('[platform."risk.max_daily_loss_usd"]', '[platform."risk.gone"]'),
     "risk.max_daily_loss_usd"),
    (lambda t: t + '\n[platform."risk.symbol_cooldown_after_loss_enabled"]\nstep = 1\n', "on/off"),
    (lambda t: t + '\n[platform."risk.unknown"]\nstep = 1\n', "no record"),
    (lambda t: t.replace("step = 1000", "step = 1000\nfloor = 1", 1), "unknown fields"),
    (lambda t: t.replace("step = 0.5", "step = 0", 1), "above 0"),
    (lambda t: t.replace("step = 0.5", 'step = "0.5"', 1), "finite number"),
])
def test_the_schema_file_is_refused_when_it_does_not_match_the_records(tmp_path, change, match):
    path = _schema_file(tmp_path, change(SCHEMA_FILE.read_text("utf-8")))
    with pytest.raises(schema.SchemaError, match=match):
        schema.load(path)


def test_a_maximum_comes_from_the_schema_file(tmp_path):
    text = SCHEMA_FILE.read_text("utf-8").replace(
        '[platform."risk.max_open_positions"]\n',
        '[platform."risk.max_open_positions"]\nmaximum = 8\n')
    settings = schema.load(_schema_file(tmp_path, text))
    assert schema.settings_of(settings, P)["risk.max_open_positions"].maximum == Decimal(8)
    refusals = schema.validate(settings, P, {"risk.max_open_positions": 9}, _current(P))
    assert [(r.key, r.cause) for r in refusals] == [
        ("risk.max_open_positions", Cause.ABOVE_MAXIMUM)]


@pytest.mark.parametrize("key", ["risk.max_weekly_loss_usd", "risk.stop_out_streak_limit"])
def test_a_loss_lock_turned_off_or_raised_is_a_loosening(key):
    """0 = off (backlog T11): raising the limit or setting it to 0 loosens; turning it on from 0
    or lowering it tightens."""
    lock = schema.settings_of(SETTINGS, P)[key]
    loosen = schema.ChangeEffect(Applies.NEXT_SESSION, needs_face_id=True)
    tighten = schema.ChangeEffect(Applies.AT_ONCE, needs_face_id=False)
    assert lock.needs_face_id
    assert schema.change_effect(lock, 3, 4) == loosen
    assert schema.change_effect(lock, 3, 0) == loosen
    assert schema.change_effect(lock, 0, 3) == tighten
    assert schema.change_effect(lock, 4, 3) == tighten
    assert schema.change_effect(lock, 0, 0) == tighten


CLASSES = schema.load_classes(SCHEMA_FILE)
BOT_KEYS = _current(schema.PLATFORM)


def test_every_key_of_bot_toml_has_a_class():
    unclassed = [k for k in BOT_KEYS if schema.classify(CLASSES, schema.PLATFORM, k) is None]
    assert unclassed == [], "name the table in [classes.tables] of config/settings_schema.toml"


def test_class_lists_name_only_tables_and_keys_that_exist():
    document = tomllib.loads(FILES[schema.PLATFORM].read_text("utf-8"))
    tables = {t for t, v in document.items() if t != "environments"} | {
        f"environments.{e}" for e in document["environments"]}
    assert set(CLASSES.tables) == tables
    assert set(CLASSES.keys) <= set(BOT_KEYS)


def test_owner_keys_are_exactly_the_platform_records():
    owner = {k for k in BOT_KEYS
             if schema.classify(CLASSES, schema.PLATFORM, k) is schema.SettingClass.OWNER}
    assert owner == {s.key for s in SETTINGS if s.owner == schema.PLATFORM}


def test_environments_secrets_and_authorization_are_closed():
    for key in BOT_KEYS:
        closed = schema.classify(CLASSES, schema.PLATFORM, key) is schema.SettingClass.CLOSED
        secretive = key.startswith(("environments.", "run.environment")) or key.endswith(
            "_variable")
        assert closed or not secretive, key


def test_a_new_model_setting_is_owner_once_it_has_a_record():
    # [model] is engineering until a record exists; the mechanism needs no list change.
    assert schema.classify(CLASSES, schema.PLATFORM, "model.enabled") is (
        schema.SettingClass.ENGINEERING)


CLOSED_NAMES = [n for n, c in (*CLASSES.tables.items(), *CLASSES.keys.items())
                if c is schema.SettingClass.CLOSED]


def _record_at(key: str) -> schema.Setting:
    base = next(r for r in schema.RECORDS if r.owner == schema.PLATFORM)
    return replace(base, key=key)


@pytest.mark.parametrize("name", CLOSED_NAMES)
@pytest.mark.parametrize("suffix", ["", ".x", ".deep.er.key"])
def test_a_record_at_or_under_any_closed_name_is_refused_and_classified_closed(
        monkeypatch, name, suffix):
    # Review probe: a record under environments.production loaded and classified as owner.
    key = name + suffix
    monkeypatch.setattr(schema, "RECORDS", (*schema.RECORDS, _record_at(key)))
    with pytest.raises(schema.SchemaError):
        schema.load_classes(SCHEMA_FILE)
    assert schema.classify(CLASSES, schema.PLATFORM, key) is schema.SettingClass.CLOSED


def test_the_review_probe_record_is_refused(monkeypatch):
    key = "environments.production.trading_enabled"
    monkeypatch.setattr(schema, "RECORDS", (*schema.RECORDS, _record_at(key)))
    with pytest.raises(schema.SchemaError, match="closed"):
        schema.load_classes(SCHEMA_FILE)
    assert schema.classify(CLASSES, schema.PLATFORM, key) is schema.SettingClass.CLOSED


@pytest.mark.parametrize("name", CLOSED_NAMES)
def test_every_spelling_of_a_closed_path_is_closed(name):
    parts = name.split(".")
    for key in ('.'.join(f'"{p}"' for p in parts) + ".k",
                " . ".join(parts) + " . k",
                ".".join(f"'{p}'" for p in parts),
                name.upper() + ".K"):
        assert schema.classify(CLASSES, schema.PLATFORM, key) is schema.SettingClass.CLOSED, key


@pytest.mark.parametrize("key", [
    "no_such_table.key", "risk", "", ".", "risk.", ".risk.x", "risk..x", "nope.x\nrun.y"])
def test_a_key_outside_the_classification_has_no_class(key):
    # Fail closed: None is refused by every caller (UNKNOWN_KEY), never defaulted to owner.
    assert schema.classify(CLASSES, schema.PLATFORM, key) is None


@pytest.mark.parametrize("key", [
    'risk."max_open_positions"', "risk.max_open_positions = 1", "risk . max_open_positions",
    "RISK.max_open_positions", 'risk."a\\u0062"'])
def test_only_the_exact_spelling_of_a_record_is_owner(key):
    assert schema.classify(CLASSES, schema.PLATFORM, key) is not schema.SettingClass.OWNER


def test_a_file_key_with_slashes_under_a_closed_table_is_closed():
    keys = [k for k in BOT_KEYS if k.startswith("environments.") and "/" in k]
    assert keys, "bot.toml has route_limits keys with slashes under [environments.*]"
    assert all(schema.classify(CLASSES, schema.PLATFORM, k) is schema.SettingClass.CLOSED
               for k in keys)


def test_a_closed_table_holding_records_is_refused(tmp_path):
    # Take risk out of the engineering list first, so only the closed check can refuse it.
    text = SCHEMA_FILE.read_text("utf-8").replace('  "risk",\n', "", 1).replace(
        "closed_tables = [", 'closed_tables = [\n  "risk",')
    bad = tmp_path / "s.toml"
    bad.write_text(text, "utf-8")
    with pytest.raises(schema.SchemaError, match="closed"):
        schema.load_classes(bad)


def test_a_record_inside_a_closed_list_is_refused(tmp_path):
    bad = tmp_path / "s.toml"
    bad.write_text(SCHEMA_FILE.read_text("utf-8").replace(
        "closed_keys = [", 'closed_keys = [\n  "risk.max_open_positions",'), "utf-8")
    with pytest.raises(schema.SchemaError):
        schema.load_classes(bad)


def test_a_table_in_both_lists_is_refused(tmp_path):
    bad = tmp_path / "s.toml"
    bad.write_text(SCHEMA_FILE.read_text("utf-8").replace(
        "closed_tables = [", 'closed_tables = [\n  "risk",'), "utf-8")
    with pytest.raises(schema.SchemaError):
        schema.load_classes(bad)
