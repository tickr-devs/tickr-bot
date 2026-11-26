"""Massive trades and quotes, live (WebSocket channels `T` and `Q`) and backfilled (REST
`/v3/trades/{ticker}`, `/v3/quotes/{ticker}`), kept as rows of the training's flat-file columns
(`us_stocks_sip/trades_v1`, `quotes_v1`, the columns the frozen converter v11 reads) so the
converter can run on the live day as it runs on the day file.

Field map (the pull request and the project note tq-field-map.md hold the table with the
doc links):

- Stream timestamps are Unix MILLISECONDS (`t`, `pt`); the flat file and REST hold nanoseconds.
  A stream row's `sip_timestamp` is `t * 10**6`, exact in unit and truncated in precision; its
  `source` is `stream` so a reader can tell.
- The stream sends no `correction`: a stream row has none (the flat file leaves it blank for an
  ordinary print too). Whether the stream relays later cancel / error / correction records as
  plain trades is not documented; `source` keeps such rows apart for that check.
- A field the stream leaves out (`trfi`, `pt`, `bp`/`bs`, `ap`/`as`, `c`, `i`) is kept as None,
  never filled: the flat file writes 0 for an empty quote side and blank for no TRF, and the
  converter coalesces both; the raw quote marks of its side files (not model inputs) do not.

Every row also carries `recv_ns`, the local wall-clock time (ns) its message or page arrived,
so the lag of the feed can be measured before any wait is chosen (owner rule).

Versions, not one merged row (Codex, codex-108 point 3): every copy of a print that arrives,
from the stream or from a REST page, is kept as its own version with its own `recv_ns` and
`source`, keyed by (kind, ticker, sequence_number) inside the store of one New York session
date; Massive documents the sequence number as unique per ticker within a session. A decision
reads an immutable `TickSnapshot` taken at its receipt cutoff: per key the versions received by
the cutoff, of which it uses the latest REST version when one arrived by then (nanosecond time,
`correction`), else the latest stream version, each with its own receipt time. A chosen stream
version is marked `provisional`: its fidelity to the file is unproven in `STREAM_UNPROVEN`.
Later arrivals never change a snapshot already taken.

Differences (codex-109 point 4): a version whose content differs from the version held before it
is a typed `Conflict`. Verified representation differences are normalized first (`_same`);
columns one source cannot carry comparably are named on the record (`uncompared`, `lossy`). A
`revision` keeps the print's identity (SIP time at the compared precision and, for a trade, the
exchange) and changes other fields; `uncertain` identity means the two versions may be different
prints under one key. Only a conflict that touches a model input (or of uncertain identity)
refuses, and only its symbol (and ranks over a set holding it), until a complete REST window of
its kind received at or after its latest REST version (else its later version) spans its SIP
range: complete REST data received by the cutoff is authoritative. The snapshot records the
receipt time from which each conflict is resolved (`resolutions`).

Initial state (codex-107 point 2): a symbol's day starts at New York midnight, as the day file
does (the converter carries a pre-04:00 quote), so `TickBackfill.initial` fetches from 00:00
America/New_York of the session date. `TickSnapshot.refusal` names why a symbol's ticks are not
proven complete at the cutoff (not subscribed, no complete backfill from midnight, an incomplete
backfill, an unfilled stream gap, coverage not verified, an unresolved conflict); a decision that
needs the symbol refuses with that cause.

Coverage (codex-109 points 4 and 6): sending a subscription, or Massive acknowledging it, does
not prove that the events after it are delivered. Coverage is reasoned in SIP event time of rows
actually seen, never by comparing local receipt times with REST SIP times: per kind, a complete
REST window (pagination proven exhausted) must start at or before the SIP millisecond of the
last stream row received before the drop (midnight for the initial state, or when no row of
that kind was seen before it in this session date) and reach past the SIP millisecond of the
first stream row received after the (re)subscription, so the REST and stream event-time ranges
overlap at both ends. Receipt times only order our own events (which row came before the drop,
which after the resubscription). Until a stream row of that kind arrives after it, the
hand-over is unverified and the symbol refused. A connection reset just after New York midnight
is such a gap: with no row of the new date seen before it, its repair starts at 00:00 and holds
the date's events before 04:00.

Clocks (codex-109 point 6), kept as separate fields: `recv_ns` is local wall-clock receipt;
`TickSnapshot.lag` gives receipt minus SIP time, an estimate of clock offset plus feed delay,
recorded and never used as proof; `Gap.down_mono` / `up_mono` and `Window.elapsed_ns` are
monotonic-clock nanoseconds, for durations only.

Measurement, no thresholds (owner rule): receipt time and lag per symbol, REST requests and
rate-limit answers, late arrivals (a version received after a snapshot whose cutoff already
passed its SIP time) and stream rows that share a SIP millisecond, are counted, not judged.

Memory: rows are held in Python tuples in memory for the day. A full-market day of quotes is
far larger than a cohort's; the capacity of either path is not measured yet and no cap is
invented here.

Not decided here: which subscription path runs (`[massive] tick_subscription`, "symbols" or
"market", a measurement decision), and whether this stream may share Massive's one connection
per cluster with `MinuteStream` (both use `stream_host` / `stream_path`).
"""

