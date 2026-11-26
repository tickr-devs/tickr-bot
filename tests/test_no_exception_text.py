"""No exception text reaches an API response, a document's cause or a log line (CLAUDE.md rule 4).

The text of a system exception carries the absolute local path of a file, an OS message or a
piece of the data (`PermissionError: [Errno 13] ... '/Users/owner/...'`). Under src/tickr/api a
caught exception may be named by its type (`type(exc).__name__`) or passed through
`tickr.api.causes.safe_cause` (a refusal of our own keeps its text, anything else its type name).
This guard parses every module of the package, except `__main__.py` (the CLI writes to the
owner's own terminal), and fails on each place an `except ... as name` handler puts the exception
itself (not `type(exc)`, not `safe_cause(exc)`, not an attribute such as `exc.retry_after`) in
an f-string, `str()`, `repr()`, `.format()` or `%`, or reads its `args`, `strerror`, `filename` or
`msg`. An exception that must stay goes into ALLOWED with its reason; an entry that no longer
matches fails the test, so the list cannot go stale.
"""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
API = ROOT / "src" / "tickr" / "api"
EXEMPT_FILES = {"__main__.py"}  # the CLI's own terminal output, never a response
TEXT_ATTRIBUTES = {"args", "strerror", "filename", "filename2", "msg", "doc", "reason"}

# (file under src/tickr/api, stripped source line, reason). Each entry must still match a line.
ALLOWED: list[tuple[str, str, str]] = [
    ("contract.py", "+ (str(exc) if isinstance(exc, ContractError)",
     "contract.py cannot import safe_cause (causes imports it); a ContractError is our own "
     "refusal, an OSError beside it is shown by its type name"),
    ("decision_cache.py", "bad.append((number, text, str(exc)))",
     "kept inside the cache so it refuses with the text DecisionLog refuses with; it reaches a "
     "response only as a RecordFileError, which safe_cause shows as file name and line"),
    ("configview.py",
     "error = (str(exc) or type(exc).__name__).replace(str(path), shown)",
     "the owner's own config file: its path is replaced by the repo-relative name before it "
     "is shown, and the text is the line and column the owner needs to fix the file"),
    ("configview.py", 'error = error or f"{shown}: {exc}"',
     "a TOML or UTF-8 decode error of the same config file: line, column and the relative name"),
]


def _is(node: ast.AST, name: str) -> bool:
    """Whether `node` is the caught exception itself (not an attribute or a call on it)."""
    return isinstance(node, ast.Name) and node.id == name


def _holds(node: ast.AST, name: str) -> bool:
    """Whether the exception itself is `node` or an element of a tuple or a `%` operand."""
    return _is(node, name) or (isinstance(node, ast.Tuple) and any(_is(e, name) for e in node.elts))


def _hits(path: Path) -> list[tuple[int, str]]:
    source = path.read_text("utf-8")
    lines = source.splitlines()
    found: list[tuple[int, str]] = []
    for handler in (n for n in ast.walk(ast.parse(source)) if isinstance(n, ast.ExceptHandler)):
        name = handler.name
        if name is None:
            continue
        for node in (n for stmt in handler.body for n in ast.walk(stmt)):
            bad = False
            if isinstance(node, ast.FormattedValue):
                bad = _is(node.value, name)
            elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name) \
                    and node.func.id in {"str", "repr", "format"}:
                bad = any(_is(a, name) for a in node.args)
            elif isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) \
                    and node.func.attr == "format":
                bad = any(_is(a, name) for a in node.args)
            elif isinstance(node, ast.BinOp) and isinstance(node.op, ast.Mod):
                bad = _holds(node.right, name)
            elif isinstance(node, ast.Attribute) and node.attr in TEXT_ATTRIBUTES:
                bad = isinstance(node.value, ast.Name) and node.value.id == name
            if bad:
                found.append((node.lineno, lines[node.lineno - 1].strip()))
    return found


def _all_hits() -> list[tuple[str, int, str]]:
    return [(p.name, number, text) for p in sorted(API.glob("*.py"))
            if p.name not in EXEMPT_FILES for number, text in _hits(p)]


def test_no_exception_text_reaches_a_response():
    allowed = {(f, line) for f, line, _ in ALLOWED}
    leaks = [f"{f}:{n}: {text}" for f, n, text in _all_hits() if (f, text) not in allowed]
    assert not leaks, "exception text in the API (use type(exc).__name__ or safe_cause):\n" \
        + "\n".join(leaks)


def test_every_allowed_exception_still_matches_a_line():
    seen = {(f, text) for f, _, text in _all_hits()}
    stale = [(f, line) for f, line, _ in ALLOWED if (f, line) not in seen]
    assert not stale, f"stale ALLOWED entries: {stale}"


def test_the_guard_sees_the_forms_it_forbids(tmp_path):
    bad = tmp_path / "m.py"
    bad.write_text(
        "def f():\n"
        "    try:\n        pass\n    except OSError as exc:\n"
        "        a = f'{exc}'\n        b = str(exc)\n        c = repr(exc)\n"
        "        d = exc.args\n        e = '%s' % exc\n        g = f'{exc!r}'\n"
        "        ok = f'{type(exc).__name__} {safe_cause(exc)}'\n", "utf-8")
    assert [n for n, _ in _hits(bad)] == [5, 6, 7, 8, 9, 10]
