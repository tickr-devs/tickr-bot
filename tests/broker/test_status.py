from __future__ import annotations

from decimal import Decimal as D

import pytest

from tickr.broker.status import map_status
from tickr.types import OrderState as S


@pytest.mark.parametrize("raw,qty,filled,expected", [
    ("SUBMITTED", "10", "0", S.ACCEPTED),
    ("PENDING", "10", "0", S.ACCEPTED),
    ("PARTIAL FILLED", "10", "4", S.PARTIALLY_FILLED),
    ("PARTIAL_FILLED", "10", "4", S.PARTIALLY_FILLED),
    ("FILLED", "10", "10", S.FILLED),
    ("CANCELLED", "10", "3", S.CANCELLED),
    ("FAILED", "10", "0", S.REJECTED),
    ("EXPIRED", "10", "0", S.EXPIRED),
    ("SOMETHING_NEW", "10", "0", S.UNKNOWN),
    ("FILLED", "10", "9", S.UNKNOWN),
    ("SUBMITTED", "10", "1", S.UNKNOWN),
    ("PARTIAL FILLED", "10", "10", S.UNKNOWN),
    ("FAILED", "10", "2", S.UNKNOWN),
    ("FILLED", "10", "11", S.UNKNOWN),
    ("FILLED", "0", "0", S.UNKNOWN),
])
def test_map_status(raw, qty, filled, expected):
    assert map_status(raw, D(qty), D(filled)) is expected
