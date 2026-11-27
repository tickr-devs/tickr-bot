"""Settings version (backlog S3): what every decision and trade was made under.

The bot computes it; nobody names it. It is a SHA-256 (hex) over a canonical text of two parts:

- `platform`: the resolved values (`tickr.config.Config`, after `config.load`) of the platform
  settings in the settings schema (`tickr.settings_schema.RECORDS`, owner `platform`). The
  schema has no narrower marker for owner-relevant settings, so every platform key it lists
  counts, and the part grows with the schema.
- `model`: what decides entries. With `[model] enabled = true`, the verified bundle's identity
  and manifest hash (`tickr.model.bundle.Bundle.manifest_sha256`; the manifest pins the sha256
  of every model file). With it false, the active strategy's resolved values (`read_values`),
  until backlog R28 removes the hand-written strategies. An observer's part is always its own
  strategy's values.

The canonical text:

- table keys are sorted, so the order of keys in a file does not count;
- numbers are written as their exact decimal value with no trailing zeros or exponent, so
  `1.5`, `1.50` and `15e-1` are one value, and so are `2` and `2.0`;
- strings are quoted, so the number 1 and the text "1" differ; booleans stay booleans;
- a strategy's `[strategy] version` is left out: it is the owner's name for its values, not a
  value. `[strategy] name` is kept (it chooses the module that reads the values).

Nothing is guessed: a value with no canonical form (a TOML date, NaN, infinity, a non-text
key), a schema key the configuration does not have, strategy values without their `[strategy]`
table, or a bundle that is missing or does not verify is a SettingsVersionRefused with one
cause, never an empty or partial version.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from tickr.config import Config
from tickr.model.bundle import bundle_folder, open_bundle
from tickr.model.errors import ModelRefused
from tickr.settings_schema import PLATFORM, RECORDS

IDENTITY_TABLE = "strategy"  # [strategy]: name and version
NAME_KEY = "version"  # the owner's name for a strategy's values, outside the version
# The refusal word for a version recorded before with other contents
# (store.SettingsVersionConflict).
CONFLICT = "settings_version_conflict"


class SettingsVersionRefused(Exception):
    """No settings version can be computed; `cause` is a stable word for records."""

    def __init__(self, cause: str, detail: str) -> None:
        super().__init__(f"{cause}: {detail}")
        self.cause = cause
        self.detail = detail


@dataclass(frozen=True)
class SettingsVersion:
    value: str
    """SHA-256 (64 hex characters) of `contents`: what decisions and trades reference."""
    contents: str
    """The canonical text it was computed from, kept so a version can be read back."""


def platform_part(config: Config) -> dict[str, Any]:
    """Dotted key -> resolved value of every platform setting in the settings schema."""
    part: dict[str, Any] = {}
    for setting in RECORDS:
        if setting.owner != PLATFORM:
            continue
        table, _, key = setting.key.partition(".")
        section = getattr(config, table, None)
        if section is None or not hasattr(section, key):
            raise SettingsVersionRefused("platform_setting_unknown",
                                         f"{setting.key} is not in the configuration")
        part[setting.key] = getattr(section, key)
    return part


def strategy_part(values: Mapping[str, Any]) -> dict[str, Any]:
    """A strategy's resolved values without its `[strategy] version` name."""
    identity = values.get(IDENTITY_TABLE)
    if not isinstance(identity, Mapping):
        raise SettingsVersionRefused("strategy_values_missing",
                                     f"no [{IDENTITY_TABLE}] table in the strategy's values")
    resolved = dict(values)
    resolved[IDENTITY_TABLE] = {k: v for k, v in identity.items() if k != NAME_KEY}
    return {"strategy": resolved}


def model_part(config: Config) -> dict[str, Any]:
    """The configured bundle's identity and manifest hash, after it verifies."""
    try:
        bundle = open_bundle(bundle_folder(config))
    except ModelRefused as exc:
        raise SettingsVersionRefused("model_bundle", str(exc)) from exc
    except OSError as exc:  # an unreadable folder or file: refused like a bundle that fails
        raise SettingsVersionRefused("model_bundle", f"{type(exc).__name__}: {exc}") from exc
    return {"bundle": bundle.identity, "manifest_sha256": bundle.manifest_sha256}


def compute(platform: Mapping[str, Any], model: Mapping[str, Any]) -> SettingsVersion:
    try:
        contents = canonical_text({"platform": platform, "model": model})
    except ValueError as exc:
        raise SettingsVersionRefused("no_canonical_form", str(exc)) from exc
    return SettingsVersion(hashlib.sha256(contents.encode("utf-8")).hexdigest(), contents)


def for_active(config: Config, strategy_values: Mapping[str, Any]) -> SettingsVersion:
    """The settings version entries are decided under: the model's bundle when the model is
    on, otherwise the active strategy's values (`strategy_values`)."""
    model = model_part(config) if config.model.enabled else strategy_part(strategy_values)
    return compute(platform_part(config), model)


def for_observer(platform: Mapping[str, Any],
                 strategy_values: Mapping[str, Any]) -> SettingsVersion:
    """The settings version of an observer strategy's decisions (`platform`: `platform_part`)."""
    return compute(platform, strategy_part(strategy_values))


def canonical_text(value: Any, path: str = "") -> str:
    """One text per value, the same however the value was written (see the module text)."""
    if isinstance(value, Mapping):
        if not all(isinstance(key, str) for key in value):
            raise ValueError(f"{path or 'values'}: a key is not text")
        return "{" + ",".join(
            f"{json.dumps(key, ensure_ascii=False)}:{canonical_text(value[key], f'{path}{key}.')}"
            for key in sorted(value)) + "}"
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float, Decimal)):
        return _number(value, path)
    if isinstance(value, Sequence):
        return "[" + ",".join(canonical_text(item, path) for item in value) + "]"
    raise ValueError(f"{path.rstrip('.') or 'values'}: {type(value).__name__} has no "
                     "canonical form")


def _number(value: int | float | Decimal, path: str) -> str:
    # str(float) is the shortest text that reads back as the same float.
    number = Decimal(value) if isinstance(value, int) else Decimal(str(value))
    if not number.is_finite():
        raise ValueError(f"{path.rstrip('.')}: {value!r} is not a finite number")
    if number.is_zero():
        return "0"
    return format(number.normalize(), "f")
