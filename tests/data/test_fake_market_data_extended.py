from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal as D

from tickr.data.base import ExtendedQuotes, FakeMarketData
from tickr.types import Quote

NOW = datetime(2026, 11, 19, 23, 30, tzinfo=UTC)


def test_fake_extended_snapshot_pairs_the_book_with_its_last_trade() -> None:
    fake = FakeMarketData()
    assert isinstance(fake, ExtendedQuotes)
    fake.set_quote(Quote("AAA", D("9.9"), D("10"), D(1), D(1), NOW, NOW))
    fake.set_quote(Quote("BBB", D("4.9"), D("5"), D(1), D(1), NOW, NOW))
    fake.set_last_trade("AAA", D("9.95"), NOW)
    aaa, bbb = fake.extended_snapshot(["AAA", "BBB", "CCC"])
    assert (aaa.last_price, aaa.last_trade_time) == (D("9.95"), NOW)
    assert (bbb.quote.symbol, bbb.last_price) == ("BBB", None)
