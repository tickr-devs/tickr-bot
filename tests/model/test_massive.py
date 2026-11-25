"""Massive inputs: flat-file history, reference lists, the minute stream; no network (stand-ins)."""

from __future__ import annotations

import datetime
import gzip
import io
import json
import threading
import urllib.error
from pathlib import Path

import model_bars
import numpy as np
import pytest

from tickr import config
from tickr.calendar import SessionCalendar
from tickr.model import features, massive
from tickr.model.errors import ModelRefused

ROOT = Path(__file__).resolve().parents[2]
CFG = config.load(ROOT / "config" / "bot.toml")
KEY, KEY_ID, SECRET = "api-canary-1", "id-canary-2", "secret-canary-3"


@pytest.fixture
def creds(monkeypatch):
    monkeypatch.setenv(CFG.massive.api_key_variable, KEY)
    monkeypatch.setenv(CFG.massive.files_key_id_variable, KEY_ID)
    monkeypatch.setenv(CFG.massive.files_secret_variable, SECRET)


def cfg_at(tmp_path: Path) -> config.Config:
    return config.load(ROOT / "config" / "bot.toml", root=tmp_path)


class Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class Wire:
    """Stand-in for urlopen: path -> body (bytes) or status code (int)."""

    def __init__(self, routes):
        self.routes = routes
        self.requests = []

    def __call__(self, request, timeout):
        self.requests.append(request)
        body = self.routes.get(request.full_url, self.routes.get(request.selector))
        if body is None or isinstance(body, int):
            raise urllib.error.HTTPError(request.full_url, body or 404, "x", {}, None)
        return Response(body)


def gz(frame) -> bytes:
    buf = io.BytesIO()
    with gzip.GzipFile(fileobj=buf, mode="wb") as f:
        f.write(frame.to_csv(index=False).encode())
    return buf.getvalue()


def test_sigv4_matches_the_aws_documented_get_object_example():
    """AWS S3 documentation, "Signature Calculations ... Example: GET Object"."""
    headers = massive.sigv4_headers(
        host="examplebucket.s3.amazonaws.com", path="/test.txt", region="us-east-1",
        key_id="AKIAIOSFODNN7EXAMPLE", secret="wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
        moment=datetime.datetime(2013, 5, 24, tzinfo=datetime.UTC),
        extra={"Range": "bytes=0-9"})
    assert headers["authorization"] == (
        "AWS4-HMAC-SHA256 Credential=AKIAIOSFODNN7EXAMPLE/20130524/us-east-1/s3/aws4_request, "
        "SignedHeaders=host;range;x-amz-content-sha256;x-amz-date, "
        "Signature=f0e8bdb87c964420e857bd35b5d6ed310bd44f0170aba48dd91039c6036bdb41")


def test_previous_sessions_come_from_the_bot_calendar():
    cal = SessionCalendar.load(ROOT / "config" / "calendar")
    days = massive.previous_sessions(cal, datetime.date(2026, 11, 25), 20,
                                     CFG.massive.calendar_lookback_days)
    assert len(days) == 20 and days[-1] == "2026-11-24" and days == sorted(days)
    assert all(datetime.date.fromisoformat(d).isoweekday() < 6 for d in days)
    with pytest.raises(ModelRefused) as exc:
        massive.previous_sessions(cal, datetime.date(2026, 1, 10), 20,
                                 CFG.massive.calendar_lookback_days)
    assert exc.value.cause == "calendar"


def test_a_missing_credential_is_named_never_valued(monkeypatch):
    monkeypatch.delenv(CFG.massive.api_key_variable, raising=False)
    with pytest.raises(ModelRefused) as exc:
        massive.credential(CFG.massive.api_key_variable)
    assert exc.value.cause == "credential_missing"
    assert exc.value.detail == CFG.massive.api_key_variable


def test_history_fetches_the_training_day_files_and_keeps_only_the_window(tmp_path, creds):
    cfg = cfg_at(tmp_path)
    market = model_bars.market(3)
    prefix = f"/{cfg.massive.files_bucket}/{cfg.massive.minute_aggs_prefix}"
    routes = {f"{prefix}/{d[:4]}/{d[5:7]}/{d}.csv.gz": gz(f) for d, f in market}
    wire = Wire(routes)
    history = massive.History(cfg, wire)
    history.folder.mkdir(parents=True)
    (history.folder / "2020-01-02.csv.gz").write_bytes(b"old")
    got = history.window([d for d, _ in market])
    assert [d for d, _ in got] == [d for d, _ in market]
    assert sorted(p.name for p in history.folder.iterdir()) == [f"{d}.csv.gz" for d, _ in market]
    for request in wire.requests:
        assert request.get_header("Authorization").startswith(
            f"AWS4-HMAC-SHA256 Credential={KEY_ID}/")
        assert SECRET not in json.dumps(dict(request.header_items()))
        assert request.host == cfg.massive.files_host
    wire.requests.clear()
    history.window([d for d, _ in market])
    assert wire.requests == []  # kept files are not fetched again
    last = market[-1][0]
    body = next(v for k, v in routes.items() if k.endswith(f"/{last}.csv.gz"))
    assert got[-1][1].equals(massive.read_day_file(body, last))  # the file's own values


