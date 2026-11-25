"""Settings API, read and validate only (backlog S4; design rules W9, W10, W14).

- GET /settings/schema: every owner setting of `tickr.settings_schema` with its kind, unit,
  bounds, step, group, words in English and Spanish, when a change applies, which direction
  needs Face ID and whether it needs retraining; plus the rules across settings.
- GET /settings/values: each schema setting's value in its owner's file as it is on disk now,
  with each file's SHA-256 (a later apply can refuse a draft made against an older file).
- POST /settings/validate: a draft checked against the schema and the files now, with typed
  refusals and, per key, the change's effect. Nothing is written, journaled or applied here:
  apply, history and rollback are later slices of S4.

Owner `platform` is the config file this API was started with; any other owner is a folder
under strategies/. The schema file is read on every request, so a bad edit answers 503 with its
cause instead of stopping the API. Values on the wire are TOML text (`"5"`, `"0.25"`,
`"true"`), the same as GET /config shows them.
"""

from __future__ import annotations

import hashlib
import re
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from tickr import settings_schema as schema
from tickr.api.configview import toml_text
from tickr.api.contract import (
    DECIMAL_PATTERN,
    SettingChangeV1,
    SettingRefusalV1,
    SettingRuleV1,
    SettingsDraftV1,
    SettingsFileV1,
    SettingSpecV1,
    SettingsSchemaV1,
    SettingsValuesV1,
    SettingValueV1,
)
from tickr.observers.loader import STRATEGIES_DIR, STRATEGY_FILE
from tickr.settings_schema import Cause, Kind, Refusal, Setting

_INT = re.compile(r"^-?[0-9]+$")
_DECIMAL = re.compile(DECIMAL_PATTERN)
_BOOLS = {"true": True, "false": False}  # TOML's two booleans (protocol constants)
CHANGE_FIELDS = frozenset({"owner", "key", "value"})


class SettingsUnavailable(Exception):
    """The schema file or an owner's file cannot be read now; the message is the one cause."""


class BadDraft(ValueError):
    """The request body is not a draft of the documented shape; the message is the cause."""


@dataclass(frozen=True)
class OwnerFile:
    owner: str
    path: Path
    shown: str  # relative to the bot's folder: the full path never leaves the API
    digest: str
    values: dict[str, Any]  # the whole file as dotted keys (rules may read keys outside it)


def load_schema(path: Path) -> tuple[Setting, ...]:
    try:
        return schema.load(path)
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError, schema.SchemaError) as exc:
        raise SettingsUnavailable(f"settings schema unreadable: {_cause(exc, path)}") from exc


def _cause(exc: Exception, path: Path) -> str:
    if isinstance(exc, OSError):
        return type(exc).__name__
    return (str(exc) or type(exc).__name__).replace(str(path), path.name)


def owners_of(settings: tuple[Setting, ...]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(s.owner for s in settings))


def owner_path(owner: str, config_file: Path, root: Path) -> Path:
    if owner == schema.PLATFORM:
        return config_file
    return root / STRATEGIES_DIR / owner / STRATEGY_FILE


def read_owner(owner: str, config_file: Path, root: Path) -> OwnerFile:
    path = owner_path(owner, config_file, root)
    try:
        shown = path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        shown = path.name
    try:
        raw = path.read_bytes()
        values = schema.flatten(tomllib.loads(raw.decode("utf-8"), parse_float=Decimal))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise SettingsUnavailable(f"settings file {shown} unreadable: "
                                  f"{_cause(exc, path)}") from exc
    return OwnerFile(owner, path, shown, hashlib.sha256(raw).hexdigest(), values)


def schema_document(settings: tuple[Setting, ...]) -> SettingsSchemaV1:
    return SettingsSchemaV1(
        settings=tuple(SettingSpecV1(
            owner=s.owner, key=s.key, kind=s.kind, unit=s.unit, group=s.group,
            description_en=s.en, description_es=s.es, applies=s.applies, loosens=s.loosens,
            needs_face_id=s.needs_face_id, requires_retraining=s.requires_retraining,
            minimum=s.minimum, minimum_inclusive=s.minimum_inclusive, maximum=s.maximum,
            step=s.step) for s in settings),
        rules=tuple(SettingRuleV1(
            owner=r.owner, name=r.name, left=r.left, compare=r.compare, right=r.right,
            description_en=r.en, description_es=r.es) for r in schema.RULES))


