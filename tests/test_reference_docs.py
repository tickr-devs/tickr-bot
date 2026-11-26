"""docs/reference.md names every refusal cause and every alert kind the bot has, and nothing else.

Each cause enum has a section whose heading ends with the enum's import path in backticks; the
first column of its table holds one code per row. The codes are read from the enums themselves,
so a new or removed code fails here until the document follows. Links into docs/operations.md
must point at a heading that exists.
"""

from __future__ import annotations

import importlib
import re
from enum import StrEnum
from pathlib import Path

from tickr.risk.causes import ENGINE_REFUSED

ROOT = Path(__file__).resolve().parents[1]
REFERENCE = ROOT / "docs" / "reference.md"
OPERATIONS = ROOT / "docs" / "operations.md"

# Every enum the document covers, by the path its heading names. A code that is not in the
# enum but is recorded as a cause too goes in EXTRA with the enum it belongs to.
SECTIONS = (
    "tickr.risk.causes.AdmissionCause",
    "tickr.engine.core.Fence",
    "tickr.calendar.CalendarCause",
    "tickr.runtime.preflight.PreflightCause",
    "tickr.data.discovery.Cause",
    "tickr.data.discovery.StopCause",
    "tickr.api.contract.NotReachedCause",
    "tickr.data.history.HistoryCause",
    "tickr.settings_schema.Cause",
    "tickr.runtime.alerts.AlertKind",
)
EXTRA = {"tickr.risk.causes.AdmissionCause": {ENGINE_REFUSED}}

HEADING = re.compile(r"^###\s.*`(?P<path>tickr\.[\w.]+)`\s*$")
ROW = re.compile(r"^\|\s*`(?P<code>[^`]+)`\s*\|")
LINK = re.compile(r"\(operations\.md#(?P<anchor>[^)]*)\)")


def documented() -> dict[str, list[str]]:
    sections: dict[str, list[str]] = {}
    current: list[str] | None = None
    for line in REFERENCE.read_text(encoding="utf-8").splitlines():
        if line.startswith("#"):
            match = HEADING.match(line)
            current = sections.setdefault(match["path"], []) if match else None
        elif current is not None and (row := ROW.match(line)):
            current.append(row["code"])
    return sections


def codes_of(path: str) -> set[str]:
    module_name, _, name = path.rpartition(".")
    enum = getattr(importlib.import_module(module_name), name)
    assert issubclass(enum, StrEnum), path
    return {str(member.value) for member in enum} | EXTRA.get(path, set())


def anchor_of(heading: str) -> str:
    """The GitHub anchor of a heading: lower case, punctuation dropped, spaces to hyphens."""
    text = heading.lstrip("#").strip().replace("`", "").lower()
    return re.sub(r"[^\w\- ]", "", text).replace(" ", "-")


def test_the_document_has_a_section_for_every_enum_and_no_other():
    assert set(documented()) == set(SECTIONS)


def test_every_code_is_documented_once_and_none_is_stale():
    sections = documented()
    for path in SECTIONS:
        listed = sections[path]
        expected = codes_of(path)
        assert len(listed) == len(set(listed)), f"{path}: a code is listed twice"
        assert set(listed) >= expected, f"{path}: missing from docs/reference.md: " \
            f"{sorted(expected - set(listed))}"
        assert set(listed) <= expected, f"{path}: in docs/reference.md but not in the code: " \
            f"{sorted(set(listed) - expected)}"


def test_links_into_operations_reach_a_heading():
    anchors = {anchor_of(line) for line in OPERATIONS.read_text(encoding="utf-8").splitlines()
               if line.startswith("#")}
    broken = {m["anchor"] for m in LINK.finditer(REFERENCE.read_text(encoding="utf-8"))
              if m["anchor"] not in anchors}
    assert not broken, f"reference.md links to missing operations.md headings: {sorted(broken)}"
