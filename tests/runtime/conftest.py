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
