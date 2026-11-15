"""Turn a model answer into a checked proposal: a new strategy folder plus a report.

The answer must match `SCHEMA`. Each change names an existing `[section] key` of the base
strategy.toml (never `[strategy]`: name and version are set here), and a new value written as
a TOML literal of the same type as the current one. The new file is the base file with only
those value lines replaced (comments kept), a bumped version and a header saying it is a
proposal. It must then load through the strategy's own create() (via observers.loader), so a
value the strategy refuses can never become a proposal. Any problem refuses the whole proposal
with its causes; nothing is fixed up or guessed.
"""

from __future__ import annotations

import json
import re
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from aperture.advisor.errors import AdvisorError

IDEA_KEYS = ("name", "hypothesis", "entry_rule", "exit_rule", "filters", "evidence",
             "test_plan")
"""A new strategy idea (all empty = none). Never code: a developer writes the module."""
IDEA_REQUIRED = ("name", "hypothesis", "entry_rule", "evidence")
_IDEA_NAME = re.compile(r"^[a-z][a-z0-9-]*-v[0-9]+$")

SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "changes": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "section": {"type": "string"},
                    "key": {"type": "string"},
                    "new_value": {"type": "string"},
                    "reason": {"type": "string"},
                    "evidence": {"type": "string"},
                },
                "required": ["section", "key", "new_value", "reason", "evidence"],
                "additionalProperties": False,
            },
        },
        "expected_effect": {"type": "string"},
        "risks": {"type": "array", "items": {"type": "string"}},
        "no_change_reason": {"type": "string"},
        "new_strategy_idea": {
            "type": "object",
            "properties": {key: {"type": "string"} for key in IDEA_KEYS},
            "required": list(IDEA_KEYS),
            "additionalProperties": False,
        },
    },
    "required": ["summary", "changes", "expected_effect", "risks", "no_change_reason",
                 "new_strategy_idea"],
    "additionalProperties": False,
}

FIXED_SECTION = "strategy"


@dataclass(frozen=True)
class Change:
    section: str
    key: str
    old: Any
    new: Any
    new_literal: str
    reason: str
    evidence: str


@dataclass(frozen=True)
class Idea:
    name: str
    hypothesis: str
    entry_rule: str
    exit_rule: str
    filters: str
    evidence: str
    test_plan: str


@dataclass(frozen=True)
class Proposal:
    summary: str
    changes: tuple[Change, ...]
    expected_effect: str
    risks: tuple[str, ...]
    no_change_reason: str
    idea: Idea | None = None


def _kind(value: Any) -> str:
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "float"
    if isinstance(value, str):
        return "string"
    return type(value).__name__


def _literal(text: str) -> Any:
    try:
        return tomllib.loads(f"v = {text}")["v"]
    except tomllib.TOMLDecodeError as exc:
        raise ValueError(f"not a TOML value: {text!r}") from exc


def parse_reply(text: str, base: Mapping[str, Any], max_changes: int) -> Proposal:
    """Validate the answer against the base values. AdvisorError lists every cause."""
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise AdvisorError(f"answer is not JSON: {exc.msg}") from exc
    if not isinstance(data, dict) or set(data) != set(SCHEMA["required"]):
        raise AdvisorError("answer does not match the proposal schema")
    raw_changes = data["changes"]
    if not isinstance(raw_changes, list):
        raise AdvisorError("changes is not a list")
    causes: list[str] = []
    if len(raw_changes) > max_changes:
        causes.append(f"{len(raw_changes)} changes proposed, at most {max_changes} allowed")
    changes: list[Change] = []
    seen: set[tuple[str, str]] = set()
    for item in raw_changes:
        if not isinstance(item, dict) or set(item) != {
            "section", "key", "new_value", "reason", "evidence"
        }:
            causes.append(f"malformed change: {item!r}")
            continue
        section, key = item["section"], item["key"]
        where = f"[{section}] {key}"
        if (section, key) in seen:
            causes.append(f"{where} changed twice")
            continue
        seen.add((section, key))
        if section == FIXED_SECTION:
            causes.append(f"{where}: [strategy] name and version are not the model's to change")
            continue
        table = base.get(section)
        if not isinstance(table, dict) or key not in table:
            causes.append(f"{where}: no such key in the base strategy.toml")
            continue
        old = table[key]
        try:
            new = _literal(str(item["new_value"]))
        except ValueError as exc:
            causes.append(f"{where}: {exc}")
            continue
        if _kind(new) != _kind(old):
            causes.append(f"{where}: new value {new!r} is a {_kind(new)}, current is {_kind(old)}")
            continue
        if new == old:
            causes.append(f"{where}: new value equals the current value {old!r}")
            continue
        changes.append(Change(section, key, old, new, str(item["new_value"]).strip(),
                              str(item["reason"]), str(item["evidence"])))
    idea = _idea(data["new_strategy_idea"], causes)
    if not changes and idea is None and not causes and not str(data["no_change_reason"]).strip():
        causes.append("no changes, no idea and no no_change_reason")
    if causes:
        raise AdvisorError("; ".join(causes))
    return Proposal(
        summary=str(data["summary"]),
        changes=tuple(changes),
        expected_effect=str(data["expected_effect"]),
        risks=tuple(str(r) for r in data["risks"]),
        no_change_reason=str(data["no_change_reason"]),
        idea=idea,
    )


