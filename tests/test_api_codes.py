"""The apps' translations of the bot's codes (apps/scripts/kit_api_strings.py and
kit_reason_strings.py) stay in step with the bot: every fence kind, entry-block cause, alert
kind, gate reason and fence reason the bot can send has a translated case, a sentence names only
values the bot sends, and the committed Swift and catalog match the generators."""

from __future__ import annotations

import ast
import importlib.util
import json
import re
from pathlib import Path

from tickr.api import reasons
from tickr.api.contract import OWN_TRADE_OPEN, BotState, NotReachedCause
from tickr.data.discovery import Cause
from tickr.engine.core import Fence
from tickr.risk.causes import AdmissionCause
from tickr.runtime.alerts import AlertKind

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "apps" / "scripts" / "kit_api_strings.py"
REASONS_SCRIPT = ROOT / "apps" / "scripts" / "kit_reason_strings.py"
KIT_CATALOG = ROOT / "apps/shared/Sources/TickrKit/Resources/Localizable.xcstrings"
SERVICE = ROOT / "src" / "tickr" / "runtime" / "service.py"


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


def test_every_discovery_cause_and_spike_rule_is_translated():
    module = load()
    assert module.codes("DiscoveryCause") == {c.value for c in Cause}
    source = (ROOT / "src" / "tickr" / "strategy" / "price_action_v3.py").read_text("utf-8")
    rules = set(re.findall(r'"(spike_[a-z_]+)"', source))
    assert rules and rules <= module.codes("CheckRule")


def test_every_not_reached_cause_is_translated():
    assert load().codes("NotReachedCause") == {c.value for c in NotReachedCause}


def test_every_alert_kind_is_translated():
    assert load().codes("AlertKindCode") == {kind.value for kind in AlertKind}


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


def shape_params(shapes) -> dict[str, list[set[str]]]:
    """Each code of `reasons` shapes with, per shape, the values its pattern can capture."""
    found: dict[str, list[set[str]]] = {}
    for code, pattern in shapes:
        found.setdefault(code, []).append(set(pattern.groupindex))
    return found


def assert_sentences_fit_shapes(sent, sentences) -> None:
    """Every shape the bot writes a code in has a sentence naming only values that shape sends
    (so no shape falls back to English), and every sentence fits some shape."""
    assert set(sentences) == set(sent)
    for code, shapes in sent.items():
        for shape in shapes:
            assert any(names <= shape for names in sentences[code]), (code, shape)
        for names in sentences[code]:
            assert any(names <= shape for shape in shapes), (code, names)


def test_reason_sentences_match_the_generator():
    assert load_reasons().check() == []


def test_every_gate_and_fence_shape_has_a_sentence_naming_only_values_the_bot_sends():
    module = load_reasons()
    fence: dict[str, list[set[str]]] = {}
    for shapes in reasons._FENCE.values():
        for code, found in shape_params(shapes).items():
            fence.setdefault(code, []).extend(found)
    assert_sentences_fit_shapes(shape_params(reasons._GATE), module.codes("GateReason"))
    assert_sentences_fit_shapes(fence, module.codes("FenceReason"))


# Refusal details with values but no sentence: their values are English words or text
# (a calendar's own code, the names of missing amounts), so the app shows the bot's words.
REFUSALS_IN_BOT_WORDS = {"CALENDAR_UNKNOWN", "DAILY_MONEY_UNKNOWN"}
# Values reasons.refusal_params adds beyond the recorded detail's shape.
REFUSAL_EXTRA_PARAMS = {"AFTER_LAST_ENTRY": {"time"}, "SYMBOL_COOLDOWN": {"until"}}


def test_every_refusal_shape_has_a_sentence_naming_only_values_the_bot_sends():
    sentences = load_reasons().codes("RefusalReason")
    sent = {
        code: [shape | REFUSAL_EXTRA_PARAMS.get(code, set()) for shape in shape_params(shapes)[""]]
        for code, shapes in reasons._REFUSAL.items()
        if code not in REFUSALS_IN_BOT_WORDS
    }
    for code, extra in REFUSAL_EXTRA_PARAMS.items():
        sent.setdefault(code, [extra])
    assert all(code in AdmissionCause.__members__ for code in sentences)
    assert_sentences_fit_shapes(sent, sentences)


# Causes the API writes itself whose values are English text (a simulation's own outcome words,
# an unexpected error's message), so the app shows the bot's words.
PROSE_IN_BOT_WORDS = {"simulation_outcome", "display_failed"}


# Codes a cause in no known shape reads as, in a field whose writer quotes a deeper layer's
# refusals (reasons.prose_reasons_or): the whole text is their `detail`.
PROSE_FALLBACKS = {reasons.NIGHTLY_REFUSED: [{"detail"}], reasons.ADVISOR_REFUSED: [{"detail"}]}


def test_every_prose_shape_has_a_sentence_naming_only_values_the_bot_sends():
    sent = {
        code: shapes
        for code, shapes in shape_params(reasons._PROSE).items()
        if code not in PROSE_IN_BOT_WORDS
    }
    sent.update(PROSE_FALLBACKS)
    assert_sentences_fit_shapes(sent, load_reasons().codes("ProseReason"))


def test_every_translation_names_the_same_values_as_the_english():
    strings = json.loads(KIT_CATALOG.read_text(encoding="utf-8"))["strings"]
    token = re.compile(r"\{\w+\}")
    for key, entry in strings.items():
        if not key.startswith(("kit.gate.", "kit.fenceReason.", "kit.refusal.", "kit.prose.")):
            continue
        units = {lang: entry["localizations"][lang]["stringUnit"]["value"] for lang in ("en", "es")}
        assert set(token.findall(units["en"])) == set(token.findall(units["es"])), key
