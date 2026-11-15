"""GET /journal: the latest rows of the bot's journal (database opened read-only), for the
Overview's journal card and its "line #N". Only the row number, time, kind and the symbol the
body names are served; the bot reads and writes nothing for it."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path

from aperture.api import contract as api
from aperture.store import Store

NO_DATABASE = "the bot has not created its database yet"


def journal_document(database: Path, rows: int) -> api.JournalV1:
    if not database.exists():
        return api.JournalV1(None, (), NO_DATABASE)
    try:
        store = Store.open_readonly(database)
        try:
            found = store.query("SELECT seq, at_utc, kind, body FROM journal ORDER BY seq DESC "
                                "LIMIT ?", (rows,))
        finally:
            store.close()
        shown = tuple(api.JournalRowV1(int(r["seq"]), datetime.fromisoformat(r["at_utc"]),
                                       str(r["kind"]), _symbol(r["body"]))
                      for r in reversed(found))
    except (sqlite3.Error, ValueError) as exc:
        return api.JournalV1(None, (), f"database unreadable ({type(exc).__name__})")
    return api.JournalV1(shown[-1].seq if shown else None, shown,
                         None if shown else "the journal has no rows yet")


def _symbol(body: str) -> str | None:
    parsed = json.loads(body)
    value = parsed.get("symbol") if isinstance(parsed, dict) else None
    return value if isinstance(value, str) else None
