"""A minimal JSON Schema checker for the subset the contract uses (no third-party dependency).

Supported: type, const, enum, pattern, properties, required, additionalProperties: false,
items, anyOf. Anything else in a schema is an error, so the subset cannot silently grow.
"""

from __future__ import annotations

import re
from typing import Any

_TYPES = {
    "object": lambda v: isinstance(v, dict),
    "array": lambda v: isinstance(v, list),
    "string": lambda v: isinstance(v, str),
    "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
    "boolean": lambda v: isinstance(v, bool),
    "null": lambda v: v is None,
}
_KNOWN = {"$schema", "$id", "title", "type", "const", "enum", "pattern", "properties",
          "required", "additionalProperties", "items", "anyOf"}


def errors(schema: dict[str, Any], value: Any, where: str = "$") -> list[str]:
    unknown = set(schema) - _KNOWN
    if unknown:
        return [f"{where}: unsupported schema keywords {sorted(unknown)}"]
    if "anyOf" in schema:
        if any(not errors(option, value, where) for option in schema["anyOf"]):
            return []
        return [f"{where}: matches no anyOf option"]
    found: list[str] = []
    kind = schema.get("type")
    if kind is not None and not _TYPES[kind](value):
        return [f"{where}: expected {kind}, got {type(value).__name__}"]
    if "const" in schema and value != schema["const"]:
        found.append(f"{where}: expected const {schema['const']!r}")
    if "enum" in schema and value not in schema["enum"]:
        found.append(f"{where}: {value!r} not in enum")
    if "pattern" in schema and not re.search(schema["pattern"], value):
        found.append(f"{where}: {value!r} does not match {schema['pattern']}")
    if kind == "object":
        props = schema.get("properties", {})
        for name in schema.get("required", []):
            if name not in value:
                found.append(f"{where}: missing {name}")
        if schema.get("additionalProperties") is False:
            found += [f"{where}: extra key {k}" for k in value if k not in props]
        for name, sub in props.items():
            if name in value:
                found += errors(sub, value[name], f"{where}.{name}")
    if kind == "array" and "items" in schema:
        for i, item in enumerate(value):
            found += errors(schema["items"], item, f"{where}[{i}]")
    return found
