"""The observer process: it never keeps the bot from exiting, never blocks the trading
thread when it dies, ends when the bot is gone, and runs without credential variables."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import textwrap
import time
from datetime import UTC, date, datetime, timedelta
from datetime import time as dtime
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

from tickr.observers.loader import load_strategy, parse_modules
from tickr.observers.log import DecisionLog
from tickr.observers.runner import ObserverSet, ProcessSpec
from tickr.strategy.base import SymbolContext
from tickr.types import Bar

ROOT = Path(__file__).resolve().parents[2]
M1 = timedelta(minutes=1)
START = datetime.combine(date(2026, 11, 10), dtime(10, 0), ZoneInfo("America/New_York")
                         ).astimezone(UTC)
SECRET = "TICKR_TEST_SECRET_VARIABLE"

MODULE = '''
import os
from tickr.strategy.base import Decision, Verdict

class Probe:
    name, version, prior_sessions_needed = "probe", "1", 0

    def __init__(self, values):
        self.hang = values["probe"]["hang"]

    def evaluate(self, context):
        if os.environ.get("TICKR_TEST_SECRET_VARIABLE") is not None:
            raise RuntimeError("a credential variable is visible")
        while self.hang:
            pass
        return Decision(self.name, self.version, context.symbol, Verdict.IGNORE, ())

def create(values):
    return Probe(values)
'''


def _setup(tmp_path: Path, hang: bool = False) -> tuple[ProcessSpec, tuple[str, ...]]:
    (tmp_path / "probe_strategy.py").write_text(MODULE)
    folder = tmp_path / "strategies" / "probe"
    folder.mkdir(parents=True)
    folder.joinpath("strategy.toml").write_text(
        f'[strategy]\nname = "probe"\nversion = "1"\n[probe]\nhang = {str(hang).lower()}\n')
    modules = ("probe=probe_strategy",)
    return ProcessSpec(tmp_path, modules, tmp_path / "decisions", frozenset({SECRET})), modules


def _bars(n: int, symbol: str = "ABC") -> tuple[Bar, ...]:
    p = Decimal(10)
    return tuple(Bar(symbol, START + i * M1, p, p, p, p, Decimal(100)) for i in range(n))


def _set(tmp_path, spec, modules, queue_size=8):
    sys.path.insert(0, str(tmp_path))  # the spawned child gets the parent's sys.path
    try:
        loaded = [load_strategy(tmp_path, "probe", parse_modules(modules))]
    finally:
        sys.path.remove(str(tmp_path))
    sys.path.insert(0, str(tmp_path))
    return ObserverSet(loaded, DecisionLog(spec.decisions), Decimal(5), queue_size, Decimal(5),
                       Decimal(1), process=spec)


def test_the_child_runs_without_the_credential_variables(tmp_path, monkeypatch):
    spec, modules = _setup(tmp_path)
    monkeypatch.setenv(SECRET, "x")
    observers = _set(tmp_path, spec, modules)
    try:
        assert observers.on_evaluation(SymbolContext("ABC", _bars(3)), START + 3 * M1) == ()
        assert observers.wait_idle(30)
        assert observers.take_failures() == ()
        assert observers.close(30)
    finally:
        sys.path.remove(str(tmp_path))


def test_a_dead_child_never_blocks_the_caller(tmp_path):
    spec, modules = _setup(tmp_path)
    observers = _set(tmp_path, spec, modules)
    try:
        for n in range(3, 8):
            observers.on_evaluation(SymbolContext("ABC", _bars(n)), START + n * M1)
        observers._process.kill()
        observers._process.join(10)
        started = time.monotonic()
        failures = observers.take_failures()
        observers.watching(START + 8 * M1, timedelta(minutes=3))
        assert time.monotonic() - started < 1
        assert any("observer process ended" in f.cause for f in failures)
        assert not observers.alive and observers.enabled == ()
        started = time.monotonic()
        observers.wait_idle(1)
        assert time.monotonic() - started < 5
    finally:
        sys.path.remove(str(tmp_path))


def _run(script: str, tmp_path: Path, timeout: float) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "PYTHONPATH": os.pathsep.join([str(tmp_path), str(ROOT / "src")])}
    return subprocess.run([sys.executable, "-c", textwrap.dedent(script)], cwd=tmp_path,
                          env=env, capture_output=True, text=True, timeout=timeout)


PREAMBLE = '''
import os, sys, time
from datetime import UTC, date, datetime, timedelta, time as dtime
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo
from tickr.observers.loader import load_strategy, parse_modules
from tickr.observers.log import DecisionLog
from tickr.observers.runner import ObserverSet, ProcessSpec
from tickr.strategy.base import SymbolContext
from tickr.types import Bar
root = Path.cwd()
spec = ProcessSpec(root, ("probe=probe_strategy",), root / "decisions")
loaded = [load_strategy(root, "probe", parse_modules(spec.modules))]
M1 = timedelta(minutes=1)
NY = ZoneInfo("America/New_York")
START = datetime.combine(date(2026, 11, 10), dtime(10, 0), NY).astimezone(UTC)
def bars(n, symbol):
    p = Decimal(10)
    return tuple(Bar(symbol, START + i * M1, p, p, p, p, Decimal(100)) for i in range(n))
'''


def test_the_bot_exits_although_a_hung_child_left_its_inbox_full(tmp_path):
    _setup(tmp_path, hang=True)
    script = PREAMBLE + '''
if __name__ == "__main__":
    observers = ObserverSet(loaded, DecisionLog(spec.decisions), Decimal(5), 4, Decimal(5),
                            Decimal(1), process=spec)
    # big first messages: whole histories of many symbols, more than the pipe holds
    for i in range(40):
        observers.on_evaluation(SymbolContext(f"S{i}", bars(2000, f"S{i}")), START + 2000 * M1)
    print("closed", observers.close(0.5), flush=True)
'''
    done = _run(script, tmp_path, timeout=60)
    assert done.returncode == 0, done.stderr
    assert "closed False" in done.stdout


def test_the_child_ends_when_the_bot_is_gone(tmp_path):
    _setup(tmp_path)
    script = PREAMBLE + '''
if __name__ == "__main__":
    observers = ObserverSet(loaded, DecisionLog(spec.decisions), Decimal(5), 4, Decimal(5),
                            Decimal(1), process=spec)
    observers.on_evaluation(SymbolContext("ABC", bars(3, "ABC")), START + 3 * M1)
    observers.wait_idle(30)
    print(observers._process.pid, flush=True)
    os._exit(0)  # killed: no shutdown code runs
'''
    done = _run(script, tmp_path, timeout=60)
    assert done.returncode == 0, done.stderr
    pid = int(done.stdout.split()[-1])
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return
        time.sleep(0.2)
    os.kill(pid, signal.SIGKILL)
    raise AssertionError("the observer process outlived the bot")
