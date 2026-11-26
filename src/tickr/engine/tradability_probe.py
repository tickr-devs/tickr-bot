"""Probe T8, read only: how Webull shows a halted or non-tradable instrument.

The tradability check (backlog T8, `[risk] tradability_check_enabled`) waits for these facts
(docs/webull-facts.md): what the snapshot's `trade_status` and `list_status`, the instrument
profile's `status` (OC, CO, NT seen) and the trade instrument's `trade_policy` and
`forbid_reason` say for a normal stock, a halted one and an OTC or otherwise non-tradable one.
`tickr probe-tradability SYMBOL...` reads the snapshot and the profiles of the symbols (one
request each), then the trade instrument of every profile row, keeps every answer as received in
`tradability.jsonl` of an evidence folder, and prints the fields above. It sends no orders and
decides nothing.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable, Sequence
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from tickr.data.base import SourceRead

READS_FILE = "tradability.jsonl"
# The fields each read is printed with (all other keys stay in the evidence file only).
SNAPSHOT_FIELDS = ("symbol", "trade_status", "list_status", "bid", "ask", "price", "quote_time")
PROFILE_FIELDS = ("symbol", "instrument_id", "status", "sub_category", "exchange_code")
TRADE_INSTRUMENT_FIELDS = ("instrument_id", "symbol", "trade_policy", "forbid_reason")
FIELDS = {"snapshot": SNAPSHOT_FIELDS, "profiles": PROFILE_FIELDS,
          "trade-instrument": TRADE_INSTRUMENT_FIELDS}
DATA_KEY = "data"


@runtime_checkable
class TradabilitySources(Protocol):
    def tradability_sources(self, symbols: Sequence[str]) -> list[tuple[str, SourceRead]]: ...


def _rows(body: Any) -> Iterable[Any]:
    """The objects of a body: a bare list, a list under `data`, or the body itself."""
    if isinstance(body, list):
        return body
    if isinstance(body, dict) and isinstance(body.get(DATA_KEY), list):
        return body[DATA_KEY]
    return [body]


def describe(label: str, read: SourceRead) -> list[str]:
    """Lines for one read: its answer, then the probed fields of each row it holds."""
    head = f"  {read.name} {label}: HTTP {read.status}"
    if read.error_code:
        head += f" {read.error_code}"
    lines = [head]
    wanted = FIELDS.get(read.name, ())
    if read.body is None:
        return lines
    for row in _rows(read.body):
        if not isinstance(row, dict):
            lines.append(f"    {row!r}")
            continue
        shown = " ".join(f"{k}={row[k]}" for k in wanted if k in row)
        missing = [k for k in wanted if k not in row]
        lines.append(f"    {shown}" + (f" (absent: {', '.join(missing)})" if missing else ""))
    return lines


def run(source: TradabilitySources, symbols: Sequence[str], folder: Path,
        out: Callable[[str], None]) -> int:
    """Read and record the symbols; 0 when every read was answered, 3 otherwise."""
    unanswered = 0
    with (folder / READS_FILE).open("a", encoding="utf-8") as file:
        for label, read in source.tradability_sources(symbols):
            file.write(json.dumps({"label": label, "read": read.name, "status": read.status,
                                   "error_code": read.error_code,
                                   "received": read.received.isoformat(), "body": read.body},
                                  default=str, sort_keys=True) + "\n")
            for line in describe(label, read):
                out(line)
            if read.status is None:
                unanswered += 1
    return 0 if unanswered == 0 else 3
