"""Size by model confidence (backlog R30): a pure map from probability to a USD position size.

Not wired into the order path: the model's live decision path is another thread's, and the
platform's admission still checks the position cap whatever size is asked for. Refuses, never
clips: a probability outside 0..1, a missing or non-finite one, or bounds outside the cap.
"""

from __future__ import annotations

from decimal import Decimal


class SizingRefused(ValueError):
    """One typed cause: `cause` is a stable code, the message says the numbers."""

    def __init__(self, cause: str, detail: str) -> None:
        super().__init__(f"{cause}: {detail}")
        self.cause = cause


def size_usd(probability: Decimal | None, min_usd: Decimal, max_usd: Decimal,
             cap_usd: Decimal) -> Decimal:
    """Linear from `min_usd` at probability 0 to `max_usd` at probability 1, at most `cap_usd`."""
    if not 0 < min_usd <= max_usd <= cap_usd:
        raise SizingRefused("bounds_outside_cap",
                            f"need 0 < min {min_usd} <= max {max_usd} <= cap {cap_usd}")
    if probability is None:
        raise SizingRefused("probability_missing", "no probability given")
    if not probability.is_finite():
        raise SizingRefused("probability_not_finite", f"probability {probability}")
    if not 0 <= probability <= 1:
        raise SizingRefused("probability_out_of_range", f"probability {probability} not in 0..1")
    return min_usd + probability * (max_usd - min_usd)
