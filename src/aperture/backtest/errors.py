"""Typed refusals of the backtest tool. Each carries one cause; nothing is guessed."""

from __future__ import annotations


class BacktestError(Exception):
    """Bad input, settings or provider data: the step stops and says why."""
