from __future__ import annotations

import shutil
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A copy of the parts of the repo that preflight and rendering read."""
    for part in ("config", "strategies", "ops"):
        shutil.copytree(ROOT / part, tmp_path / part)
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
    """A replacement create(values) for price_action_v1 that misbehaves in one way."""
    from aperture.strategy import price_action_v1

    real_create = price_action_v1.create

    def create(values):
        if kind == "raises":
            raise KeyError("boom")
        need = {"missing": _MISSING, "text": "5", "bool": True, "negative": -1}[kind]
        return _NoHistoryDeclared(real_create(values), need)

    return create



@pytest.fixture
def break_strategy(monkeypatch):
    """Replace price_action_v1.create with one that misbehaves in the named way."""
    from aperture.strategy import price_action_v1

    def apply(kind):
        monkeypatch.setattr(price_action_v1, "create", broken_create(kind))

    return apply
