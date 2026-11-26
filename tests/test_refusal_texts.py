"""The refusals the API may show by their own text hold no path (tickr.api.causes.REFUSALS).

`safe_cause` keeps the text of the classes listed in REFUSALS and names every other exception by
its type. This guard reads every place such a class is built (`Name(...)` or `module.Name(...)`
in the module that defines it or in a module that imports it) and fails when an argument
interpolates something that carries a local path or a caught exception's text:

- a variable or attribute named like a path (`path`, `file`, `directory`, `root`,
  `captured`, anything ending in `_path`, `_file` or `_dir`), unless only its `.name` is used;
- the caught exception itself (`{exc}`, `str(exc)`), unless under `type()`.

A class with the same name defined elsewhere is never read through another module's list: a file
is read for a class only if it defines it or imports it from its module.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

from tickr.api.causes import REFUSALS, safe_cause

SRC = Path(__file__).resolve().parents[1] / "src"
PATHY = re.compile(r"^(path|file|filename|dir|directory|root|captured|base|target|source|"
                   r".*_(path|file|dir|root))$")
EXCEPTIONS = {"exc", "e", "err", "error", "ex"}
NAME_ONLY = {"name", "stem", "suffix", "parent_name"}  # an attribute that is not a path
# `_cause` is settingsview's own helper: it replaces the path by the file's name.
SAFE_CALLS = {"type", "len", "sorted", "isinstance", "safe_cause", "_cause"}
EXCEPTION_FACTS = {"msg", "lineno", "colno"}  # a parser's fixed phrase and its position


# (file, stripped source line, reason): a site the guard would flag that is fine.
ALLOWED_SITES = [
    ("src/tickr/observers/loader.py",
     'raise ObserverError(f"{folder}: values refused by {module_name}: {exc}") from exc',
     "the strategy module's own validation message about its values, written in this repo; the "
     "owner needs it to fix the strategy's values and it names no file"),
]


def _module_file(module: str) -> Path:
    return SRC.joinpath(*module.split(".")).with_suffix(".py")


def _bad(node: ast.AST) -> str | None:
    """The first path-like or exception name under `node`, or None."""
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) \
            and node.func.id in SAFE_CALLS:
        return None
    if isinstance(node, ast.Attribute):
        if node.attr in NAME_ONLY:
            return None
        if node.attr in EXCEPTION_FACTS and isinstance(node.value, ast.Name) \
                and node.value.id in EXCEPTIONS:
            return None
        if PATHY.match(node.attr):
            return node.attr
    if isinstance(node, ast.Name) and (PATHY.match(node.id) or node.id in EXCEPTIONS):
        return node.id
    for child in ast.iter_child_nodes(node):
        found = _bad(child)
        if found:
            return found
    return None


def _reads(tree: ast.AST, module: str, name: str, defines: bool) -> bool:
    if defines:
        return True
    parent, _, leaf = module.rpartition(".")
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            if node.module == module and any(a.name == name for a in node.names):
                return True
            if node.module == parent and any(a.name == leaf for a in node.names):
                return True
        if isinstance(node, ast.Import) and any(a.name == module for a in node.names):
            return True
    return False


def _sites(qualified: str) -> list[tuple[str, int, str]]:
    module, _, name = qualified.rpartition(".")
    found = []
    for path in sorted(SRC.rglob("*.py")):
        tree = ast.parse(path.read_text("utf-8"))
        defines = path == _module_file(module)
        if not _reads(tree, module, name, defines):
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            called = func.id if isinstance(func, ast.Name) else \
                func.attr if isinstance(func, ast.Attribute) else None
            if called != name:
                continue
            for arg in [*node.args, *(k.value for k in node.keywords)]:
                culprit = _bad(arg)
                line = path.read_text("utf-8").splitlines()[node.lineno - 1].strip()
                if culprit and (str(path.relative_to(SRC.parent)), line) not in {
                        (f, text) for f, text, _ in ALLOWED_SITES}:
                    where = f"{path.relative_to(SRC.parent)}:{node.lineno}"
                    found.append((where, node.lineno, f"{name}(... {culprit} ...)"))
    return found


def test_every_listed_refusal_exists():
    for qualified in REFUSALS:
        module, _, name = qualified.rpartition(".")
        tree = ast.parse(_module_file(module).read_text("utf-8"))
        assert any(isinstance(n, ast.ClassDef) and n.name == name for n in ast.walk(tree)), \
            qualified


def test_no_listed_refusal_is_built_with_a_path_or_a_caught_exceptions_text():
    leaks = [f"{where}: {text}" for q in sorted(REFUSALS) for where, _, text in _sites(q)]
    assert not leaks, "a listed refusal interpolates a path or exception text (use `.name`, " \
        "type(exc).__name__, or take the class off REFUSALS):\n" + "\n".join(leaks)


def test_every_allowed_site_still_matches_a_line():
    for file, text, _ in ALLOWED_SITES:
        lines = [line.strip() for line in (SRC.parent / file).read_text("utf-8").splitlines()]
        assert text in lines, (file, text)


def test_the_guard_sees_what_it_forbids(tmp_path):
    source = ("class Boom(Exception): pass\n"
              "def f(path, exc):\n"
              "    raise Boom(f'{path}: bad')\n"
              "def g(path, exc):\n    raise Boom(f'{path.name}: {type(exc).__name__}')\n"
              "def h(exc):\n    raise Boom(str(exc))\n"
              "def i(self):\n    raise Boom(f'{self.captured}:1')\n")
    tree = ast.parse(source)
    flagged = [n.lineno for n in ast.walk(tree) if isinstance(n, ast.Call)
               and getattr(n.func, "id", "") == "Boom" and _bad(n.args[0])]
    assert flagged == [3, 7, 9]


def test_a_class_that_is_not_listed_shows_its_type_name_only():
    class SettingsError(Exception):
        pass

    assert safe_cause(SettingsError("/Users/owner/replay.toml: invalid TOML")) == "SettingsError"
