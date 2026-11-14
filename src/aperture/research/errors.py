"""Typed refusals of the research tools. Each carries one cause; nothing is guessed."""

from __future__ import annotations


class ResearchError(Exception):
    """Base class: a research tool refused to run or to continue."""


class RecordFileError(ResearchError):
    """A decision record file is malformed. `path` and `line` point at the first bad record."""

    def __init__(self, path: str, line: int, cause: str) -> None:
        super().__init__(f"{path}:{line}: {cause}")
        self.path = path
        self.line = line
        self.cause = cause


class ResearchSettingsError(ResearchError):
    """The research settings file is missing a key, has an unknown key, or a bad value."""