def test_a_missing_or_bad_history_file_is_refused_and_not_kept(tmp_path, creds):
    cfg = cfg_at(tmp_path)
    day, frame = model_bars.market(1)[0]
    history = massive.History(cfg, Wire({}))
    with pytest.raises(ModelRefused) as exc:
        history.fetch(day)
    assert exc.value.cause == "history_file_missing"
    bad = frame.copy()
    bad.loc[3, "close"] = 0.0
    key = f"/{cfg.massive.files_bucket}/{cfg.massive.minute_aggs_prefix}/{day[:4]}/{day[5:7]}"
    history = massive.History(cfg, Wire({f"{key}/{day}.csv.gz": gz(bad)}))
    with pytest.raises(ModelRefused) as exc:
        history.fetch(day)
    assert exc.value.cause == "bar_value_invalid"
    assert not history.path(day).exists()
    history = massive.History(cfg, Wire({f"{key}/{day}.csv.gz": b"not gzip"}))
    with pytest.raises(ModelRefused) as exc:
        history.fetch(day)
    assert exc.value.cause == "history_file_unreadable"


def test_splits_are_paged_and_give_the_training_factor(creds):
    host = f"https://{CFG.massive.api_host}"
    first = (f"{host}/v3/reference/splits?execution_date.gte=2026-11-01&"
             f"execution_date.lte=2026-11-25&limit={CFG.massive.page_limit}")
    second = f"{host}/v3/reference/splits?cursor=abc"
    wire = Wire({
        first: json.dumps({"results": [{"execution_date": "2026-11-20", "ticker": "AAA",
                                        "split_from": 1, "split_to": 4}],
                           "next_url": second}).encode(),
        second: json.dumps({"results": [{"execution_date": "2026-11-20", "ticker": "BBB",
                                         "split_from": 10, "split_to": 1}]}).encode()})
    got = massive.Reference(CFG, wire).splits("2026-11-01", "2026-11-25")
    assert got == {"2026-11-20": [("AAA", 0.25), ("BBB", 10.0)]}
    assert all(r.get_header("Authorization") == f"Bearer {KEY}" for r in wire.requests)


def test_a_next_page_on_another_host_or_too_many_pages_is_refused(creds, monkeypatch):
    host = f"https://{CFG.massive.api_host}"
    url = f"{host}/v3/reference/tickers?market=stocks&type=CS&active=true&limit=1000"
    for nxt in ("https://elsewhere.test/x", f"http://{CFG.massive.api_host}/x"):
        wire = Wire({url: json.dumps({"results": [], "next_url": nxt}).encode()})
        with pytest.raises(ModelRefused) as exc:
            massive.Reference(CFG, wire).stock_tickers()
        assert exc.value.cause == "reference_unreadable"
        assert len(wire.requests) == 1  # the key never went to the next page
    loop = Wire({url: json.dumps({"results": [{"ticker": "A"}], "next_url": url}).encode()})
    with pytest.raises(ModelRefused) as exc:
        massive.Reference(CFG, loop).stock_tickers()
    assert exc.value.cause == "reference_incomplete"


def test_stock_tickers_take_every_training_type(creds):
    host = f"https://{CFG.massive.api_host}"
    routes = {
        f"{host}/v3/reference/tickers?market=stocks&type={kind}&active=true&limit=1000":
            json.dumps({"results": [{"ticker": f"{kind}1"}, {"ticker": "NA"}]}).encode()
        for kind in CFG.massive.stock_types}
    assert massive.Reference(CFG, Wire(routes)).stock_tickers() == {"CS1", "ADRC1", "NA"}


