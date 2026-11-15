"""Typed refusals of the advisor."""

from __future__ import annotations


class AdvisorError(Exception):
    """The advisor refused to run or to accept a response; one cause."""
