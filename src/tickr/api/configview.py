"""GET /config: the bot's configuration file, read-only, with its validation.

The apps show config/bot.toml as it is on disk: every table, every key with its value and the
comment above it (CLAUDE.md rule 6: every key has one). The file holds only names of credential
variables, never their values (rule 4), so nothing is hidden.

Keys and values come from `tomllib`, never from reading lines, so a multi-line array or string,
a quoted or dotted key or an inline table is shown as TOML reads it; a value is written back as
TOML text. Only the comments come from the lines: the block of `#` lines right above a key, or
its trailing comment, inside its `[table]`. A key whose line the scan cannot place (a dotted
key, a quoted key) shows an empty comment rather than a wrong one. A file TOML cannot parse
shows no sections, only the parse error.

`valid` and `error` come from loading the file now with `config.load`, exactly as the next start
of the bot would; `changed_since_start` says the file is no longer the one this API loaded, so
what the running processes use differs from what is shown until they restart. Nothing here
writes: editing comes later, behind a backup and a restart.
"""

from __future__ import annotations

import hashlib
import json
import re
import tomllib
from datetime import date, datetime, time
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from tickr import config as config_module
from tickr.api.contract import ConfigKeyV1, ConfigSectionV1, ConfigV1
from tickr.config import ConfigError

_HEADER = re.compile(r"^\s*\[\s*([A-Za-z0-9_\-]+(?:\s*\.\s*[A-Za-z0-9_\-]+)*)\s*\]\s*(#.*)?$")
_KEY = re.compile(r"^\s*([A-Za-z0-9_\-]+)\s*=(.*)$")
COMMENT = "#"
TOP = ""  # keys before the first [table] header
TABLE_SEPARATOR = "."


def _trailing_comment(value: str) -> str | None:
    """The comment after a single-line value; a `#` inside a quoted string is part of it."""
    quote: str | None = None
    escaped = False
    for index, char in enumerate(value):
        if quote is None:
            if char == COMMENT:
                return value[index + 1:].strip()
            if char in "\"'":
                quote = char
        elif escaped:
            escaped = False
        elif char == "\\" and quote == '"':
            escaped = True
        elif char == quote:
            quote = None
    return None


def comments_of(text: str) -> dict[tuple[str, str], str]:
    """(table, bare key) -> its comment: the `#` block right above (joined with spaces) and a
    trailing comment. Lines inside a multi-line value never look like a key to TOML, but a scan
    could mistake one; such a "key" is simply never looked up, because keys come from tomllib."""
    found: dict[tuple[str, str], str] = {}
    table = TOP
    block: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith(COMMENT):
            block.append(stripped[len(COMMENT):].strip())
            continue
        header = _HEADER.match(line)
        key = _KEY.match(line)
        if header is not None:
            table = TABLE_SEPARATOR.join(p.strip() for p in header.group(1).split(TABLE_SEPARATOR))
        elif key is not None and (table, key.group(1)) not in found:
            trailing = _trailing_comment(key.group(2))
            found[(table, key.group(1))] = " ".join(p for p in (*block, trailing or "") if p)
        block = []
    return found


def toml_text(value: Any) -> str:
    """A value as TOML text (strings as JSON-style basic strings, which TOML reads back)."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, int | Decimal):
        return str(value)
    if isinstance(value, datetime | date | time):
        return value.isoformat()
    if isinstance(value, list):
        return "[" + ", ".join(toml_text(v) for v in value) + "]"
    if isinstance(value, dict):
        return "{" + ", ".join(f"{json.dumps(k)} = {toml_text(v)}" for k, v in value.items()) + "}"
    raise ValueError(f"not a TOML value: {type(value).__name__}")  # pragma: no cover


def sections_of(text: str) -> tuple[ConfigSectionV1, ...]:
    """Tables in file order (tomllib keeps it); a table's sub-tables follow it as their own
    sections named with dots, as their headers are written; an inline table is a key's value.
    Raises TOMLDecodeError."""
    data = tomllib.loads(text, parse_float=Decimal)
    comments = comments_of(text)
    sections: list[ConfigSectionV1] = []

    def inline(name: str, key: str, value: Any) -> bool:
        # A table written as `key = {...}` is a value of its key (the scan saw its key line);
        # one written under its own [header] is a section.
        return not isinstance(value, dict) or (name, key) in comments

    def walk(name: str, table: dict[str, Any]) -> None:
        keys = [ConfigKeyV1(k, toml_text(v), comments.get((name, k), ""))
                for k, v in table.items() if inline(name, k, v)]
        if keys:
            sections.append(ConfigSectionV1(name, tuple(keys)))
        for k, v in table.items():
            if not inline(name, k, v):
                walk(f"{name}{TABLE_SEPARATOR}{k}" if name else k, v)

    walk(TOP, data)
    return tuple(sections)


def config_document(path: Path, root: Path, loaded_digest: str) -> ConfigV1:
    """The file at `path` now; `root` is what the shown path is relative to. The full path never
    leaves the API (the phone may read this), not even inside an error."""
    raw = path.read_bytes()
    try:
        shown = path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        shown = path.name
    error = None
    try:
        config_module.load(path)
    except (ConfigError, tomllib.TOMLDecodeError, UnicodeDecodeError, InvalidOperation,
            OSError) as exc:
        error = (str(exc) or type(exc).__name__).replace(str(path), shown)
    sections: tuple[ConfigSectionV1, ...] = ()
    try:
        sections = sections_of(raw.decode("utf-8"))
    except (tomllib.TOMLDecodeError, UnicodeDecodeError) as exc:
        error = error or f"{shown}: {exc}"
    digest = hashlib.sha256(raw).hexdigest()
    return ConfigV1(file=shown, digest=digest, changed_since_start=digest != loaded_digest,
                    valid=error is None, error=error, sections=sections)
