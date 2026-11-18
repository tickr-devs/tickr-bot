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
from datetime import UTC, datetime, timedelta
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


def open_page_answers(pages: list[dict[str, Any]], placed: list[str]) -> dict[str, Any]:
    """What the open-orders pages say while every order of the run works: rows per page,
    whether a second page came, which orders of the run were listed (and any listed twice).
    Verdict PASSED when every order of the run was listed exactly once (on one page or
    several): the list the bot reads at 20 slots is then complete."""
    listed = Counter(cid for page in pages for cid in page.get("listed", []))
    paged = len(pages) > 1
    every_once = bool(placed) and all(listed[cid] == 1 for cid in placed)
    return {
        "rows_per_page": [page["rows"] for page in pages],
        "next_page_offered": [page["next_page"] for page in pages],
        "paged": paged,
        "page_size": pages[0]["rows"] if paged else None,
        "listed_of_placed": sum(1 for cid in placed if listed[cid]),
        "placed": len(placed),
        "listed_twice": sorted(cid for cid, n in listed.items() if n > 1),
        "missing": sorted(cid for cid in placed if not listed[cid]),
        "verdict": PASSED if every_once and not pages[-1]["next_page"] else UNFINISHED,
    }


def run_open_orders(config: Config, broker: Broker, market: MarketData, symbol: str,
                    folder: Path, *, now: Clock, sleep: Sleep, out: Out, session: str,
                    session_end: datetime) -> int:
    """Probe H2d: `[probe] open_orders_paging_orders` far BUY LIMIT orders placed one by one
    and kept working, the open-orders list read page by page (up to the environment's
    `open_orders_max_pages`), then every order cancelled and proven over, one by one. Each
    order is written to the evidence file before its one request, never resent; an unknown
    outcome is read back by Detail. An order Detail shows terminal while it should work
    (filled or rejected), or a share of the symbol held before the next order (an earlier
    order filled), stops the placing at once; the cancels always run. Refused within `[probe]
    open_orders_end_margin_minutes` of the session's end. Every order of the run stays in
    that day's order history (H2c: one page held 87 rows). Returns the exit
    code; the caller holds the bot's lock."""
    probe_cfg = config.probe
    started = now()
    probe = Probe(config, broker, market, symbol, Evidence(folder), now, sleep, out,
                  run_tag=f"{started.astimezone(UTC):%H%M%S}")
    summary: dict[str, Any] = {"probe": "open-orders-paging",
                               "environment": config.environment_name, "symbol": symbol,
                               "session": session, "started_at": started.isoformat(),
                               "session_end": session_end.isoformat()}
    shapes = getattr(broker, "open_orders_page_shapes", None)
    poll = float(config.orders.detail_poll_seconds)
    interval = float(probe_cfg.paging_send_interval_seconds)
    placed: list[str] = []
    unresolved: list[str] = []
    try:
        try:
            if shapes is None:
                raise ManualRefused("this broker cannot read open-orders page shapes")
            margin = timedelta(minutes=probe_cfg.open_orders_end_margin_minutes)
            if now() > session_end - margin:
                raise ManualRefused(f"the {session} ends at {session_end.isoformat()}: less "
                                    f"than {probe_cfg.open_orders_end_margin_minutes} min left")
            require_flat(broker)
            quote = probe.quote()
            if quote.ask > probe_cfg.max_price_usd:
                raise ManualRefused(f"ask {quote.ask} is above [probe] max_price_usd")
            limit = floor_tick(quote.bid * (1 - probe_cfg.paging_far_percent / PERCENT))
            for n in range(probe_cfg.open_orders_paging_orders):
                # Any earlier order may fill while later ones are placed (a falling stock, no
                # broker stop outside the regular session): a share held stops the placing.
                try:
                    held = probe._position()
                except Unresolved as exc:
                    raise ManualRefused(f"before open-{n}: {exc}: stopped") from exc
                if held != 0:
                    raise ManualRefused(f"before open-{n}: the broker holds {held} {symbol}: "
                                        "stopped")
                price = limit - n * tick(limit)
                if price <= 0:
                    raise ManualRefused(f"order {n}: the price would fall to {price}")
                name = f"open-{n}"
                cid = probe._cid("open")
                sent = probe.place(name, probe.request(cid, Side.BUY, OrderType.LIMIT,
                                                       Decimal(probe_cfg.quantity), limit=price))
                if sent in (REFUSED, NOT_SENT):
                    raise ManualRefused(f"{name} was {sent}: stopped before the next")
                placed.append(cid)
                row, absent = probe.settle(name, cid, poll)
                if absent or row is None or row.state.terminal:
                    state = "absent" if absent or row is None else row.raw_status
                    raise ManualRefused(f"{name} {cid} is {state}, not working: stopped")
                sleep(interval)
            pages = shapes(config.environment.open_orders_max_pages,
                           config.orders.probe_client_order_id_prefix)
            probe._log("open-orders-pages", pages=pages)
            summary["pages"] = pages
            summary.update(open_page_answers(pages, placed))
            out(f"h2d_open_orders: {summary['verdict']}; rows per page "
                f"{summary['rows_per_page']}, page size {summary['page_size']}, listed "
                f"{summary['listed_of_placed']} of {summary['placed']}, twice "
                f"{len(summary['listed_twice'])}")
        except ManualRefused as exc:
            summary["stopped"] = str(exc)
            out(f"stopped: {exc}")
        except BrokerError as exc:
            summary["error"] = f"{type(exc).__name__}: {exc}"
            out(f"open-orders read failed: {exc}")
    finally:
        # Every order of the run is cancelled and proven over, one by one, whatever happened.
        for n, cid in enumerate(placed):
            name = f"open-{n}"
            try:
                row = probe.finish(name, cid, None, False)
            except Unresolved as exc:
                unresolved.append(str(exc))
                out(f"not proven over: {exc}")
                continue
            if row is not None and row.filled_quantity > 0:
                unresolved.append(f"{name} {cid} filled {row.filled_quantity}")
            sleep(interval)
        if unresolved:
            summary["unresolved"] = unresolved
        summary["broker_after"] = after = probe.leftovers()
        summary["finished_at"] = now().isoformat()
        probe.evidence.summary(summary)
    clean = (after["readable"] and after["position"] == 0 and not after["working_probe_orders"])
    if unresolved or not clean:
        first = f"{'; '.join(unresolved)}; " if unresolved else ""
        out(f"OPERATOR: {first}the broker shows {after}: cancel the probe orders and sell what "
            "is held in the Webull app now")
        return EXIT_OPERATOR
    return EXIT_ANSWERED
