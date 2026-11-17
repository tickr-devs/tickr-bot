"""Probe Q1 (engine/quote_sources_probe.py, `tickr probe-quote-sources`): read only."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from tickr import cli, config
from tickr.data.base import SourceRead
from tickr.engine.quote_sources_probe import SOURCES_FILE, describe, run, time_fields

ROOT = Path(__file__).resolve().parents[2]
CFG = config.load(ROOT / "config" / "bot.toml")
NOW = datetime(2026, 11, 17, 0, 6, tzinfo=UTC)  # 19:06 ET, post-market
MS = int((NOW - timedelta(seconds=12)).timestamp() * 1000)


def snapshot(extended: bool) -> list[dict]:
    row = {"symbol": "SOFI", "quote_time": MS - 600_000, "last_trade_time": MS - 600_000}
    if extended:
        row["extend_hour_last_trade_time"] = MS
    return [row]


class Sources:
    def __init__(self) -> None:
        self.asked: list[str] = []

    def quote_sources(self, symbol: str) -> list[SourceRead]:
        self.asked.append(symbol)
        return [SourceRead("snapshot", 200, None, snapshot(False), NOW),
                SourceRead("snapshot-extended", 200, None, snapshot(True), NOW),
                SourceRead("depth", 417, "NO_PERMISSION", {"error_code": "NO_PERMISSION"}, NOW),
                SourceRead("ticks", None, "BudgetExhausted", None, NOW)]


def test_time_fields_reads_epoch_ms_and_iso_times_anywhere() -> None:
    body = {"result": [{"time": "2026-11-17T00:05:00.000+0000", "price": "1"}],
            "quote_time": MS, "note_time": "n/a", "flag_time": True}
    assert [w for w, _ in time_fields(body)] == ["result[0].time", "quote_time"]


def test_describe_prints_every_age() -> None:
    read = SourceRead("snapshot-extended", 200, None, snapshot(True), NOW)
    lines = describe(read)
    assert lines[0] == "  snapshot-extended: HTTP 200"
    assert lines[-1].startswith("    [0].extend_hour_last_trade_time") and "age 12.0 s" in lines[-1]


def test_run_records_every_answer_and_says_when_one_was_missing(tmp_path: Path) -> None:
    out: list[str] = []
    assert run(Sources(), ["SOFI", "NIO"], tmp_path, out.append) == 3
    rows = [json.loads(x) for x in (tmp_path / SOURCES_FILE).read_text("utf-8").splitlines()]
    assert [(r["symbol"], r["read"]) for r in rows][:4] == [
        ("SOFI", "snapshot"), ("SOFI", "snapshot-extended"), ("SOFI", "depth"),
        ("SOFI", "ticks")]
    assert len(rows) == 8 and rows[2]["error_code"] == "NO_PERMISSION"
    assert "  depth: HTTP 417 NO_PERMISSION" in out


def test_cli_reads_only_and_refuses_bad_symbols(tmp_path: Path, monkeypatch, capsys) -> None:
    (tmp_path / "config").mkdir()
    bot = tmp_path / "config" / "bot.toml"
    bot.write_text((ROOT / "config" / "bot.toml").read_text("utf-8"), "utf-8")
    sources = Sources()
    monkeypatch.setattr(cli, "PROBE_ADAPTERS", lambda cfg: (None, sources))
    monkeypatch.setattr(cli, "NOW", lambda: NOW)
    assert cli.main(["--config", str(bot), "probe-quote-sources", "sofi"]) == 2
    assert sources.asked == []
    assert cli.main(["--config", str(bot), "probe-quote-sources", "SOFI"]) == 3
    assert sources.asked == ["SOFI"]
    assert "evidence in" in capsys.readouterr().out
