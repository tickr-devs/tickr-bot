"""Percent-of-equity risk limits (backlog X2).

A percent limit of 0 is off. When on, the effective limit is the smaller of the USD limit and
percent / 100 x the broker-reported equity, so a percent can only tighten a limit, never loosen
it. Pure arithmetic; admission decides what to do when the equity is not known.
"""

from __future__ import annotations

from decimal import Decimal

PERCENT = Decimal(100)  # unit factor: percent to fraction


def effective_limit(usd: Decimal, percent: Decimal, equity_usd: Decimal | None) -> Decimal:
    """`usd` when `percent` is 0 (off), else min(`usd`, percent / 100 x `equity_usd`).
    Raises ValueError for an on percent without a usable equity: the caller refuses first."""
    if percent == 0:
        return usd
    if equity_usd is None or not equity_usd.is_finite() or equity_usd <= 0:
        raise ValueError(f"percent limit {percent} is on but equity is {equity_usd!r}")
    return min(usd, percent / PERCENT * equity_usd)
