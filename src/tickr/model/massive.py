"""Massive, the model's market data source (owner 2026-11-20: the features come live from the
same vendor and the same files as the training; Webull stays for orders and the bot's lists).

Three inputs, each refused with one cause rather than guessed:

- **History** (`History`): the minute-bar day files of the previous 20 trading sessions, fetched
  from Massive's flat files: the same `minute_aggs` files, byte for byte, the training reads.
  Each file is checked by the training's own bar rules (`features.bars_frame`) before it is
  kept; a trading day whose file is missing refuses (`history_file_missing`). Only the current
  window is kept on disk; an older day file is deleted (owner 2026-11-22).
- **Reference** (`Reference`): the splits of the window (to put the carried state on today's
  scale, as the training does) and the tickers of the training's stock types (`[massive]
  stock_types`, the training's types file holds common stocks and ADRs).
- **Today** (`MinuteStream` into `DayBars`): the real-time full-market minute aggregates
  (channel `AM.*`), kept as rows of the flat file's columns, so the shared feature code gets the
  same frame live as in training. A dropped connection marks the day gapped
  (`DayBars.gaps`): bars sent while it was down are lost, so the caller refuses to decide on
  that day rather than score an incomplete market.

Every host, bucket, path, variable name and number comes from `[massive]` in
`config/bot.toml`. Credentials are read by variable NAME at the moment of a request, sent only
in request headers, and never printed, logged or stored (hard rule 4); a missing one refuses
with its name (`credential_missing`).

Not decided here (measured first, owner rule): how long after a minute ends its bars count as
complete. `DayBars` records every message's arrival so the wait can be measured on real days.
"""

from __future__ import annotations

import datetime
import gzip
import hashlib
import hmac
import io
import json
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import zoneinfo
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from tickr.calendar import CalendarRefused, SessionCalendar
from tickr.config import Config, MassiveConfig
from tickr.model import features
from tickr.model.errors import ModelRefused

NEW_YORK = zoneinfo.ZoneInfo("America/New_York")
NS_PER_MS = 10**6
EMPTY_SHA256 = hashlib.sha256(b"").hexdigest()
# The flat-file columns the feature code reads (features.BAR_COLUMNS). The day file also holds
# `transactions`, which the AM stream does not send and no feature reads, so the live frame
# leaves it out instead of inventing a value.
FILE_COLUMNS = features.BAR_COLUMNS
# AM message key -> flat-file column (Massive stream: sym, v, o, c, h, l, s = start in ms).
AM_KEYS = {"sym": "ticker", "v": "volume", "o": "open", "c": "close", "h": "high", "l": "low"}
HTTP_NOT_FOUND = 404


def credential(name: str) -> str:
    """The value of one named credential variable, or a refusal naming the variable."""
    value = os.environ.get(name)
    if not value:
        raise ModelRefused("credential_missing", name)
    return value


# --- HTTP ---------------------------------------------------------------------------------------

