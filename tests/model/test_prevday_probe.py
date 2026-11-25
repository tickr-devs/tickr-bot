"""Probe prev-day (tickr/model/prevday_probe.py, `tickr probe-prev-day`): fake HTTP, fake clock."""

from __future__ import annotations

import datetime
import io
import json
import shutil
import urllib.error
from pathlib import Path

import pytest

from tickr import cli, config
from tickr.calendar import NEW_YORK, SessionCalendar
from tickr.model import prevday_probe
from tickr.model.errors import ModelRefused

ROOT = Path(__file__).resolve().parents[2]
CFG = config.load(ROOT / "config" / "bot.toml")
CAL = SessionCalendar.load(ROOT / "config" / "calendar")
KEY, KEY_ID, SECRET = "api-canary-1", "id-canary-2", "secret-canary-3"


def ny(*parts: int) -> datetime.datetime:
    return datetime.datetime(*parts, tzinfo=NEW_YORK)


@pytest.fixture
def creds(monkeypatch):
    monkeypatch.setenv(CFG.massive.api_key_variable, KEY)
    monkeypatch.setenv(CFG.massive.files_key_id_variable, KEY_ID)
    monkeypatch.setenv(CFG.massive.files_secret_variable, SECRET)


class Clock:
    def __init__(self, start: datetime.datetime) -> None:
        self.t = start

    def now(self) -> datetime.datetime:
        return self.t

    def sleep(self, seconds: float) -> None:
        assert seconds > 0
        self.t += datetime.timedelta(seconds=seconds)


class Head(io.BytesIO):
    def __init__(self, status: int = 200, headers: dict | None = None) -> None:
        super().__init__(b"")
        self.status, self.headers = status, headers or {}

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class Night:
    """The overnight server of 2026-11-27 (an early close): minute_aggs appears at 01:00 and
    is republished at 03:30, trades never, quotes answer 403, a split shows up at 02:00."""

    def __init__(self, clock: Clock) -> None:
        self.clock = clock
        self.requests: list = []

    def __call__(self, request, timeout):
        self.requests.append(request)
        t, path = self.clock.now(), request.selector
        if request.get_method() == "HEAD":
            if "minute_aggs_v1/2026/11/2026-11-27.csv.gz" in path and t >= ny(2026, 11, 28, 1):
                etag = '"b"' if t >= ny(2026, 11, 28, 3, 30) else '"a"'
                return Head(headers={"Content-Length": "123", "ETag": etag,
                                     "Last-Modified": "Sat, 28 Nov 2026 06:00:00 GMT"})
            code = 403 if "quotes_v1" in path else 404
            raise urllib.error.HTTPError(request.full_url, code, "x", {}, None)
        assert request.get_method() == "GET" and path.startswith("/v3/reference/splits")
        rows = ([{"execution_date": "2026-11-27", "ticker": "ABC", "split_from": 1,
                  "split_to": 4}] if t >= ny(2026, 11, 28, 2) else [])
        return io.BytesIO(json.dumps({"results": rows}).encode())


def rows_of(path: Path) -> list[dict]:
    return [json.loads(x) for x in path.read_text("utf-8").splitlines()]


@pytest.mark.parametrize(("opens", "start", "previous", "half"), [
    (ny(2026, 11, 24, 23), "2026-11-25", "2026-11-24", False),
    (ny(2026, 11, 25, 23), "2026-11-27", "2026-11-25", False),  # Thanksgiving is closed
    (ny(2026, 11, 27, 23), "2026-11-30", "2026-11-27", True),  # after the early close
    (ny(2026, 12, 27, 23), "2026-12-28", "2026-12-24", True),  # Christmas, a Sunday window
    (ny(2026, 12, 31, 23), "2027-01-04", "2026-12-31", False),  # into next year's file
])
def test_the_night_comes_from_the_bot_calendar(opens, start, previous, half):
    night = prevday_probe.night_of(CFG, CAL, opens)
    assert (night.start_day.isoformat(), night.previous.isoformat(), night.previous_half_day) \
        == (start, previous, half)
    assert night.deadline == CAL.day(night.start_day).premarket_open == ny(
        *map(int, start.split("-")), 4)


def test_a_day_the_calendar_does_not_cover_is_refused():
    with pytest.raises(ModelRefused) as exc:
        prevday_probe.night_of(CFG, CAL, ny(2027, 12, 31, 23))
    assert exc.value.cause == "calendar"


def test_the_window_is_the_one_running_or_the_next():
    assert prevday_probe.window(CFG, ny(2026, 11, 28, 1)) == (ny(2026, 11, 27, 23),
                                                              ny(2026, 11, 28, 6))
    assert prevday_probe.window(CFG, ny(2026, 11, 27, 22)) == (ny(2026, 11, 27, 23),
                                                               ny(2026, 11, 28, 6))
    assert prevday_probe.window(CFG, ny(2026, 11, 28, 7)) == (ny(2026, 11, 28, 23),
                                                              ny(2026, 11, 29, 6))