def _idea(raw: Any, causes: list[str]) -> Idea | None:
    """The idea, None when every field is empty; a partial or misnamed idea is a cause."""
    if not isinstance(raw, dict) or set(raw) != set(IDEA_KEYS):
        causes.append("new_strategy_idea does not match the schema")
        return None
    values = {k: str(raw[k]).strip() for k in IDEA_KEYS}
    if not any(values.values()):
        return None
    empty = [k for k in IDEA_REQUIRED if not values[k]]
    if empty:
        causes.append(f"new_strategy_idea lacks {', '.join(empty)}")
        return None
    if not _IDEA_NAME.match(values["name"]):
        causes.append(f"new_strategy_idea name {values['name']!r} is not like 'name-v1'")
        return None
    return Idea(**values)


def next_version(strategies_dir: Path, name: str) -> str:
    """One above the highest whole-number version among folders of the same strategy name."""
    versions: list[int] = []
    for path in sorted(strategies_dir.glob("*/strategy.toml")):
        with open(path, "rb") as handle:
            table = tomllib.load(handle).get("strategy", {})
        if table.get("name") != name:
            continue
        version = str(table.get("version"))
        if not version.isdigit():
            raise AdvisorError(f"{path}: version {version!r} is not a whole number")
        versions.append(int(version))
    if not versions:
        raise AdvisorError(f"no strategy folder named {name} under {strategies_dir}")
    return str(max(versions) + 1)


_SECTION = re.compile(r"^\s*\[([^\]]+)\]\s*$")


def _one_line(text: str) -> str:
    return " ".join(text.split())


def render_toml(base_text: str, proposal: Proposal, version: str, header: list[str]) -> str:
    """The base file with only the changed value lines and the version replaced."""
    wanted = {(c.section, c.key): c for c in proposal.changes}
    wanted[(FIXED_SECTION, "version")] = None  # type: ignore[assignment]
    done: set[tuple[str, str]] = set()
    section = None
    out = [f"# {_one_line(line)}" for line in header]
    for line in base_text.splitlines():
        match = _SECTION.match(line)
        if match:
            section = match.group(1).strip()
            out.append(line)
            continue
        key_match = re.match(r"^(\s*)([A-Za-z0-9_-]+)(\s*=\s*)(.*)$", line)
        if section is not None and key_match and (section, key_match.group(2)) in wanted:
            indent, key, eq, _ = key_match.groups()
            change = wanted[(section, key)]
            if change is None:
                out.append(f'{indent}{key}{eq}"{version}"')
            else:
                out.append(f"{indent}# advisor proposal: was {change.old!r}. "
                           f"{_one_line(change.reason)}")
                out.append(f"{indent}{key}{eq}{change.new_literal}")
            done.add((section, key))
            continue
        out.append(line)
    missing = set(wanted) - done
    if missing:
        raise AdvisorError(f"could not place {sorted(missing)} in the base file")
    text = "\n".join(out) + "\n"
    values = tomllib.loads(text)
    expected = tomllib.loads(base_text)
    expected[FIXED_SECTION]["version"] = version
    for change in proposal.changes:
        expected[change.section][change.key] = change.new
    if values != expected:
        raise AdvisorError("rendered strategy.toml does not parse to the proposed values")
    return text