Opener = Callable[[urllib.request.Request, int], Any]


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """A 3xx answer is refused, never followed: urllib would resend the request's headers, the
    key among them, to whatever host the Location names (hard rule 4)."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        return None  # urllib then raises HTTPError with the 3xx status


_OPENER = urllib.request.build_opener(_NoRedirect())


def _open(request: urllib.request.Request, timeout: int) -> Any:
    return _OPENER.open(request, timeout=timeout)


def _get(opener: Opener, url: str, headers: Mapping[str, str], timeout: int) -> bytes:
    request = urllib.request.Request(url, headers=dict(headers), method="GET")
    try:
        with opener(request, timeout) as response:
            return response.read()
    except urllib.error.HTTPError as exc:
        if exc.code == HTTP_NOT_FOUND:
            raise ModelRefused("not_found", urllib.parse.urlsplit(url).path) from exc
        raise ModelRefused("http_status", f"{exc.code} {urllib.parse.urlsplit(url).path}") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise ModelRefused("http_failed", type(exc).__name__) from exc


def head(opener: Opener, url: str, headers: Mapping[str, str],
         timeout: int) -> tuple[int, Mapping[str, str]]:
    """(status, response headers) of a HEAD of `url` (the prev-day probe); an HTTP error
    status is returned as the answer, a failed request refuses (`http_failed`)."""
    request = urllib.request.Request(url, headers=dict(headers), method="HEAD")
    try:
        with opener(request, timeout) as response:
            return response.status, response.headers
    except urllib.error.HTTPError as exc:
        return exc.code, {}
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise ModelRefused("http_failed", type(exc).__name__) from exc


def sigv4_headers(*, host: str, path: str, region: str, key_id: str, secret: str,
                  moment: datetime.datetime, extra: Mapping[str, str] | None = None,
                  payload_sha256: str = EMPTY_SHA256, method: str = "GET") -> dict[str, str]:
    """AWS Signature Version 4 headers of a `method` (GET, or HEAD for the prev-day probe) of
    `path` (already URI-encoded) on an S3-compatible `host` without a query. Massive's flat
    files are served this way."""
    amz_date = moment.astimezone(datetime.UTC).strftime("%Y%m%dT%H%M%SZ")
    scope_day = amz_date[:8]
    headers = {"host": host, "x-amz-content-sha256": payload_sha256, "x-amz-date": amz_date}
    headers.update({k.lower(): v for k, v in (extra or {}).items()})
    names = sorted(headers)
    canonical = "\n".join([
        method, path, "",
        "".join(f"{n}:{headers[n].strip()}\n" for n in names),
        ";".join(names), payload_sha256])
    scope = f"{scope_day}/{region}/s3/aws4_request"
    to_sign = "\n".join(["AWS4-HMAC-SHA256", amz_date, scope,
                         hashlib.sha256(canonical.encode()).hexdigest()])
    key = f"AWS4{secret}".encode()
    for part in (scope_day, region, "s3", "aws4_request"):
        key = hmac.new(key, part.encode(), hashlib.sha256).digest()
    signature = hmac.new(key, to_sign.encode(), hashlib.sha256).hexdigest()
    headers["authorization"] = (f"AWS4-HMAC-SHA256 Credential={key_id}/{scope}, "
                                f"SignedHeaders={';'.join(names)}, Signature={signature}")
    headers.pop("host")
    return headers


# --- history: the previous sessions' minute-bar day files ----------------------------------------


def previous_sessions(calendar: SessionCalendar, today: datetime.date, count: int,
                      lookback_days: int) -> list[str]:
    """The `count` trading sessions before `today`, oldest first, from the bot's calendar (a
    date the calendar does not cover refuses through CalendarRefused)."""
    days: list[str] = []
    day = today
    for _ in range(lookback_days):
        day -= datetime.timedelta(days=1)
        try:
            calendar.day(day)
        except CalendarRefused as exc:
            if exc.cause in ("WEEKEND", "HOLIDAY"):
                continue
            raise ModelRefused("calendar", exc.detail) from exc
        days.append(day.isoformat())
        if len(days) == count:
            return list(reversed(days))
    raise ModelRefused("calendar", f"fewer than {count} sessions before {today.isoformat()}")


class History:
    """The minute-bar day files of the window, kept under `<data_dir>/<history_dir>`."""

    def __init__(self, cfg: Config, opener: Opener = _open,
                 now: Callable[[], datetime.datetime] | None = None) -> None:
        self.m: MassiveConfig = cfg.massive
        rel = Path(self.m.history_dir)
        if not rel.parts or rel.is_absolute() or ".." in rel.parts:  # "" and "." have no parts
            # window() deletes the day files outside the window in this folder
            raise ModelRefused("history_dir_outside", "[massive] history_dir must be relative")
        self.folder = cfg.root / cfg.run.data_dir / rel
        self.opener = opener
        self.now = now or (lambda: datetime.datetime.now(datetime.UTC))

    def path(self, day: str) -> Path:
        return self.folder / f"{day}.csv.gz"

    def fetch(self, day: str) -> Path:
        """The day file, downloaded once and checked by the training's bar rules."""
        target = self.path(day)
        if target.is_file():
            return target
        key = f"{self.m.minute_aggs_prefix}/{day[:4]}/{day[5:7]}/{day}.csv.gz"
        path = "/" + urllib.parse.quote(f"{self.m.files_bucket}/{key}")
        headers = sigv4_headers(
            host=self.m.files_host, path=path, region=self.m.files_region,
            key_id=credential(self.m.files_key_id_variable),
            secret=credential(self.m.files_secret_variable), moment=self.now())
        try:
            body = _get(self.opener, f"https://{self.m.files_host}{path}", headers,
                        self.m.http_timeout_seconds)
        except ModelRefused as exc:
            if exc.cause == "not_found":
                raise ModelRefused("history_file_missing", day) from exc
            raise
        read_day_file(body, day)  # refuses before anything is kept
        self.folder.mkdir(parents=True, exist_ok=True)
        part = target.with_suffix(".part")
        part.write_bytes(body)
        os.replace(part, target)
        return target

    def window(self, days: Iterable[str]) -> list[tuple[str, Any]]:
        """(day, bar frame) of every day, fetched where missing; day files outside the window
        are deleted afterwards."""
        days = list(days)
        out = [(day, read_day_file(self.fetch(day).read_bytes(), day)) for day in days]
        keep = {self.path(day).name for day in days}
        for stale in self.folder.glob("*.csv.gz"):
            if stale.name not in keep:
                stale.unlink()
        return out


