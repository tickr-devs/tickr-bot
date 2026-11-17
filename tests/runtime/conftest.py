from __future__ import annotations

import importlib
import shutil
import tomllib
from pathlib import Path
from types import ModuleType

import pytest
from baseline import load_baseline, write_baseline

from tickr.observers.loader import parse_modules

ROOT = Path(__file__).resolve().parents[2]


def active_module() -> ModuleType:
    """The module of the repo config's active strategy: tests build or break the real one."""
    cfg = load_baseline()
    values = tomllib.loads((ROOT / "strategies" / cfg.strategy.active / "strategy.toml")
                           .read_text("utf-8"))
    modules = parse_modules(cfg.observers.strategy_modules)
    return importlib.import_module(modules[values["strategy"]["name"]])


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A copy of the parts of the repo that preflight and rendering read."""
    for part in ("config", "strategies", "ops"):
        shutil.copytree(ROOT / part, tmp_path / part)
    write_baseline(tmp_path / "config" / "bot.toml")
    return tmp_path


@pytest.fixture
def config_path(repo: Path) -> Path:
    return repo / "config" / "bot.toml"


class _NoHistoryDeclared:
    """A strategy that builds but does not declare a usable prior_sessions_needed."""

    def __init__(self, real, need):
        self.name, self.version = real.name, real.version
        if need is not _MISSING:
            self.prior_sessions_needed = need

    def evaluate(self, context):  # pragma: no cover - never reached
        raise AssertionError


_MISSING = object()


def broken_create(kind):
    """A replacement create(values) for the active strategy that misbehaves in one way."""
    real_create = active_module().create

    def create(values):
        if kind == "raises":
            raise KeyError("boom")
        need = {"missing": _MISSING, "text": "5", "bool": True, "negative": -1}[kind]
        return _NoHistoryDeclared(real_create(values), need)

    return create



@pytest.fixture
def break_strategy(monkeypatch):
    """Replace the active strategy's create with one that misbehaves in the named way."""
    module = active_module()

    def apply(kind):
        monkeypatch.setattr(module, "create", broken_create(kind))

    return apply