from __future__ import annotations

import collections
import dataclasses
import datetime
import json
import threading
import time
import types
import urllib.parse
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from tickr.config import Config
from tickr.model.errors import ModelRefused
from tickr.model.massive import NEW_YORK, NS_PER_MS, Opener, _get, _open, credential

# The flat-file columns of trades_v1 the converter v11 reads (its TCOLS, plus
# participant_timestamp of its side files), and of quotes_v1 (its QCOLS).
TRADE_COLUMNS = ("ticker", "conditions", "correction", "exchange", "price", "size",
                 "sip_timestamp", "sequence_number", "trf_id", "participant_timestamp")
QUOTE_COLUMNS = ("ticker", "ask_price", "ask_size", "bid_price", "bid_size", "sip_timestamp",
                 "sequence_number", "conditions", "indicators")
EXTRA_COLUMNS = ("recv_ns", "source")
STREAM, REST = "stream", "rest"
KINDS = ("trades", "quotes")
MARKET = "*"  # the gap key of a full-market subscription
SUBSCRIPTIONS = ("symbols", "market")
CHANNELS = {"trades": "T", "quotes": "Q"}

# Stream key -> flat-file column. `ms` marks a millisecond time put in nanoseconds; `codes` a
# list or single code written as the file's comma-separated text.
TRADE_STREAM = {"sym": "ticker", "c": "conditions", "x": "exchange", "p": "price", "s": "size",
                "t": "sip_timestamp", "q": "sequence_number", "trfi": "trf_id",
                "pt": "participant_timestamp"}
QUOTE_STREAM = {"sym": "ticker", "ap": "ask_price", "as": "ask_size", "bp": "bid_price",
                "bs": "bid_size", "t": "sip_timestamp", "q": "sequence_number",
                "c": "conditions", "i": "indicators"}
STREAM_MS = {"t", "pt"}
STREAM_REQUIRED = {"trades": ("sym", "x", "p", "s", "t", "q"), "quotes": ("sym", "t", "q")}
# REST results carry the flat-file names (no ticker: it is the path's).
REST_REQUIRED = {"trades": ("exchange", "price", "size", "sip_timestamp", "sequence_number"),
                 "quotes": ("sip_timestamp", "sequence_number")}
CODE_COLUMNS = {"conditions", "indicators"}
# Columns a stream and a REST version of one print both carry with one meaning (besides the SIP
# millisecond): a difference is a conflict. Two versions of one source compare every column.
CROSS_COMPARED = {"trades": ("exchange", "price", "size", "conditions", "trf_id"),
                  "quotes": ("ask_price", "ask_size", "bid_price", "bid_size", "indicators")}
# Columns not compared between a stream and a REST version, named on each such Conflict: the
# stream sends no correction; its participant time is in ms and absent on ORF prints; its quote
# condition is one code where REST lists several (tq-field-map gaps 2 and 4).
CROSS_UNCOMPARED = {"trades": ("correction", "participant_timestamp"), "quotes": ("conditions",)}
# Compared at the stream's lower precision (milliseconds) between a stream and a REST version.
CROSS_LOSSY = ("sip_timestamp",)
# Stream columns whose fidelity to the file is unproven (tq-field-map gaps 1, 2, 4, 5, 7): a
# chosen stream version is `provisional` until a REST version replaces it.
STREAM_UNPROVEN = {"trades": ("sip_timestamp", "correction", "size", "participant_timestamp"),
                   "quotes": ("sip_timestamp", "conditions", "bid_size", "ask_size")}
# Columns the converter v11 does not turn into model inputs (tq-field-map "converter use": the
# exchange is a header check, the participant time feeds only the px side files). A difference
# confined to them is recorded, never refused.
NOT_INPUTS = {"trades": ("exchange", "participant_timestamp"), "quotes": ()}
# The columns that name one print besides its key: a difference in them is uncertain identity.
IDENTITY = {"trades": ("sip_timestamp", "exchange"), "quotes": ("sip_timestamp",)}
# An empty quote side: the stream leaves it out (None), REST and the file write 0.
QUOTE_SIDES = {"ask_price", "ask_size", "bid_price", "bid_size"}
HTTP_TOO_MANY_REQUESTS = "429"  # protocol constant: the status of a rate-limit answer


def columns(kind: str) -> tuple[str, ...]:
    return (TRADE_COLUMNS if kind == "trades" else QUOTE_COLUMNS) + EXTRA_COLUMNS


def day_bounds_ns(day: str) -> tuple[int, int]:
    """New York midnight to the next midnight of `day`, Unix ns: the span of the day file."""
    start = datetime.datetime.fromisoformat(day).replace(tzinfo=NEW_YORK)
    end = (start + datetime.timedelta(days=1)).replace(tzinfo=NEW_YORK)
    return int(start.timestamp()) * 10**9, int(end.timestamp()) * 10**9