def test_a_night_records_first_present_republish_and_unknown(tmp_path, creds, monkeypatch):
    cfg = config.load(ROOT / "config" / "bot.toml", root=tmp_path)
    clock = Clock(ny(2026, 11, 27, 22, 50))
    server = Night(clock)
    monkeypatch.setattr(prevday_probe, "OPENER", server)
    out: list[str] = []
    assert prevday_probe.run(cfg, CAL, now=clock.now, sleep=clock.sleep, out=out.append) == 0
    assert clock.t == ny(2026, 11, 28, 6)
    path = prevday_probe.night_file(cfg, datetime.date(2026, 11, 30))
    rows = rows_of(path)
    assert {r["previous"] for r in rows} == {"2026-11-27"} and all(r["previous_half_day"]
                                                                   for r in rows)
    assert min(r["at"] for r in rows) == "2026-11-28T04:00:00+00:00"  # the window's 23:00 ET
    events = [(r["input"], r["event"], r["at"]) for r in rows if r["event"] != "check"]
    assert events == [
        ("minute_aggs", "first_present", "2026-11-28T06:00:00+00:00"),
        ("splits", "first_present", "2026-11-28T07:00:00+00:00"),
        ("minute_aggs", "changed", "2026-11-28T08:30:00+00:00")]
    first = next(r for r in rows if r["event"] == "first_present")
    assert (first["size"], first["etag"], first["status"]) == ("123", '"a"', 200)
    # 2026-11-30 04:00 ET is 2 days and 3 hours after 2026-11-28 01:00 ET
    assert first["since_deadline_s"] == -(2 * 24 + 3) * 3600
    quotes = [r for r in rows if r["input"] == "quotes_v1"]
    assert quotes and all(r["present"] is None and r["status"] == 403 for r in quotes)
    assert all(not r["present"] and r["status"] == 404 for r in rows
               if r["input"] == "trades_v1")
    splits = [r for r in rows if r["input"] == "splits"]
    assert splits[0]["by_day"] == {} and splits[-1]["by_day"] == {"2026-11-27": 1}
    # every file check is a signed HEAD of the previous session's key, never a GET
    heads = [q for q in server.requests if q.get_method() == "HEAD"]
    assert {q.selector for q in heads} == {
        f"/flatfiles/us_stocks_sip/{kind}/2026/11/2026-11-27.csv.gz"
        for kind in ("minute_aggs_v1", "trades_v1", "quotes_v1")}
    assert all("AWS4-HMAC-SHA256 Credential=" + KEY_ID in q.get_header("Authorization")
               for q in heads)
    text = path.read_text("utf-8")
    assert all(secret not in text and secret not in "".join(out)
               for secret in (KEY, KEY_ID, SECRET))

    summary_out: list[str] = []
    result = prevday_probe.summary(cfg, summary_out.append)
    assert result["minute_aggs"]["first_present_s"] == (first["since_deadline_s"],) * 3
    assert result["minute_aggs"]["republished"] == 1
    assert result["trades_v1"]["never_present"] == 1
    assert result["quotes_v1"]["present_at_deadline"] == 0
    assert any(line.startswith("  2026-11-30: first present -51:00:00") for line in summary_out)


def test_a_restart_continues_the_night_and_a_present_first_check_only_bounds(tmp_path, creds,
                                                                            monkeypatch):
    cfg = config.load(ROOT / "config" / "bot.toml", root=tmp_path)
    clock = Clock(ny(2026, 11, 28, 2, 30))  # started after the file appeared
    monkeypatch.setattr(prevday_probe, "OPENER", Night(clock))
    assert prevday_probe.run(cfg, CAL, now=clock.now, sleep=clock.sleep, out=lambda _: None) == 0
    clock.t = ny(2026, 11, 28, 5)  # a second start the same night
    assert prevday_probe.run(cfg, CAL, now=clock.now, sleep=clock.sleep, out=lambda _: None) == 0
    rows = rows_of(prevday_probe.night_file(cfg, datetime.date(2026, 11, 30)))
    assert [r["event"] for r in rows if r["input"] == "minute_aggs"].count("first_present") == 1
    out: list[str] = []
    result = prevday_probe.summary(cfg, out.append)
    assert result["minute_aggs"]["present_before_window"] == 1
    assert result["minute_aggs"]["first_present_s"] is None
    assert any(line.startswith("  2026-11-30: first present <=-") for line in out)


def test_a_failed_request_is_unknown_not_absent(tmp_path, creds):
    cfg = config.load(ROOT / "config" / "bot.toml", root=tmp_path)

    def down(request, timeout):
        raise TimeoutError

    row = prevday_probe.head_file(cfg, "k", down, lambda: ny(2026, 11, 28, 1))
    assert row == {"status": None, "present": None, "cause": "http_failed",
                   "detail": "TimeoutError"}


def test_cli_refuses_without_a_credential_and_names_it(tmp_path, monkeypatch, capsys):
    (tmp_path / "config").mkdir()
    bot = tmp_path / "config" / "bot.toml"
    bot.write_text((ROOT / "config" / "bot.toml").read_text("utf-8"), "utf-8")
    shutil.copytree(ROOT / "config" / "calendar", tmp_path / "config" / "calendar")
    monkeypatch.setenv(CFG.massive.api_key_variable, KEY)
    monkeypatch.setenv(CFG.massive.files_key_id_variable, KEY_ID)
    monkeypatch.delenv(CFG.massive.files_secret_variable, raising=False)
    sent: list = []
    monkeypatch.setattr(prevday_probe, "OPENER", lambda r, t: sent.append(r))
    assert cli.main(["--config", str(bot), "probe-prev-day"]) == 3
    err = capsys.readouterr().err
    assert CFG.massive.files_secret_variable in err and KEY not in err and sent == []
    assert cli.main(["--config", str(bot), "probe-prev-day", "--summary"]) == 0
    assert "no captured nights" in capsys.readouterr().out


def test_a_bad_window_is_a_config_error(tmp_path):
    text = (ROOT / "config" / "bot.toml").read_text("utf-8").replace(
        'prev_day_window_start = "23:00"', 'prev_day_window_start = "11pm"')
    bad = tmp_path / "bot.toml"
    bad.write_text(text, "utf-8")
    with pytest.raises(config.ConfigError, match="prev_day_window"):
        config.load(bad)
