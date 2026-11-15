"""The apps' translations of the bot's codes (apps/scripts/kit_api_strings.py) stay in step with
the bot: every fence kind and entry-block cause the bot can send has a translated case, and the
committed Swift and catalog match the generator."""

from __future__ import annotations

import ast
import importlib.util
from pathlib import Path

from aperture.api.contract import OWN_TRADE_OPEN, BotState
from aperture.engine.core import Fence

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "apps" / "scripts" / "kit_api_strings.py"
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