def am_messages(frame) -> list[dict]:
    return [{"ev": "AM", "sym": r.ticker, "v": int(r.volume), "o": r.open, "c": r.close,
             "h": r.high, "l": r.low, "s": int(r.window_start) // 10**6,
             "e": int(r.window_start) // 10**6 + 60000}
            for r in frame.itertuples()]


class FakeSocket:
    def __init__(self, batches, drop=False):
        self.incoming = [json.dumps(b) for b in batches]
        self.sent = []
        self.drop = drop
        self.stop = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def send(self, text):
        self.sent.append(json.loads(text))

    def recv(self, timeout):
        if self.incoming:
            return self.incoming.pop(0)
        if self.drop:
            raise ConnectionError("closed")
        if self.stop is not None:
            self.stop.set()  # everything was read: the test ends the stream
        raise TimeoutError


def run_stream(batches, *, drop=False, sockets=None):
    stop = threading.Event()
    bars = massive.DayBars(model_bars.trading_days(1)[0])
    made = []

    def connect(url, max_size):
        assert url == f"wss://{CFG.massive.stream_host}{CFG.massive.stream_path}"
        assert max_size == CFG.massive.stream_max_message_bytes
        sock = (sockets or [None]).pop(0) or FakeSocket(batches, drop)
        made.append(sock)
        sock.stop = stop
        return sock

    stream = massive.MinuteStream(CFG, bars, connect)
    return stream, stop, bars, made


def test_the_stream_authenticates_subscribes_and_keeps_flat_file_rows(creds):
    days = model_bars.market(7)
    today, frame = days[-1]
    msgs = am_messages(frame)
    batches = [[{"ev": "status", "status": "connected"}],
               [{"ev": "status", "status": "auth_success"}],
               msgs[:5000], msgs[5000:]]
    stream, stop, bars, made = run_stream(batches)
    stream.session(stop)
    assert made[0].sent == [{"action": "auth", "params": KEY},
                            {"action": "subscribe", "params": "AM.*"}]
    live = bars.frame()
    assert len(live) == len(frame) and bars.gaps == []
    assert list(live.columns) == list(features.BAR_COLUMNS)  # no invented transactions
    # the shared feature code gets the same minute from the stream as from the day file
    minute = 450
    state = features.history(days[:-1], {}, today, None)
    a = features.minute_rows(model_bars.cut(live, today, minute), today, minute, state, None)
    b = features.minute_rows(model_bars.cut(frame, today, minute), today, minute, state, None)
    a, b = a.sort_values("sym").reset_index(drop=True), b.sort_values("sym").reset_index(drop=True)
    assert list(a.sym) == list(b.sym) and len(a) > 0
    for c in a.columns[1:]:
        assert np.array_equal(a[c].to_numpy(), b[c].to_numpy(), equal_nan=True), c


def test_a_refused_login_stops_the_stream(creds):
    stream, stop, _, _ = run_stream([[{"ev": "status", "status": "auth_failed"}]])
    with pytest.raises(ModelRefused) as exc:
        stream.session(stop)
    assert exc.value.cause == "stream_refused"


def test_bars_before_login_are_refused(creds):
    msg = am_messages(model_bars.market(1)[0][1].head(1))
    stream, stop, _, _ = run_stream([msg])
    with pytest.raises(ModelRefused) as exc:
        stream.session(stop)
    assert exc.value.cause == "stream_message_unreadable"


def test_a_dropped_connection_is_a_recorded_gap(creds, monkeypatch):
    monkeypatch.setattr(threading.Event, "wait", lambda self, t=None: False)
    first = FakeSocket([[{"ev": "status", "status": "auth_success"}]], drop=True)
    second = FakeSocket([])
    stream, stop, bars, made = run_stream([], sockets=[first, second])
    stream.run(stop)
    assert len(made) == 2 and len(bars.gaps) == 1
    assert bars.gaps[0][2] == "ConnectionError"  # what ended it is kept


def test_a_bar_of_another_day_is_counted_not_kept():
    bars = massive.DayBars("2026-11-18")
    msg = am_messages(model_bars.market(1, seed=1)[0][1].head(1))[0]
    msg["s"] += 86400000
    bars.add(msg, 0.0)
    assert bars.other_day == 1 and bars.frame().empty


def test_a_single_event_sent_bare_is_read_like_a_batch(creds):
    msg = am_messages(model_bars.market(1)[0][1].head(1))[0]
    stream, stop, bars, _ = run_stream([{"ev": "status", "status": "auth_success"}, msg])
    stream.session(stop)
    assert len(bars.frame()) == 1


def test_a_redirect_is_refused_and_the_key_never_follows_it():
    """A 3xx would make urllib resend the Authorization header to the Location's host."""
    import http.server

    seen = []

    class Other(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            seen.append(self.headers.get("Authorization"))
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"{}")

        def log_message(self, *args):
            pass

    other = http.server.HTTPServer(("127.0.0.1", 0), Other)

    class Redirect(Other):
        def do_GET(self):  # noqa: N802
            self.send_response(302)
            self.send_header("Location", f"http://localhost:{other.server_port}/x")
            self.end_headers()

    first = http.server.HTTPServer(("127.0.0.1", 0), Redirect)
    for server in (first, other):
        threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        with pytest.raises(ModelRefused) as exc:
            massive._get(massive._open, f"http://127.0.0.1:{first.server_port}/v3/x",
                         {"Authorization": "Bearer DUMMY-NOT-A-KEY"}, 5)
        assert exc.value.cause == "http_status" and exc.value.detail.startswith("302")
        assert seen == []
    finally:
        for server in (first, other):
            server.shutdown()


@pytest.mark.parametrize("value", ["", ".", "./", "/tmp/x", "../x", "a/../../x"])
def test_a_history_folder_outside_the_data_folder_is_refused(tmp_path, value):
    import dataclasses

    cfg = cfg_at(tmp_path)
    cfg = dataclasses.replace(cfg, massive=dataclasses.replace(cfg.massive, history_dir=value))
    with pytest.raises(ModelRefused) as exc:
        massive.History(cfg)
    assert exc.value.cause == "history_dir_outside"
