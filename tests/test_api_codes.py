"""The apps' translations of the bot's codes (apps/scripts/kit_api_strings.py and
kit_reason_strings.py) stay in step with the bot: every fence kind, entry-block cause, gate
reason and fence reason the bot can send has a translated case, a sentence names only values the
bot sends, and the committed Swift and catalog match the generators."""

from __future__ import annotations

import ast
import importlib.util
import json
import re
from pathlib import Path

from aperture.api import reasons
from aperture.api.contract import OWN_TRADE_OPEN, BotState
from aperture.engine.core import Fence

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "apps" / "scripts" / "kit_api_strings.py"
REASONS_SCRIPT = ROOT / "apps" / "scripts" / "kit_reason_strings.py"
KIT_CATALOG = ROOT / "apps/shared/Sources/ApertureKit/Resources/Localizable.xcstrings"
SERVICE = ROOT / "src" / "aperture" / "runtime" / "service.py"


def load():
    spec = importlib.util.spec_from_file_location("kit_api_strings", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


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


def test_committed_files_match_the_generator():
    assert load().check() == []


def test_every_fence_kind_is_translated():
    sent = {str(fence).replace("-", "_") for fence in Fence}
    assert load().codes("FenceKind") == sent


def test_every_entry_block_cause_is_translated():
    module = load()
    known = module.codes("BlockCode") | module.codes("FenceKind")
    not_running = {f"bot_{state}" for state in BotState if state is not BotState.RUNNING}
    causes = literal_block_causes()
    assert causes, "the service builds no EntryBlock from a literal: update this test"
    assert causes | not_running | {OWN_TRADE_OPEN} <= known


def load_reasons():
    spec = importlib.util.spec_from_file_location("kit_reason_strings", REASONS_SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def shape_params(shapes) -> dict[str, set[str]]:
    """Each code of `reasons` shapes with the values its pattern can capture."""
    found: dict[str, set[str]] = {}
    for code, pattern in shapes:
        found.setdefault(code, set()).update(pattern.groupindex)
    return found


def test_reason_sentences_match_the_generator():
    assert load_reasons().check() == []


def test_every_gate_and_fence_reason_has_a_sentence_naming_only_values_the_bot_sends():
    module = load_reasons()
    gate = shape_params(reasons._GATE)
    fence = {
        code: names
        for shapes in reasons._FENCE.values()
        for code, names in shape_params(shapes).items()
    }
    for sent, sentences in (
        (gate, module.codes("GateReason")),
        (fence, module.codes("FenceReason")),
    ):
        assert set(sentences) == set(sent)
        for code, names in sentences.items():
            assert names <= sent[code], code


def test_every_translation_names_the_same_values_as_the_english():
    strings = json.loads(KIT_CATALOG.read_text(encoding="utf-8"))["strings"]
    token = re.compile(r"\{\w+\}")
    for key, entry in strings.items():
        if not key.startswith(("kit.gate.", "kit.fenceReason.")):
            continue
        units = {lang: entry["localizations"][lang]["stringUnit"]["value"] for lang in ("en", "ru")}
        assert set(token.findall(units["en"])) == set(token.findall(units["ru"])), key