def _number(value: Any, what: str) -> Any:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ModelRefused("tick_unreadable", what)
    return value


def _codes(value: Any, what: str) -> str | None:
    """A list of codes (or one code) as the flat file's text `12,37`; absent stays None."""
    if value is None:
        return None
    items = value if isinstance(value, list) else [value]
    if not all(isinstance(v, int) and not isinstance(v, bool) for v in items):
        raise ModelRefused("tick_unreadable", what)
    return ",".join(str(v) for v in items)


def stream_row(kind: str, message: Mapping[str, Any]) -> dict[str, Any]:
    """One T or Q message as flat-file columns; a missing required field refuses."""
    for key in STREAM_REQUIRED[kind]:
        if message.get(key) is None:
            raise ModelRefused("tick_unreadable", f"a {CHANNELS[kind]} message without {key}")
    if not isinstance(message["sym"], str) or not message["sym"]:
        raise ModelRefused("tick_unreadable", f"a {CHANNELS[kind]} message without sym")
    row: dict[str, Any] = dict.fromkeys(TRADE_COLUMNS if kind == "trades" else QUOTE_COLUMNS)
    for key, col in (TRADE_STREAM if kind == "trades" else QUOTE_STREAM).items():
        value = message.get(key)
        if value is None or col == "ticker":
            row[col] = value
        elif col in CODE_COLUMNS:
            row[col] = _codes(value, f"{CHANNELS[kind]}.{key}")
        else:
            value = _number(value, f"{CHANNELS[kind]}.{key}")
            row[col] = value * NS_PER_MS if key in STREAM_MS else value
    return row


def rest_row(kind: str, symbol: str, result: Any) -> dict[str, Any]:
    """One REST result as flat-file columns; a missing required field refuses."""
    if not isinstance(result, dict):
        raise ModelRefused("tick_unreadable", f"a {kind} result that is not an object")
    for key in REST_REQUIRED[kind]:
        if result.get(key) is None:
            raise ModelRefused("tick_unreadable", f"a {kind} result without {key}")
    row: dict[str, Any] = {"ticker": symbol}
    for col in (TRADE_COLUMNS if kind == "trades" else QUOTE_COLUMNS)[1:]:
        value = result.get(col)
        if value is None:
            row[col] = None
        elif col in CODE_COLUMNS:
            row[col] = _codes(value, f"{kind}.{col}")
        else:
            row[col] = _number(value, f"{kind}.{col}")
    return row


# --- the day's versions and the decision snapshot --------------------------------------------


REVISION, UNCERTAIN = "revision", "uncertain"


@dataclass(frozen=True)
class Conflict:
    """Two versions of one print whose content differs: `fields` names the differing columns,
    `held` and `later` the (source, recv_ns) of the version held before and of the one that
    arrived. `identity` is REVISION or UNCERTAIN (module text); `uncompared` and `lossy` name
    the columns not compared or compared at reduced precision; `inputs` says whether it touches
    a model input; `sip_range` the SIP span [from, to) a resolving window must cover."""

    kind: str
    ticker: str
    sequence_number: int
    fields: tuple[str, ...]
    held: tuple[str, int]
    later: tuple[str, int]
    identity: str
    uncompared: tuple[str, ...]
    lossy: tuple[str, ...]
    inputs: bool
    sip_range: tuple[int, int]

    @property
    def authority_ns(self) -> int:
        """A resolving window must be received at or after this: the conflict's latest REST
        version, else its later version (a stream copy after REST does not reopen it)."""
        rest = [ns for source, ns in (self.held, self.later) if source == REST]
        return max(rest) if rest else self.later[1]


@dataclass(frozen=True)
class Window:
    """One REST backfill of one kind of a symbol over SIP time [from_ns, to_ns): `cause` is None
    only when its final page came without next_url (exhaustion proven), else why it is
    incomplete; `recv_ns` is the receipt of its last answer."""

    symbol: str
    kind: str
    from_ns: int
    to_ns: int
    recv_ns: int
    pages: int
    cause: str | None
    elapsed_ns: int  # monotonic duration from its first request to its last answer


@dataclass(frozen=True)
class Gap:
    """A dropped stream of one symbol (or MARKET): `down_ns` / `up_ns` are wall-clock receipt
    times of the drop and of the resubscription (None while down), used only to order our own
    receipts; `down_mono` / `up_mono` the same moments on the monotonic clock (durations);
    `before` the SIP ns of the last stream row per (kind, ticker) received before the drop."""

    cause: str
    down_ns: int
    down_mono: int
    up_ns: int | None
    up_mono: int | None
    before: Mapping[tuple[str, str], int]

    @property
    def duration_ns(self) -> int | None:
        return None if self.up_mono is None else self.up_mono - self.down_mono


def _same(col: str, a: Any, b: Any) -> bool:
    """Equal after the verified representation differences: an empty quote side (None or 0) and
    absent codes (None or empty), which the converter v11 coalesces to one model input."""
    if a == b:
        return True
    if col in QUOTE_SIDES:
        return a in (None, 0) and b in (None, 0)
    return col in CODE_COLUMNS and a in (None, "") and b in (None, "")


