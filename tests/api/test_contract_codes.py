"""The codes the bot sends, for the apps (tickr-ios): `contract/codes.json`.

The apps put every code the API can send into words (TickrKit's generated Swift and String
Catalog). Their repository checks its translations against this file, the same way it decodes
the fixtures, so the file must list exactly what this bot sends: every fence kind, discovery cause,
not-reached cause, entry-block cause and refusal code, and for each gate, fence, refusal and prose
reason the sets of values its shapes can carry. Regenerate with
`TICKR_WRITE_FIXTURES=1 pytest tests/api/test_contract_codes.py` and review the diff.
"""

from __future__ import annotations

import ast
import json
import os
import re
from pathlib import Path

from tickr.api import reasons
from tickr.api.contract import OWN_TRADE_OPEN, UNKNOWN_CAUSE, BotState, NotReachedCause, RefusalCode
from tickr.data.discovery import Cause
from tickr.engine.core import Fence
from tickr.risk.causes import AdmissionCause

ROOT = Path(__file__).resolve().parents[2]
CODES = ROOT / "contract" / "codes.json"
SERVICE = ROOT / "src" / "tickr" / "runtime" / "service.py"
STRATEGY = ROOT / "src" / "tickr" / "strategy" / "price_action_v3.py"
FORMAT = 1  # version of this file's layout; the apps' check refuses another

# Refusal details with values but no sentence: their values are English words or text
# (a calendar's own code, the names of missing amounts), so the app shows the bot's words.
REFUSALS_IN_BOT_WORDS = {"CALENDAR_UNKNOWN", "DAILY_MONEY_UNKNOWN", "WEEKLY_MONEY_UNKNOWN"}
# Values reasons.refusal_params adds beyond the recorded detail's shape.
REFUSAL_EXTRA_PARAMS = {"AFTER_LAST_ENTRY": {"time"}, "SYMBOL_COOLDOWN": {"until"}}
# Causes the API writes itself whose values are English text (a simulation's own outcome words,
# an unexpected error's message), so the app shows the bot's words.
PROSE_IN_BOT_WORDS = {"simulation_outcome", "display_failed"}
# Codes a cause in no known shape reads as, in a field whose writer quotes a deeper layer's
# refusals (reasons.prose_reasons_or): the whole text is their `detail`.
PROSE_FALLBACKS = {reasons.NIGHTLY_REFUSED: [{"detail"}], reasons.ADVISOR_REFUSED: [{"detail"}]}


def literal_block_causes() -> set[str]:
    """First arguments of `EntryBlock(...)` calls in the service that are plain string literals."""
    causes = set()
    for node in ast.walk(ast.parse(SERVICE.read_text(encoding="utf-8"))):
        if not isinstance(node, ast.Call) or not node.args:
            continue
        name = (
            node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, "id", "")
        )
        first = node.args[0]
        if (
            name == "EntryBlock"
            and isinstance(first, ast.Constant)
            and isinstance(first.value, str)
        ):
            causes.add(first.value)
    return causes


def shape_params(shapes) -> dict[str, list[set[str]]]:
    """Each code of `reasons` shapes with, per shape, the values its pattern can capture."""
    found: dict[str, list[set[str]]] = {}
    for code, pattern in shapes:
        found.setdefault(code, []).append(set(pattern.groupindex))
    return found


def as_json(shapes: dict[str, list[set[str]]]) -> dict[str, list[list[str]]]:
    """Shapes as sorted lists, without repeats, so the file is stable."""
    return {
        code: sorted({tuple(sorted(shape)) for shape in found})
        for code, found in sorted(shapes.items())
    }


def document() -> dict:
    causes = literal_block_causes()
    assert causes, "the service builds no EntryBlock from a literal: update this test"
    not_running = {f"bot_{state}" for state in BotState if state is not BotState.RUNNING}
    fence: dict[str, list[set[str]]] = {}
    for shapes in reasons._FENCE.values():
        for code, found in shape_params(shapes).items():
            fence.setdefault(code, []).extend(found)
    refusal = {
        code: [shape | REFUSAL_EXTRA_PARAMS.get(code, set()) for shape in shape_params(shapes)[""]]
        for code, shapes in reasons._REFUSAL.items()
        if code not in REFUSALS_IN_BOT_WORDS
    }
    for code, extra in REFUSAL_EXTRA_PARAMS.items():
        refusal.setdefault(code, [extra])
    prose = {
        code: shapes
        for code, shapes in shape_params(reasons._PROSE).items()
        if code not in PROSE_IN_BOT_WORDS
    }
    prose.update(PROSE_FALLBACKS)
    rules = set(re.findall(r'"(spike_[a-z_]+)"', STRATEGY.read_text("utf-8")))
    assert rules, "the strategy names no spike rule: update this test"
    return {
        "format": FORMAT,
        "fence_kinds": sorted(str(fence).replace("-", "_") for fence in Fence),
        "discovery_causes": sorted(c.value for c in Cause),
        "spike_rules": sorted(rules),
        "not_reached_causes": sorted(c.value for c in NotReachedCause),
        "entry_block_causes": sorted(causes | not_running | {OWN_TRADE_OPEN}),
        "admission_causes": sorted(AdmissionCause.__members__),
        # UNKNOWN (a cause the API cannot read exactly) has no words on purpose: the apps show
        # "Other reason" for it, as for a code from a newer bot.
        "refusal_codes": sorted({code.value for code in RefusalCode} - {UNKNOWN_CAUSE}),
        "gate_reasons": as_json(shape_params(reasons._GATE)),
        "fence_reasons": as_json(fence),
        "refusal_reasons": as_json(refusal),
        "prose_reasons": as_json(prose),
    }


def test_codes_file_is_current():
    text = json.dumps(document(), indent=1, sort_keys=True) + "\n"
    if os.environ.get("TICKR_WRITE_FIXTURES") == "1":
        CODES.write_text(text, encoding="utf-8")
    stale = "contract/codes.json is stale: TICKR_WRITE_FIXTURES=1 pytest " + Path(__file__).name
    assert CODES.read_text(encoding="utf-8") == text, stale


def test_every_refusal_reason_is_an_admission_cause():
    assert set(document()["refusal_reasons"]) <= AdmissionCause.__members__.keys()
