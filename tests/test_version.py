"""The running code version (tickr/version.py): git commit, image build argument, refusal."""

from __future__ import annotations

import subprocess
from decimal import Decimal
from pathlib import Path

import pytest

from tickr.version import ENV_VARIABLE, CodeVersionUnknown, code_version, recorded

TIMEOUT = Decimal(10)
SHA = "0123456789abcdef0123456789abcdef01234567"


def _git(root: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True,
                          text=True).stdout.strip()


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "test@example.invalid")
    _git(tmp_path, "config", "user.name", "test")
    _git(tmp_path, "config", "commit.gpgsign", "false")
    (tmp_path / "src").mkdir()
    (tmp_path / "config").mkdir()
    (tmp_path / "src" / "tracked.py").write_text("one\n")
    (tmp_path / "config" / "bot.toml").write_text("a = 1\n")
    _git(tmp_path, "add", "src", "config")
    _git(tmp_path, "commit", "-q", "-m", "first")
    return tmp_path


def test_clean_checkout_gives_the_full_commit(repo):
    assert code_version(repo, {}, TIMEOUT) == _git(repo, "rev-parse", "HEAD")


def test_untracked_files_never_make_it_dirty(repo):
    (repo / "data.sqlite3").write_text("x")
    assert not code_version(repo, {}, TIMEOUT).endswith("-dirty")


@pytest.mark.parametrize("change", ["edit", "stage"])
def test_a_changed_tracked_file_makes_it_dirty(repo, change):
    (repo / "src" / "tracked.py").write_text("two\n")
    if change == "stage":
        _git(repo, "add", "src/tracked.py")
    assert code_version(repo, {}, TIMEOUT) == _git(repo, "rev-parse", "HEAD") + "-dirty"


def test_a_config_edit_never_makes_it_dirty(repo):
    """The config is tagged by the run's config_digest; only src/ and strategies/ count."""
    (repo / "config" / "bot.toml").write_text("a = 2\n")
    assert code_version(repo, {}, TIMEOUT) == _git(repo, "rev-parse", "HEAD")


def test_the_environment_is_ignored_in_a_git_checkout(repo):
    assert code_version(repo, {ENV_VARIABLE: SHA}, TIMEOUT) == _git(repo, "rev-parse", "HEAD")


def test_a_worktree_with_a_git_file_reads_its_own_commit(repo, tmp_path_factory):
    worktree = tmp_path_factory.mktemp("wt") / "wt"
    _git(repo, "worktree", "add", "-q", "--detach", str(worktree))
    assert (worktree / ".git").is_file()
    assert code_version(worktree, {}, TIMEOUT) == _git(repo, "rev-parse", "HEAD")


@pytest.mark.parametrize("value", [SHA, SHA + "-dirty", SHA[:8]])
def test_without_git_the_image_build_argument_is_used(tmp_path, value):
    assert code_version(tmp_path, {ENV_VARIABLE: value}, TIMEOUT) == value


@pytest.mark.parametrize("environ", [{}, {ENV_VARIABLE: ""}])
def test_neither_git_nor_build_argument_refuses(tmp_path, environ):
    with pytest.raises(CodeVersionUnknown, match=ENV_VARIABLE):
        code_version(tmp_path, environ, TIMEOUT)


@pytest.mark.parametrize("value", ["latest", "-", "$(git rev-parse HEAD)", SHA.upper()])
def test_a_build_argument_that_is_not_a_commit_refuses(tmp_path, value):
    with pytest.raises(CodeVersionUnknown, match="not a commit id"):
        code_version(tmp_path, {ENV_VARIABLE: value}, TIMEOUT)


def test_a_broken_git_folder_refuses_with_the_cause(tmp_path):
    (tmp_path / ".git").write_text("gitdir: /nonexistent\n")
    with pytest.raises(CodeVersionUnknown, match="rev-parse"):
        code_version(tmp_path, {ENV_VARIABLE: SHA}, TIMEOUT)


def test_recorded_shows_old_runs_as_null():
    assert recorded("-") is None
    assert recorded(SHA) == SHA


def test_the_running_code_is_read_from_the_folder_above_src():
    import tickr.version as version

    assert (Path(version.__file__).resolve().parents[2] / "src" / "tickr" / "version.py").is_file()
