"""Probe H2c: how the order history pages when one day holds more orders than one page.

`tickr probe-history-paging SYMBOL`, run by the owner on the Mac in the pre-market or the
post-market (the session check of engine/session_probe.py), with the bot stopped and the
account flat. The bot reads today's history window (docs/webull-facts.md H2b) to bring fills
forward when several entries fill together; with 20 slots a day holds more orders than the 44
rows one page was seen to hold, so the page size, the paging with a window and the rows' order
must be known before the fills-together bound relies on it (Fable, review of #629).

1. `[probe] paging_orders` BUY LIMIT DAY ALL orders of `[probe] quantity` share(s), each
   `[probe] paging_far_percent` below the bid and one tick below the one before (Webull
   refuses an order identical to a working one), so none can fill. Each is placed, read back by
   Detail, cancelled and proven over before the next; one is sent every
   `[probe] paging_send_interval_seconds`, within the place budget.
2. Today's history window is read page by page (up to `[probe] history_max_pages`, the window
   with every page): per page the row count, our rows, the place times in page order, whether a
   next page is offered and which of this run's orders it lists.

Answers: the page size (the row count of a full page), whether paging with a window works (a
second page, no repeated key, no row on two pages), whether every order of the run is listed,
and the rows' order. Verdict PASSED when a second page came and every order of the run was
listed exactly once.

Nothing fills (each limit lies far below the bid) and every order is proven over before the
next is sent; an order not proven over stops the run at once and the probe exits 4 with its id.
It is a sender besides the engine (tests/test_invariants.py names it). Hard rule 3: every
order is written to the evidence file before its one request, never resent; an unknown outcome
is read back by Detail (the plumbing of engine/session_probe.py). The probe holds the bot's lock
and uses the probe order prefix. Evidence: `<data_dir>/<[probe] evidence_dir>/<NY date>/
paging-<UTC time>/steps.jsonl` and a `summary.json`.
"""

from __future__ import annotations

from collections import Counter
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from tickr.broker.base import Broker, BrokerError
from tickr.calendar import ny_date_of
from tickr.config import Config
from tickr.data.base import MarketData
from tickr.engine.manual import ManualRefused
from tickr.engine.session_probe import (
    EXIT_ANSWERED,
    EXIT_OPERATOR,
    NOT_SENT,
    PERCENT,
    REFUSED,
    Clock,
    Evidence,
    Out,
    Probe,
    Sleep,
    Unresolved,
    floor_tick,
    require_flat,
    tick,
)
from tickr.types import OrderType, Side

PASSED = "passed"
UNFINISHED = "unfinished"


def page_answers(pages: list[dict[str, Any]], placed: list[str]) -> dict[str, Any]:
    """What the pages say: rows per page, whether a second page came, which orders of the run
    were listed (and any listed twice), and whether every page's place times run newest
    first."""
    listed = Counter(cid for page in pages for cid in page.get("listed", []))
    newest_first = all(page["place_times"] == sorted(page["place_times"], reverse=True)
                       for page in pages)
    paged = len(pages) > 1
    every_once = all(listed[cid] == 1 for cid in placed)
    return {
        "rows_per_page": [page["rows"] for page in pages],
        "next_page_offered": [page["next_page"] for page in pages],
        "paged": paged,
        "page_size": pages[0]["rows"] if paged else None,
        "listed_of_placed": sum(1 for cid in placed if listed[cid]),
        "placed": len(placed),
        "listed_twice": sorted(cid for cid, n in listed.items() if n > 1),
        "missing": sorted(cid for cid in placed if not listed[cid]),
        "newest_first_in_each_page": newest_first,
        "verdict": PASSED if paged and placed and every_once else UNFINISHED,
    }


def run(config: Config, broker: Broker, market: MarketData, symbol: str, folder: Path, *,
        now: Clock, sleep: Sleep, out: Out, session: str, session_end: datetime) -> int:
    """The two steps; returns the exit code. The caller holds the bot's lock. An order not
    proven over stops the run before any further order; the final broker read always runs and
    decides the exit code."""
    probe_cfg = config.probe
    started = now()
    probe = Probe(config, broker, market, symbol, Evidence(folder), now, sleep, out,
                  run_tag=f"{started.astimezone(UTC):%H%M%S}")
    summary: dict[str, Any] = {"probe": "history-paging", "environment": config.environment_name,
                               "symbol": symbol, "session": session,
                               "started_at": started.isoformat(),
                               "session_end": session_end.isoformat()}
    shapes = getattr(broker, "history_page_shapes", None)
    placed: list[str] = []
    unresolved: str | None = None
    try:
        try:
            if shapes is None:
                raise ManualRefused("this broker cannot read history page shapes")
            require_flat(broker)
            quote = probe.quote()
            if quote.ask > probe_cfg.max_price_usd:
                raise ManualRefused(f"ask {quote.ask} is above [probe] max_price_usd")
            limit = floor_tick(quote.bid * (1 - probe_cfg.paging_far_percent / PERCENT))
            poll = float(config.orders.detail_poll_seconds)
            for n in range(probe_cfg.paging_orders):
                price = limit - n * tick(limit)
                if price <= 0:
                    raise ManualRefused(f"order {n}: the price would fall to {price}")
                name = f"paging-{n}"
                cid = probe._cid("paging")
                sent = probe.place(name, probe.request(cid, Side.BUY, OrderType.LIMIT,
                                                       Decimal(probe_cfg.quantity), limit=price))
                if sent in (REFUSED, NOT_SENT):
                    raise ManualRefused(f"{name} was {sent}: stopped before the next")
                placed.append(cid)
                row, absent = probe.settle(name, cid, poll)
                row = probe.finish(name, cid, row, absent)
                if row is not None and row.filled_quantity > 0:
                    raise Unresolved(f"{name} {cid} filled {row.filled_quantity}")
                sleep(float(probe_cfg.paging_send_interval_seconds))
        except ManualRefused as exc:
            summary["stopped"] = str(exc)
            out(f"stopped: {exc}")
        if placed and shapes is not None:
            pages = shapes(probe_cfg.history_max_pages, ny_date_of(now()),
                           config.orders.probe_client_order_id_prefix)
            probe._log("history-pages", pages=pages)
            summary["pages"] = pages
            summary.update(page_answers(pages, placed))
            out(f"h2c_paging: {summary['verdict']}; rows per page {summary['rows_per_page']}, "
                f"page size {summary['page_size']}, listed {summary['listed_of_placed']} of "
                f"{summary['placed']}, twice {len(summary['listed_twice'])}, newest first in "
                f"each page {summary['newest_first_in_each_page']}")
    except Unresolved as exc:
        unresolved = str(exc)
        summary["unresolved"] = unresolved
        out(f"stopped, nothing more sent: {exc}")
    except BrokerError as exc:
        summary["error"] = f"{type(exc).__name__}: {exc}"
        out(f"history read failed: {exc}")
    finally:
        summary["broker_after"] = after = probe.leftovers()
        summary["finished_at"] = now().isoformat()
        probe.evidence.summary(summary)
    clean = (after["readable"] and after["position"] == 0 and not after["working_probe_orders"])
    if unresolved is not None or not clean:
        first = f"{unresolved}; " if unresolved is not None else ""
        out(f"OPERATOR: {first}the broker shows {after}: cancel the probe orders and sell what "
            "is held in the Webull app now")
        return EXIT_OPERATOR
    return EXIT_ANSWERED