def values_document(settings: tuple[Setting, ...], config_file: Path,
                    root: Path) -> SettingsValuesV1:
    files = {o: read_owner(o, config_file, root) for o in owners_of(settings)}
    return SettingsValuesV1(
        files=tuple(SettingsFileV1(f.owner, f.shown, f.digest) for f in files.values()),
        values=tuple(SettingValueV1(
            s.owner, s.key, _text(files[s.owner].values.get(s.key))) for s in settings))


def _text(value: Any) -> str | None:
    return None if value is None else toml_text(value)


def parse_draft(body: Mapping[str, Any]) -> tuple[tuple[str, str, str], ...]:
    """`{"changes": [{"owner", "key", "value"}, ...]}`, each a string, at least one, no
    (owner, key) twice; BadDraft otherwise."""
    shape = ('body must be {"changes": [{"owner": "...", "key": "...", "value": "..."}, ...]} '
             "with at least one change, each field a string")
    changes = body.get("changes")
    if set(body) != {"changes"} or not isinstance(changes, list) or not changes:
        raise BadDraft(shape)
    parsed: list[tuple[str, str, str]] = []
    for change in changes:
        if (not isinstance(change, dict) or set(change) != CHANGE_FIELDS
                or not all(isinstance(change[f], str) for f in CHANGE_FIELDS)):
            raise BadDraft(shape)
        entry = (change["owner"], change["key"], change["value"])
        if any(p[:2] == entry[:2] for p in parsed):
            raise BadDraft(f"{entry[0]} {entry[1]} is given twice")
        parsed.append(entry)
    return tuple(parsed)


def _value_of(spec: Setting, text: str) -> int | Decimal | bool | None:
    """The draft text as the setting's kind, or None when it is not written as one."""
    if spec.kind is Kind.BOOL:
        return _BOOLS.get(text)
    if not (_INT if spec.kind is Kind.INT else _DECIMAL).match(text):
        return None
    try:
        value = int(text) if spec.kind is Kind.INT else Decimal(text)
        if spec.step is not None:
            value % spec.step  # noqa: B018 - more digits than the decimal context checks raise
    except (ValueError, InvalidOperation):  # Python's limit on digits
        return None
    return value


def draft_document(settings: tuple[Setting, ...], changes: tuple[tuple[str, str, str], ...],
                   config_file: Path, root: Path) -> SettingsDraftV1:
    """Check `changes` (owner, dotted key, value text) against the schema and the files now.
    Raises SettingsUnavailable when an owner's file cannot be read."""
    owners = owners_of(settings)
    refusals: list[SettingRefusalV1] = []
    effects: list[SettingChangeV1] = []
    for owner in dict.fromkeys(c[0] for c in changes):
        mine = [c for c in changes if c[0] == owner]
        if owner not in owners:
            refusals.extend(SettingRefusalV1(owner, key, Cause.UNKNOWN_KEY,
                                             f"no settings owner {owner}")
                            for _, key, _ in mine)
            continue
        current = read_owner(owner, config_file, root).values
        specs = schema.settings_of(settings, owner)
        draft: dict[str, Any] = {}
        found: list[Refusal] = []
        for _, key, text in mine:
            spec = specs.get(key)
            if spec is None:
                draft[key] = text  # validate refuses it as UNKNOWN_KEY
                continue
            value = _value_of(spec, text)
            if value is None:
                found.append(Refusal(key, Cause.WRONG_TYPE,
                                     f"expected {spec.kind.value} as TOML text with at most the "
                                     f"digits the check can read, got {text!r}"))
                continue
            draft[key] = value
            present = None if key not in current else toml_text(current[key])
            old = None if present is None else _value_of(spec, present)
            if present is None or old is None:
                found.append(Refusal(key, Cause.VALUE_MISSING,
                                     f"{key} has no present {spec.kind.value} value in its file"))
                continue
            effect = schema.change_effect(spec, old, value)
            changed = value != old
            effects.append(SettingChangeV1(
                owner=owner, key=key, current=present, draft=text,
                changed=changed, applies=effect.applies, needs_face_id=effect.needs_face_id,
                requires_retraining=changed and spec.requires_retraining))
        found.extend(schema.validate(settings, owner, draft, current))
        refusals.extend(SettingRefusalV1(owner, r.key, r.cause, r.detail) for r in found)
    return SettingsDraftV1(valid=not refusals, refusals=tuple(refusals), changes=tuple(effects))
