"""Typed refusals of the replay tool. Each carries one cause; nothing is guessed or clipped."""

from __future__ import annotations


class ReplayError(Exception):
    """Base class: the replay refused to run or to continue."""


class BarFileError(ReplayError):
    """A bar file is malformed. `path` and `line` point at the first offending record."""

    def __init__(self, path: str, line: int, cause: str) -> None:
        super().__init__(f"{path}:{line}: {cause}")
        self.path = path
        self.line = line
        self.cause = cause


class SettingsError(ReplayError):
    """The replay settings file is missing a key, has an unknown key, or a bad value."""


class StrategyContractError(ReplayError):
    """The strategy returned a decision that breaks the interface contract."""