def read_day_file(body: bytes, day: str) -> Any:
    """A minute_aggs day file as the training's reader parses it (ticker as text, nothing turned
    into a missing value), checked by `features.bars_frame`; returns the raw frame."""
    import pandas as pd

    try:
        frame = pd.read_csv(io.BytesIO(gzip.decompress(body)), dtype={"ticker": str},
                            na_filter=False)
    except Exception as exc:  # noqa: BLE001 - any unreadable file is one refusal
        raise ModelRefused("history_file_unreadable", f"{day}: {type(exc).__name__}") from exc
    features.bars_frame(frame, day)
    return frame


# --- reference: splits and stock types -----------------------------------------------------------


class Reference:
    """Massive's REST reference lists, paged to the end or refused as incomplete."""

    def __init__(self, cfg: Config, opener: Opener = _open) -> None:
        self.m = cfg.massive
        self.opener = opener

    def _pages(self, path: str, query: Mapping[str, str]) -> list[dict[str, Any]]:
        headers = {"Authorization": "Bearer " + credential(self.m.api_key_variable)}
        url = (f"https://{self.m.api_host}{path}?"
               + urllib.parse.urlencode({**query, "limit": str(self.m.page_limit)}))
        rows: list[dict[str, Any]] = []
        for _ in range(self.m.max_pages):
            try:
                page = json.loads(_get(self.opener, url, headers, self.m.http_timeout_seconds))
            except json.JSONDecodeError as exc:
                raise ModelRefused("reference_unreadable", path) from exc
            if not isinstance(page, dict) or not isinstance(page.get("results", []), list):
                raise ModelRefused("reference_unreadable", path)
            rows += page.get("results", [])
            nxt = page.get("next_url")
            if not nxt:
                return rows
            parts = urllib.parse.urlsplit(nxt)
            if parts.scheme != "https" or parts.hostname != self.m.api_host:
                # the next request carries the key: only to the configured host over https
                raise ModelRefused("reference_unreadable", "next page not on the https api host")
            url = nxt
        raise ModelRefused("reference_incomplete", f"{path}: more than {self.m.max_pages} pages")

    def listing(self, path: str, query: Mapping[str, str]) -> list[dict[str, Any]]:
        """Every row of one REST listing on the API host, paged to the end, or a refusal."""
        return self._pages(path, query)

    def splits(self, first: str, last: str) -> dict[str, list[tuple[str, float]]]:
        """Execution date -> [(ticker, split_from / split_to)] for first..last (the training's
        factor, l1labels.splits_parse)."""
        out: dict[str, list[tuple[str, float]]] = {}
        rows = self._pages("/v3/reference/splits", {"execution_date.gte": first,
                                                    "execution_date.lte": last})
        for row in rows:
            try:
                day, ticker = row["execution_date"], row["ticker"]
                factor = row["split_from"] / row["split_to"]
            except (KeyError, TypeError, ZeroDivisionError) as exc:
                raise ModelRefused("reference_unreadable", "a split row") from exc
            out.setdefault(day, []).append((ticker, factor))
        return out

    def stock_tickers(self) -> set[str]:
        """Active tickers of the training's stock types."""
        tickers: set[str] = set()
        for kind in self.m.stock_types:
            rows = self._pages("/v3/reference/tickers",
                               {"market": "stocks", "type": kind, "active": "true"})
            for row in rows:
                ticker = row.get("ticker") if isinstance(row, dict) else None
                if not isinstance(ticker, str) or not ticker.strip():
                    raise ModelRefused("reference_unreadable", "a ticker row without a ticker")
                tickers.add(ticker)
        return tickers


# --- today: the real-time minute aggregates -----------------------------------------------------


