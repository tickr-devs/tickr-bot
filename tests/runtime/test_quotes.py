"""The live quotes file thread (runtime/quotes.py)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal as D

from tickr.api import contract as api
from tickr.runtime.market import StreamInbox
from tickr.runtime.quotes import QUOTES_WRITE_FAILED, QuoteFile
from tickr.types import Quote

T0 = datetime(2026, 11, 17, 15, 0, tzinfo=UTC)


def quote(symbol: str, bid: str, ask: str, at: datetime = T0) -> Quote:
    return Quote(symbol, D(bid), D(ask), D(100), D(100), at, at + timedelta(milliseconds=50))


def test_the_file_holds_every_valid_quote_and_is_written_only_when_one_changes(tmp_path):
    inbox = StreamInbox()
    path = tmp_path / "quotes.json"
    writer = QuoteFile(inbox, path, 0.5, lambda: T0)
    assert writer.write_once()  # the first look writes, even with no quotes
    assert api.read_quotes(path).quotes == ()
    inbox.on_quote(quote("BBB", "2.00", "2.01"))
    inbox.on_quote(quote("AAA", "10.10", "10.12"))
    inbox.on_quote(quote("XXX", "5.10", "5.00"))  # crossed: never shown
    inbox.on_quote(quote("ZZZ", "0", "1.00"))  # no bid: never shown
    assert writer.write_once()
    written = api.read_quotes(path)
    assert [(q.symbol, q.bid, q.ask) for q in written.quotes] == [
        ("AAA", D("10.10"), D("10.12")), ("BBB", D("2.00"), D("2.01"))]
    assert written.quotes[0].quoted_at == T0
    assert not writer.write_once()  # nothing changed: the file is left as it is
    inbox.on_quote(quote("AAA", "10.11", "10.12", T0 + timedelta(seconds=1)))
    assert writer.write_once()
    assert api.read_quotes(path).quotes[0].bid == D("10.11")


def test_a_failed_write_is_journaled_once_per_stretch(tmp_path):
    class Store:
        def __init__(self) -> None:
            self.rows: list[tuple[str, dict]] = []

        def transaction(self):
            from contextlib import nullcontext
            return nullcontext()

        def append(self, kind, body, run_id):
            self.rows.append((kind, body))

    class Engine:
        store = Store()
        run_id = "r1"

    blocked = tmp_path / "file"
    blocked.write_text("not a folder", "utf-8")
    inbox = StreamInbox()
    inbox.on_quote(quote("AAA", "1.00", "1.01"))
    writer = QuoteFile(inbox, blocked / "quotes.json", 0.5, lambda: T0)
    assert not writer.write_once()
    assert not writer.write_once()
    engine = Engine()
    writer.journal_failures(engine)  # type: ignore[arg-type]
    writer.journal_failures(engine)  # type: ignore[arg-type]
    assert [kind for kind, _ in engine.store.rows] == [QUOTES_WRITE_FAILED]
