"""The bot never reaches a pinned training file's command-line entry points (`main`, `test`, the
`refprep`/`day_`/`prefix`/`facts`/`build`... modes): they read files, the environment or the
network, and only the training runs them."""

from __future__ import annotations

import ast
import sys
from pathlib import Path

import model_bars as synthetic
import model_inputs as inputs

from tickr.model import features
from tickr.model.features import full

SRC = Path(full.__file__).parents[2]  # src/tickr
PINNED = features.PINNED_DIR
STEMS = {p.stem for p in PINNED.glob("*.py")}
# every function the pinned files' `__main__` blocks can start
ENTRY = {
    "main",
    "test",
    "selftest",
    "refprep",
    "day_",
    "prefix",
    "facts",
    "coverage",
    "calcheck",
    "keydiff",
    "published",
    "build",
}
HOLDERS = {"feat", "labels", "ref", "tk"}  # fields of the bot's namespace of pinned modules


def _bot_files():
    return [p for p in SRC.rglob("*.py") if PINNED not in p.parents]


def _calls_into_pinned(tree):
    """Entry-point names called on, or imported from, a pinned module."""
    aliases, found = set(STEMS), []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            aliases |= {a.asname for a in node.names if a.name in STEMS and a.asname}
        elif isinstance(node, ast.ImportFrom) and node.module in STEMS:
            found += [a.name for a in node.names if a.name in ENTRY]
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            if node.func.attr not in ENTRY:
                continue
            base = node.func.value
            if (isinstance(base, ast.Name) and base.id in aliases) or (
                isinstance(base, ast.Attribute) and base.attr in HOLDERS
            ):
                found.append(node.func.attr)
    return found


def test_no_bot_module_calls_a_pinned_entry_point():
    assert STEMS >= {"l1feat", "l1labels", "l1bz", "l1fl", "l1tk2", "wide"}
    bad = {
        str(p.relative_to(SRC)): hits
        for p in _bot_files()
        if (hits := _calls_into_pinned(ast.parse(p.read_text("utf-8"))))
    }
    assert not bad, bad


def test_the_scan_sees_a_call_when_there_is_one():
    tree = ast.parse("import l1feat\nfrom l1fl import facts\ncode.labels.build(1)\nl1feat.test()")
    assert sorted(_calls_into_pinned(tree)) == ["build", "facts", "test"]


def test_minute_rows_full_runs_with_every_entry_point_raising(monkeypatch, tmp_path):
    days = synthetic.market(7, last="2024-03-06")
    today, bars = days[-1]
    state = features.history(days[:-1], {}, today, None)
    symbols = ["SPY", "QQQ"] + [f"T{i:03d}" for i in range(12)]
    root = tmp_path / "x"
    root.mkdir()
    given = full.FullInputs(
        tick_file=inputs.tick_file(root / "tk.parquet", symbols, range(300, 420)),
        reference=inputs.reference(root / "ref", today, symbols, [340]),
        benzinga=inputs.benzinga(root / "bz", today, symbols, [340], "2023-01-02"),
        fl_facts=inputs.fl_facts(root / "fl.csv", today, symbols),
    )
    full.full_code()  # imports the pinned modules

    def boom(*_a, **_k):
        raise AssertionError("a pinned entry point was reached")

    patched = 0
    for stem in STEMS:
        module = sys.modules.get(stem)
        for name in ENTRY:
            if module is not None and hasattr(module, name):
                monkeypatch.setattr(module, name, boom)
                patched += 1
    assert patched >= 6
    got = full.minute_rows_full(synthetic.cut(bars, today, 340), today, 340, state, None, given)
    assert len(got) > 0