@dataclass
class DayBars:
    """Today's full-market minute bars from the stream, as flat-file rows. Thread-safe: the
    stream thread adds, the decision side reads."""

    day: str
    rows: dict[tuple[str, int], tuple[Any, ...]] = field(default_factory=dict)
    arrivals: dict[int, float] = field(default_factory=dict)  # bar start (ms) -> latest arrival
    # (down, up) monotonic times and the type of what ended the connection
    gaps: list[tuple[float, float, str]] = field(default_factory=list)
    other_day: int = 0
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def add(self, message: Mapping[str, Any], arrived: float) -> None:
        """One AM message; a bar of another New York day is counted and dropped (a stream
        that runs past midnight), a malformed one refused."""
        try:
            start_ms = int(message["s"])
            row = tuple(message[k] for k in AM_KEYS)
        except (KeyError, TypeError, ValueError) as exc:
            raise ModelRefused("stream_message_unreadable", "an AM message") from exc
        stamp = datetime.datetime.fromtimestamp(start_ms / 1000, NEW_YORK)
        with self._lock:
            if stamp.date().isoformat() != self.day:
                self.other_day += 1
                return
            ticker, volume, o, c, h, low = row
            self.rows[(ticker, start_ms)] = (ticker, volume, o, c, h, low,
                                             start_ms * NS_PER_MS)
            self.arrivals[start_ms] = max(self.arrivals.get(start_ms, 0.0), arrived)

    def gap(self, down: float, up: float, cause: str) -> None:
        with self._lock:
            self.gaps.append((down, up, cause))

    def frame(self) -> Any:
        """Every bar so far as a flat-file frame (the shared feature code checks it)."""
        import pandas as pd

        with self._lock:
            rows = list(self.rows.values())
        return pd.DataFrame(rows, columns=list(FILE_COLUMNS))


class BarSink(Protocol):
    """Where the stream hands its bars and gaps: `DayBars` for the model, the feed-arrival
    recorder (`tickr.model.arrival`) for the measurement."""

    def add(self, message: Mapping[str, Any], arrived: Any) -> None: ...

    def gap(self, down: Any, up: Any, cause: str) -> None: ...


class MinuteStream:
    """The real-time stream of full-market minute aggregates into a DayBars (or another
    `BarSink`). `connect` is `websockets.sync.client.connect` (a stand-in in the tests);
    `clock` stamps each received batch and both ends of a gap."""

    def __init__(self, cfg: Config, bars: BarSink, connect: Callable[..., Any] | None = None,
                 clock: Callable[[], Any] = time.monotonic) -> None:
        self.m = cfg.massive
        self.bars = bars
        self.clock = clock
        if connect is None:
            from websockets.sync.client import connect as ws_connect

            connect = ws_connect
        self.connect = connect

    def session(self, stop: threading.Event) -> None:
        """One connection: auth, subscribe, read until it closes or `stop` is set. Returns on a
        normal end; refuses on a status the stream cannot go on with."""
        url = f"wss://{self.m.stream_host}{self.m.stream_path}"
        with self.connect(url, max_size=self.m.stream_max_message_bytes) as ws:
            ws.send(json.dumps({"action": "auth",
                                "params": credential(self.m.api_key_variable)}))
            authed = False
            while not stop.is_set():
                try:
                    raw = ws.recv(timeout=float(self.m.stream_reconnect_seconds))
                except TimeoutError:
                    continue
                arrived = self.clock()
                batch = json.loads(raw)
                # the server bundles events into a JSON array; a single event may come bare
                for message in batch if isinstance(batch, list) else [batch]:
                    if message.get("ev") == "status":
                        status = message.get("status")
                        if status == "auth_success":
                            authed = True
                            ws.send(json.dumps({"action": "subscribe", "params": "AM.*"}))
                        elif status in ("auth_failed", "max_connections", "error"):
                            raise ModelRefused("stream_refused", str(status))
                    elif message.get("ev") == "AM":
                        if not authed:
                            raise ModelRefused("stream_message_unreadable", "bars before auth")
                        self.bars.add(message, arrived)

    def run(self, stop: threading.Event) -> None:
        """Connect, and connect again after a drop until `stop`; every drop is a gap."""
        while not stop.is_set():
            down, cause = None, "closed"
            try:
                self.session(stop)
            except ModelRefused:
                raise
            except Exception as exc:  # noqa: BLE001 - any transport end is a gap, never a crash
                down, cause = self.clock(), type(exc).__name__
            if stop.is_set():
                return
            down = down if down is not None else self.clock()
            stop.wait(float(self.m.stream_reconnect_seconds))
            self.bars.gap(down, self.clock(), cause)