class TickStore:
    """Every version of the session date's trades and quotes (see the module text). Thread-safe:
    the stream thread and a backfill add, the decision side takes snapshots."""

    def __init__(self, day: str) -> None:
        self.day = day
        self.start_ns, self.end_ns = day_bounds_ns(day)
        self.cols = {kind: columns(kind) for kind in KINDS}
        # kind -> (ticker, sequence_number) -> versions (column tuples) in arrival order
        self.versions: dict[str, dict[tuple[str, int], list[tuple[Any, ...]]]] = {
            kind: {} for kind in KINDS}
        self.conflicts: list[Conflict] = []
        self.windows: list[Window] = []
        self.subscribed: dict[str, int] = {}  # symbol (or MARKET) -> first subscribe sent, ns
        self._resumed: dict[str, int] = {}  # symbol (or MARKET) -> latest subscribe sent, ns
        # (kind, ticker) -> (resume ns, recv_ns, SIP ns) of the first stream row after each resume
        self.first: dict[tuple[str, str], list[tuple[int, int, int]]] = {}
        # symbol (or MARKET) -> its gaps in drop order: rows sent while down are lost
        self.gaps: dict[str, list[Gap]] = {}
        self._last_sip: dict[tuple[str, str], int] = {}  # (kind, ticker) -> latest stream SIP ns
        # measurement counts: other_day, late, same_ms_trades / _quotes / _trade_quote
        self.counts: collections.Counter[str] = collections.Counter()
        self._cutoff = 0  # the latest cutoff a snapshot was taken at
        self._last_ms: dict[tuple[str, str], int] = {}  # (kind, ticker) -> last stream SIP ms
        self._lock = threading.Lock()

    def add(self, kind: str, rows: list[dict[str, Any]], recv_ns: int, source: str) -> int:
        """Keeps every row as a version received at `recv_ns` from `source`; returns how many
        were the first version of their key. A row of another New York day is counted, dropped."""
        cols = self.cols[kind]
        new = 0
        with self._lock:
            held = self.versions[kind]
            for row in rows:
                ts = row["sip_timestamp"]
                if not self.start_ns <= ts < self.end_ns:
                    self.counts["other_day"] += 1
                    continue
                version = tuple({**row, "recv_ns": recv_ns, "source": source}[c] for c in cols)
                key = (row["ticker"], row["sequence_number"])
                if ts < self._cutoff < recv_ns:
                    self.counts["late"] += 1
                if source == STREAM:
                    self._same_ms(kind, row["ticker"], ts // NS_PER_MS)
                    self._first(kind, row["ticker"], recv_ns, ts)
                    held_sip = self._last_sip.get((kind, row["ticker"]), ts)
                    self._last_sip[(kind, row["ticker"])] = max(held_sip, ts)
                versions = held.setdefault(key, [])
                if not versions:
                    new += 1
                else:
                    conflict = self._conflict(kind, versions[-1], version)
                    if conflict is not None:
                        self.conflicts.append(conflict)
                versions.append(version)
        return new

    def _same_ms(self, kind: str, ticker: str, ms: int) -> None:
        other = "quotes" if kind == "trades" else "trades"
        if self._last_ms.get((kind, ticker)) == ms:
            self.counts[f"same_ms_{kind}"] += 1
        if self._last_ms.get((other, ticker)) == ms:
            self.counts["same_ms_trade_quote"] += 1
        self._last_ms[(kind, ticker)] = ms

    def _first(self, kind: str, ticker: str, recv_ns: int, sip_ns: int) -> None:
        mark = max(self._resumed.get(ticker, -1), self._resumed.get(MARKET, -1))
        found = self.first.setdefault((kind, ticker), [])
        if not found or found[-1][0] != mark:
            found.append((mark, recv_ns, sip_ns))

    def _conflict(self, kind: str, a: tuple[Any, ...], b: tuple[Any, ...]) -> Conflict | None:
        cols = self.cols[kind]
        va, vb = dict(zip(cols, a, strict=True)), dict(zip(cols, b, strict=True))
        uncompared: tuple[str, ...] = ()
        lossy: tuple[str, ...] = ()
        if va["source"] == vb["source"]:
            names = [c for c in cols[:-len(EXTRA_COLUMNS)] if not _same(c, va[c], vb[c])]
        else:
            names = [c for c in CROSS_COMPARED[kind] if not _same(c, va[c], vb[c])]
            if va["sip_timestamp"] // NS_PER_MS != vb["sip_timestamp"] // NS_PER_MS:
                names.append("sip_timestamp")
            uncompared, lossy = CROSS_UNCOMPARED[kind], CROSS_LOSSY
        if not names:
            return None
        identity = UNCERTAIN if any(c in IDENTITY[kind] for c in names) else REVISION
        times = (va["sip_timestamp"], vb["sip_timestamp"])
        return Conflict(
            kind, va["ticker"], va["sequence_number"], tuple(names),
            (va["source"], va["recv_ns"]), (vb["source"], vb["recv_ns"]), identity,
            uncompared, lossy,
            inputs=identity == UNCERTAIN or any(c not in NOT_INPUTS[kind] for c in names),
            # whole milliseconds: a stream time is truncated to its millisecond
            sip_range=(min(times) // NS_PER_MS * NS_PER_MS,
                       (max(times) // NS_PER_MS + 1) * NS_PER_MS))

    def subscribe(self, keys: list[str], at_ns: int) -> None:
        """A subscription of `keys` was sent at `at_ns` (not proof of delivery: module text)."""
        with self._lock:
            for key in keys:
                self.subscribed.setdefault(key, at_ns)
                self._resumed[key] = at_ns

    def down(self, keys: list[str], down_ns: int, cause: str, mono_ns: int) -> None:
        """The stream of `keys` dropped at wall-clock `down_ns` (monotonic `mono_ns`); the gap
        stays open until `up`. It keeps the SIP time of the last row seen before it."""
        with self._lock:
            for key in keys:
                before = types.MappingProxyType(
                    {k: v for k, v in self._last_sip.items() if key in (MARKET, k[1])})
                self.gaps.setdefault(key, []).append(
                    Gap(cause, down_ns, mono_ns, None, None, before))

    def up(self, up_ns: int, mono_ns: int) -> None:
        """The subscription was sent again: every open gap ends its drop at `up_ns`; whether
        the events of the gap are covered is a question of SIP time (TickSnapshot.refusal)."""
        with self._lock:
            for key, gaps in self.gaps.items():
                for i, gap in enumerate(gaps):
                    if gap.up_ns is None:
                        gaps[i] = dataclasses.replace(gap, up_ns=up_ns, up_mono=mono_ns)
                        self._resumed[key] = up_ns

    def window(self, window: Window) -> None:
        with self._lock:
            self.windows.append(window)

    def snapshot(self, cutoff_ns: int) -> TickSnapshot:
        """What had arrived by `cutoff_ns` (receipt time), frozen for one decision."""
        rows: dict[str, tuple[tuple[Any, ...], ...]] = {}
        last: dict[str, tuple[int, int]] = {}  # ticker -> (last stream recv_ns, its SIP ns)
        with self._lock:
            self._cutoff = max(self._cutoff, cutoff_ns)
            for kind in KINDS:
                ts, seq = (self.cols[kind].index(c) for c in ("sip_timestamp", "sequence_number"))
                chosen = []
                for versions in self.versions[kind].values():
                    seen = [v for v in versions if v[-2] <= cutoff_ns]
                    if not seen:
                        continue
                    chosen.append(max(seen, key=lambda v: (v[-1] == REST, v[-2])))
                    for v in seen:
                        if v[-1] == STREAM and v[-2] >= last.get(v[0], (-1, 0))[0]:
                            last[v[0]] = (v[-2], v[ts])
                chosen.sort(key=lambda v: (v[0], v[ts], v[seq]))
                rows[kind] = tuple(chosen)
            gaps = {key: tuple(
                g if g.up_ns is not None and g.up_ns <= cutoff_ns
                else dataclasses.replace(g, up_ns=None, up_mono=None)
                for g in found if g.down_ns <= cutoff_ns) for key, found in self.gaps.items()}
            return TickSnapshot(
                day=self.day, start_ns=self.start_ns, cutoff_ns=cutoff_ns,
                rows=types.MappingProxyType(rows),
                conflicts=tuple(c for c in self.conflicts if c.later[1] <= cutoff_ns),
                windows=tuple(w for w in self.windows if w.recv_ns <= cutoff_ns),
                subscribed=types.MappingProxyType(
                    {k: v for k, v in self.subscribed.items() if v <= cutoff_ns}),
                gaps=types.MappingProxyType({k: v for k, v in gaps.items() if v}),
                last=types.MappingProxyType(last),
                first=types.MappingProxyType(
                    {k: tuple(f for f in v if f[1] <= cutoff_ns) for k, v in self.first.items()}))


@dataclass(frozen=True)
class TickSnapshot:
    """The ticks a decision saw: per key the version chosen as of `cutoff_ns` (module text), and
    the per-symbol delivery state at that cutoff. Immutable: later arrivals change the store,
    never a snapshot taken before them. `resolutions` pairs each conflict with the receipt time
    of the first complete window that resolves it by the cutoff (None while unresolved)."""

    day: str
    start_ns: int
    cutoff_ns: int
    rows: Mapping[str, tuple[tuple[Any, ...], ...]]
    conflicts: tuple[Conflict, ...]
    windows: tuple[Window, ...]
    subscribed: Mapping[str, int]
    gaps: Mapping[str, tuple[Gap, ...]]
    last: Mapping[str, tuple[int, int]]
    first: Mapping[tuple[str, str], tuple[tuple[int, int, int], ...]]

    @property
    def resolutions(self) -> tuple[tuple[Conflict, int | None], ...]:
        return tuple((c, self._resolved(c)) for c in self.conflicts)

    def _complete(self, symbol: str, kind: str) -> list[Window]:
        return [w for w in self.windows
                if w.symbol == symbol and w.kind == kind and w.cause is None]

    def _resolved(self, c: Conflict) -> int | None:
        return min((w.recv_ns for w in self._complete(c.ticker, c.kind)
                    if w.from_ns <= c.sip_range[0] and w.to_ns >= c.sip_range[1]
                    and w.recv_ns >= c.authority_ns), default=None)

    def frame(self, kind: str, symbol: str | None = None) -> Any:
        """The rows of one kind (one symbol or all) in ticker, SIP time, sequence order, with the
        flat-file columns followed by recv_ns and source of the chosen version, and
        `provisional`: True for a stream version (its STREAM_UNPROVEN columns unproven)."""
        import pandas as pd

        rows = [r + (r[-1] == STREAM,) for r in self.rows[kind]
                if symbol is None or r[0] == symbol]
        # object columns: an absent field stays None and a nanosecond time an exact int
        return pd.DataFrame(rows, columns=[*columns(kind), "provisional"], dtype=object)

    def lag(self, symbol: str) -> tuple[int, int] | None:
        """(wall-clock receipt ns, receipt minus SIP ns) of the symbol's latest stream version by
        the cutoff; None when none arrived. The difference estimates clock offset plus feed
        delay: recorded for measurement, never a limit and never coverage evidence."""
        if symbol not in self.last:
            return None
        recv, sip = self.last[symbol]
        return recv, recv - sip

    def _from(self, symbol: str, kind: str, gap: Gap | None) -> int:
        """SIP ns a repair of `gap` (None: the initial state) must start by: the millisecond of
        the last stream row of the kind seen before the drop, else New York midnight."""
        sip = None if gap is None else gap.before.get((kind, symbol))
        return self.start_ns if sip is None else sip // NS_PER_MS * NS_PER_MS

    def repair_from(self, symbol: str) -> int | None:
        """The earliest SIP ns a backfill must start at to repair the symbol's gaps by this
        cutoff, or None when it has none."""
        return min((self._from(symbol, kind, g) for g in self._gaps(symbol) for kind in KINDS),
                   default=None)

    def _gaps(self, symbol: str) -> tuple[Gap, ...]:
        return self.gaps.get(symbol, ()) + self.gaps.get(MARKET, ())

    def _coverage(self, symbol: str, gap: Gap | None, mark: int) -> str | None:
        """None when, for both kinds, a complete window from `_from` reaches past the SIP
        millisecond of the first stream row received after the (re)subscription at `mark`;
        "unverified" when no such stream row arrived yet (a send or an acknowledgment proves no
        delivery); "missing" when no complete window from `_from` (reaching past it) arrived."""
        for kind in KINDS:
            start = self._from(symbol, kind, gap)
            complete = [w for w in self._complete(symbol, kind) if w.from_ns <= start]
            if not complete:
                return "missing"
            sip = next((s for m, _, s in self.first.get((kind, symbol), ()) if m >= mark), None)
            if sip is None:
                return "unverified"
            if not any(w.to_ns >= (sip // NS_PER_MS + 1) * NS_PER_MS for w in complete):
                return "missing"
        return None

    def refusal(self, symbol: str) -> str | None:
        """Why the symbol's ticks are not proven complete from midnight to the cutoff (one
        cause), or None. A stream gap is filled by complete windows of both kinds from the last
        row seen before it past the first row seen after it, in SIP time; a conflict
        refuses while it touches an input and is unresolved (module text)."""
        since = min((self.subscribed[k] for k in (symbol, MARKET) if k in self.subscribed),
                    default=None)
        if since is None:
            return "tick_not_subscribed"
        initial = self._coverage(symbol, None, since)
        if initial == "missing":
            return ("tick_backfill_incomplete"
                    if any(w.cause for w in self.windows if w.symbol == symbol)
                    else "tick_no_initial_state")
        if initial is not None:
            return "tick_coverage_unverified"
        for gap in self._gaps(symbol):
            found = "missing" if gap.up_ns is None else self._coverage(symbol, gap, gap.up_ns)
            if found is not None:
                return "tick_gap" if found == "missing" else "tick_coverage_unverified"
        if any(c.ticker == symbol and c.inputs and r is None for c, r in self.resolutions):
            return "tick_conflict"
        return None

    def require(self, symbol: str) -> None:
        cause = self.refusal(symbol)
        if cause is not None:
            raise ModelRefused(cause, f"{symbol} as of {self.cutoff_ns}")

    def require_ranks(self, symbols: list[str]) -> None:
        """Ranks over `symbols` depend on every member: unavailable while one is refused."""
        refused = {s: c for s in sorted(symbols) if (c := self.refusal(s)) is not None}
        if refused:
            raise ModelRefused("tick_ranks_unavailable", ", ".join(
                f"{s}: {c}" for s, c in refused.items()) + f" as of {self.cutoff_ns}")


# --- the live stream ------------------------------------------------------------------------


class TickStream:
    """Trades and quotes from the real-time stream into a TickStore: per symbol (a set that
    grows during the day, `add`) or the full market, by `[massive] tick_subscription`.
    `connect` is `websockets.sync.client.connect` (a stand-in in the tests); `clock` gives
    wall-clock ns (receipt times), `mono` monotonic ns (durations). A subscription is recorded
    when it is sent; neither the send nor an acknowledgment proves delivery
    (TickSnapshot.refusal)."""

    def __init__(self, cfg: Config, store: TickStore, connect: Callable[..., Any] | None = None,
                 clock: Callable[[], int] = time.time_ns,
                 mono: Callable[[], int] = time.monotonic_ns) -> None:
        self.m = cfg.massive
        self.market = self.m.tick_subscription == "market"
        self.store = store
        self.clock = clock
        self.mono = mono
        if connect is None:
            from websockets.sync.client import connect as ws_connect

            connect = ws_connect
        self.connect = connect
        self.symbols: set[str] = set()
        self._ws: Any = None  # the connection while logged in
        self._lock = threading.Lock()

    @staticmethod
    def _params(symbols: list[str]) -> str:
        return ",".join(f"{CHANNELS[k]}.{s}" for s in symbols for k in KINDS)

    def add(self, symbol: str) -> bool:
        """Subscribes `symbol` now if logged in, else at the next login; True when it is new.
        With the full-market subscription every symbol already arrives."""
        if not isinstance(symbol, str) or not symbol or symbol == MARKET or "," in symbol:
            raise ModelRefused("tick_symbol", repr(symbol))
        with self._lock:
            if symbol in self.symbols:
                return False
            self.symbols.add(symbol)
            if self._ws is not None and not self.market:
                self._ws.send(json.dumps({"action": "subscribe",
                                          "params": self._params([symbol])}))
                self.store.subscribe([symbol], self.clock())
            return True

    def _keys(self) -> list[str]:
        return [MARKET] if self.market else sorted(self.symbols)

    def _login(self, ws: Any) -> None:
        with self._lock:
            self._ws = ws
            keys = self._keys()
            if keys:
                ws.send(json.dumps({"action": "subscribe", "params": self._params(keys)}))
                now = self.clock()
                self.store.subscribe(keys, now)
                self.store.up(now, self.mono())

    def session(self, stop: threading.Event) -> None:
        """One connection: auth, subscribe, read until it closes or `stop` is set."""
        url = f"wss://{self.m.stream_host}{self.m.stream_path}"
        try:
            with self.connect(url, max_size=self.m.stream_max_message_bytes) as ws:
                ws.send(json.dumps({"action": "auth",
                                    "params": credential(self.m.api_key_variable)}))
                while not stop.is_set():
                    try:
                        raw = ws.recv(timeout=float(self.m.stream_reconnect_seconds))
                    except TimeoutError:
                        continue
                    arrived = self.clock()
                    batch = json.loads(raw)
                    for message in batch if isinstance(batch, list) else [batch]:
                        self._read(ws, message, arrived)
        finally:
            with self._lock:
                self._ws = None

    def _read(self, ws: Any, message: Any, arrived: int) -> None:
        if not isinstance(message, dict):
            raise ModelRefused("tick_unreadable", "a stream event that is not an object")
        ev = message.get("ev")
        if ev == "status":
            status = message.get("status")
            if status == "auth_success":
                self._login(ws)
            elif status in ("auth_failed", "max_connections", "error"):
                raise ModelRefused("stream_refused", str(status))
            return
        kind = {"T": "trades", "Q": "quotes"}.get(ev)  # type: ignore[arg-type]
        if kind is None:
            return
        if self._ws is None:
            raise ModelRefused("tick_unreadable", "ticks before auth")
        self.store.add(kind, [stream_row(kind, message)], arrived, STREAM)

    def run(self, stop: threading.Event) -> None:
        """Connect, and connect again after a drop until `stop`; every drop opens a gap of every
        subscribed symbol (MARKET for the full market) that the next subscription closes."""
        while not stop.is_set():
            cause = "closed"
            try:
                self.session(stop)
            except ModelRefused:
                raise
            except Exception as exc:  # noqa: BLE001 - any transport end is a gap, never a crash
                cause = type(exc).__name__
            if stop.is_set():
                return
            with self._lock:
                keys = self._keys()
            self.store.down(keys, self.clock(), cause, self.mono())
            stop.wait(float(self.m.stream_reconnect_seconds))


# --- the backfill of a symbol that joins late or of a stream gap ---------------------------


class TickBackfill:
    """A symbol's trades and quotes of a SIP time range from Massive's REST, paged to the end,
    into the TickStore as REST versions with each page's receipt time. One request per page,
    never retried. Each kind's outcome is a `Window` kept in the store: complete only when the
    final page came without next_url. Paging has no count limit (none is measured): a page
    without progress (no rows, a last SIP time that does not pass the previous page's, or a
    next_url fetched before) ends the window as incomplete (`backfill_no_progress`) with the
    rows received kept. A failed or unreadable answer refuses, keeps no row of the backfill and
    records the window as incomplete with that cause. `requests` and `rate_limited` count every
    request and every 429 answer (measurement)."""

    def __init__(self, cfg: Config, store: TickStore, opener: Opener = _open,
                 clock: Callable[[], int] = time.time_ns,
                 mono: Callable[[], int] = time.monotonic_ns) -> None:
        self.m = cfg.massive
        self.store = store
        self.opener = opener
        self.clock = clock
        self.mono = mono
        self.requests = 0
        self.rate_limited = 0

    def _get(self, url: str) -> bytes:
        self.requests += 1
        headers = {"Authorization": "Bearer " + credential(self.m.api_key_variable)}
        try:
            return _get(self.opener, url, headers, self.m.http_timeout_seconds)
        except ModelRefused as exc:
            if exc.cause == "http_status" and exc.detail.split(" ")[0] == HTTP_TOO_MANY_REQUESTS:
                self.rate_limited += 1
            raise

    def _pages(self, kind: str, symbol: str, from_ns: int, to_ns: int,
               pages: list[tuple[list[dict[str, Any]], int]]) -> str | None:
        """Fetches into `pages`; returns None when exhaustion is proven, else the cause."""
        path = f"/v3/{kind}/{urllib.parse.quote(symbol, safe='')}"
        query = {"timestamp.gte": str(from_ns), "timestamp.lt": str(to_ns), "order": "asc",
                 "sort": "timestamp", "limit": str(self.m.tick_page_limit)}
        url = f"https://{self.m.api_host}{path}?{urllib.parse.urlencode(query)}"
        seen = {url}
        last_ts: int | None = None
        while True:
            body = self._get(url)
            recv_ns = self.clock()
            try:
                page = json.loads(body)
            except json.JSONDecodeError as exc:
                raise ModelRefused("backfill_unreadable", path) from exc
            if (not isinstance(page, dict) or page.get("status") != "OK"
                    or not isinstance(page.get("results", []), list)):
                raise ModelRefused("backfill_unreadable", f"{path}: status "
                                   f"{page.get('status') if isinstance(page, dict) else None}")
            rows = [rest_row(kind, symbol, r) for r in page.get("results", [])]
            if any(not from_ns <= r["sip_timestamp"] < to_ns for r in rows):
                raise ModelRefused("backfill_unreadable", f"{path}: a row outside the range")
            pages.append((rows, recv_ns))
            nxt = page.get("next_url")
            if not nxt:
                return None
            parts = urllib.parse.urlsplit(nxt)
            if parts.scheme != "https" or parts.hostname != self.m.api_host:
                # the next request carries the key: only to the configured host over https
                raise ModelRefused("backfill_unreadable", "next page not on the https api host")
            page_last = max((r["sip_timestamp"] for r in rows), default=None)
            if (page_last is None or (last_ts is not None and page_last <= last_ts)
                    or nxt in seen):
                return "backfill_no_progress"  # a stalled cursor is not proof of the end
            last_ts = page_last
            seen.add(nxt)
            url = nxt

    def backfill(self, symbol: str, from_ns: int, to_ns: int) -> dict[str, Window]:
        """Trades and quotes of `symbol` with SIP time in [from_ns, to_ns); returns each kind's
        window (see TickSnapshot.refusal for how a decision reads it)."""
        if (not isinstance(symbol, str) or not symbol or isinstance(from_ns, bool)
                or isinstance(to_ns, bool) or not isinstance(from_ns, int)
                or not isinstance(to_ns, int) or from_ns >= to_ns):
            raise ModelRefused("backfill_range", f"{symbol!r} {from_ns!r}..{to_ns!r}")
        fetched: dict[str, tuple[list[tuple[list[dict[str, Any]], int]], str | None, int]] = {}
        for kind in KINDS:
            pages: list[tuple[list[dict[str, Any]], int]] = []
            began = self.mono()
            try:
                cause = self._pages(kind, symbol, from_ns, to_ns, pages)
            except ModelRefused as exc:
                self.store.window(Window(symbol, kind, from_ns, to_ns, self.clock(),
                                         len(pages), exc.cause, self.mono() - began))
                raise
            fetched[kind] = (pages, cause, self.mono() - began)
        windows = {}
        for kind, (pages, cause, elapsed) in fetched.items():
            for rows, recv_ns in pages:
                self.store.add(kind, rows, recv_ns, REST)
            windows[kind] = Window(symbol, kind, from_ns, to_ns, pages[-1][1], len(pages), cause,
                                   elapsed)
            self.store.window(windows[kind])
        return windows

    def initial(self, symbol: str) -> dict[str, Window]:
        """The symbol's day so far, from 00:00 America/New_York of the session date (the day
        file's start: the converter carries a pre-04:00 quote) to now. Subscribe the symbol
        first and fetch after its first stream row of each kind arrived, so the window reaches
        past it and the hand-over is verified (TickSnapshot.refusal)."""
        return self.backfill(symbol, self.store.start_ns, min(self.clock(), self.store.end_ns))
