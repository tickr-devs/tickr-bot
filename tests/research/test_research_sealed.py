"""The sealed exam window (config/research.toml [sealed], backlog R20): every research command
that takes dates or reads dated inputs refuses one that touches it, as a whole, with the cause
`sealed_window`; an input ending the day before the window opens passes."""

from __future__ import annotations

import csv
import json
import tomllib
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal as D

import pytest
from research_helpers import HERE, NY, bar_json, flat, record, series, write_jsonl

from tickr.data.base import StreamNotice
from tickr.data.capture import CaptureWriter
from tickr.replay.errors import MeasuredSlippageError
from tickr.replay.measured import FORMAT as MEASURED_FORMAT
from tickr.replay.measured import OPERATIONS_FORMAT, load_measured_slippage
from tickr.research import sealed
from tickr.research.__main__ import COMMANDS, _parser, main
from tickr.research.errors import ResearchSettingsError
from tickr.research.gate import TRADE_COLUMNS
from tickr.research.records import write_records
from tickr.research.sealed import (
    SealedWindowRefused,
    check_command,
    refuse_sealed,
    refuse_sealed_own_records,
    window_from_mapping,
)
from tickr.store import Store
from tickr.types import Quote

ROOT = HERE.parent.parent
CONFIG = ROOT / "config" / "research.toml"
SETTINGS = ["--settings", str(HERE / "research_settings.toml")]
PLACES = ["--replay-settings", str(HERE / "replay_settings.toml"),
          "--config", str(ROOT / "config" / "bot.toml")]
OPEN = date(2026, 6, 30)  # the day before the window opens
SEALED = (date(2026, 7, 1), date(2026, 8, 14), date(2026, 11, 20), date(2027, 1, 4))
BARS = [flat("10.00"), ("10.00", "10.20", "10.00", "10.03"), ("10.03", "10.30", "10.03", "10.30"),
        flat("10.30"), flat("10.40"), flat("10.40")]


def test_the_window_has_one_home_with_the_documented_dates():
    window, zone = sealed.load()
    assert window.ranges == ((date(2026, 7, 1), date(2026, 8, 14)),)
    assert window.every_day_from == date(2026, 11, 20) and zone == NY
    assert sealed.SEALED_FILE == CONFIG


@pytest.mark.parametrize(
    ("first", "last", "hit"),
    [
        (date(2026, 6, 1), OPEN, None),
        (date(2026, 8, 15), date(2026, 11, 19), None),
        (date(2026, 6, 1), date(2026, 12, 31), date(2026, 7, 1)),
        (date(2026, 8, 14), date(2026, 8, 20), date(2026, 8, 14)),
        (date(2026, 9, 1), date(2026, 11, 20), date(2026, 11, 20)),
        (date(2027, 3, 1), date(2027, 3, 2), date(2027, 3, 1)),
    ],
)
def test_a_range_is_refused_on_its_first_sealed_day(first, last, hit):
    window, _ = sealed.load()
    assert window.first_sealed(first, last) == hit
    if hit is None:
        refuse_sealed("range", first=first, last=last)
        return
    with pytest.raises(SealedWindowRefused) as info:
        refuse_sealed("range", first=first, last=last)
    assert (info.value.cause, info.value.day) == ("sealed_window", hit)


def test_a_time_counts_on_its_exchange_date():
    late = datetime(2026, 7, 1, 2, 0, tzinfo=UTC)  # 22:00 New York on 2026-06-30
    refuse_sealed("times", days=[late, OPEN])
    with pytest.raises(SealedWindowRefused):
        refuse_sealed("times", days=[late + timedelta(hours=4)])
    with pytest.raises(SealedWindowRefused, match="2026-08-14"):
        refuse_sealed("days", days=[OPEN, date(2026, 8, 14), OPEN])  # one is enough


def _table(**changes):
    data = tomllib.loads(CONFIG.read_text("utf-8"))
    data["sealed"].update(changes)
    return data


@pytest.mark.parametrize(
    "changes",
    [{"ranges": [["2026-07-01", "2026-08-14"]]}, {"ranges": [[date(2026, 8, 14)]]},
     {"ranges": [[date(2026, 8, 14), date(2026, 7, 1)]]}, {"every_day_from": "2026-11-20"},
     {"surprise": 1}],
)
def test_a_broken_window_is_refused(changes):
    with pytest.raises(ResearchSettingsError):
        window_from_mapping(_table(**changes))
    data = tomllib.loads(CONFIG.read_text("utf-8"))
    del data["sealed"]
    with pytest.raises(ResearchSettingsError):
        window_from_mapping(data)


# One builder per command: the argv of a run whose every input lies on `days`.


def _bars(tmp_path, days):
    rows = [bar_json(b) for day in days for b in series("10:00", BARS, day=day)]
    return str(write_jsonl(tmp_path / "bars.jsonl", rows))


def _decisions(tmp_path, days, name="decisions.jsonl"):
    path = tmp_path / name
    write_records(path, [record("10:02", day=day, low="10.20", high="10.30", stop="10.00",
                                quantity="10") for day in days])
    return str(path)


def _capture(tmp_path, days):
    w = CaptureWriter(tmp_path / "cap", "webull")
    paths = {"quotes": [], "notices": [], "bars": []}
    for day in days:
        t0 = datetime.combine(day, time(10), NY).astimezone(UTC)
        w.bars([b for b in series("09:58", BARS[:2], day=day)], "webull", t0)
        w.on_quote(Quote("ABC", D("10.00"), D("10.02"), D(1), D(1), t0, t0))
        w.on_notice(StreamNotice(t0, 0, 40, 3))
        for kind in paths:
            paths[kind].append(str(tmp_path / "cap" / day.isoformat() / f"{kind}.jsonl"))
    w.close()
    return paths


def _database(tmp_path, days):
    path = tmp_path / "tickr.sqlite3"
    first = datetime.combine(days[0], time(11), NY)
    store = Store.open(path, busy_timeout_seconds=1, now=lambda: first)
    run = store.start_run(environment="sandbox", config_digest="d", strategy="s",
                          strategy_version="1", code_version="c")
    for n, day in enumerate(days):
        closed = datetime.combine(day, time(11), NY).astimezone(UTC).isoformat()
        with store.transaction():
            store.execute(
                "INSERT INTO trades(trade_id, run_id, symbol, state, entry_quantity, "
                "entry_limit_price, stop_price, planned_loss_usd, opened_at, closed_at, "
                "close_cause) VALUES (?, ?, 'ABC', 'closed', '10', '10.00', '9.80', '2.00', ?, "
                "?, 'stopped-out')", (f"t{n}", run, closed, closed))
            for leg, side, limit, price in (("entry", "BUY", "10.00", "10.01"),
                                            ("stop", "SELL", None, "9.70")):
                store.execute(
                    "INSERT INTO orders(client_order_id, trade_id, leg, symbol, side, "
                    "order_type, quantity, limit_price, stop_price, send_state, "
                    "filled_quantity, average_fill_price, updated_at) VALUES (?, ?, ?, 'ABC', "
                    "?, 'LIMIT', '10', ?, '9.80', 'acknowledged', '10', ?, ?)",
                    (f"t{n}-{leg}", f"t{n}", leg, side, limit, price, closed))
    store.close()
    return str(path)


def _strategy_values(tmp_path):
    path = tmp_path / "v.toml"
    path.write_text('[strategy]\nversion = "1"\nedge = "0"\n')
    return str(path)


def _decide(tmp_path, days):
    return ["decide", "--bars", _bars(tmp_path, days), *PLACES, "--strategy",
            "research_helpers", "--values", _strategy_values(tmp_path)]


def _outcomes(tmp_path, days):
    return ["outcomes", "--bars", _bars(tmp_path, days), "--decisions",
            _decisions(tmp_path, days), *SETTINGS, *PLACES]


def _whatif(tmp_path, days):
    return ["whatif", "--bars", _bars(tmp_path, days), "--decisions",
            _decisions(tmp_path, days), *PLACES]


def _observers(tmp_path, days):
    return ["observers", "--bars", _bars(tmp_path, days), "--decisions",
            _decisions(tmp_path, days), *PLACES]


def _diff(tmp_path, days):
    # The candidate is always open: a sealed baseline alone is enough to refuse.
    return ["diff", "--baseline", _decisions(tmp_path, days, "a.jsonl"), "--candidate",
            _decisions(tmp_path, [OPEN], "b.jsonl"), *SETTINGS, *PLACES]


def _refusals(tmp_path, days):
    return ["refusals", "--decisions", _decisions(tmp_path, days), *SETTINGS, *PLACES]


def _latency(tmp_path, days):
    cap = _capture(tmp_path, days)
    return ["latency", "--quotes", *cap["quotes"], "--notices", *cap["notices"], *SETTINGS,
            *PLACES]


def _latency_notices(tmp_path, days):
    # Quotes of the open day only: the notices alone carry the sealed day.
    quotes = _capture(tmp_path / "open", [OPEN])["quotes"]
    return ["latency", "--quotes", *quotes, "--notices", *_capture(tmp_path, days)["notices"],
            *SETTINGS, *PLACES]


def _collar(tmp_path, days):
    cap = _capture(tmp_path, days)
    return ["collar", "--quotes", *cap["quotes"], "--bars", *cap["bars"], *PLACES]


def _collar_bars(tmp_path, days):
    quotes = _capture(tmp_path / "open", [OPEN])["quotes"]
    return ["collar", "--quotes", *quotes, "--bars", *_capture(tmp_path, days)["bars"], *PLACES]


def _slippage(tmp_path, days):
    return ["slippage", "--database", _database(tmp_path, days), *SETTINGS, *PLACES]


def _fill_quality(tmp_path, days):
    return ["fill-quality", "--database", _database(tmp_path, days), *SETTINGS, *PLACES]


def _fill_quality_quotes(tmp_path, days):
    return ["fill-quality", "--database", _database(tmp_path / "open", [OPEN]), "--quotes",
            *_capture(tmp_path, days)["quotes"], *SETTINGS, *PLACES]


def _gate_files(tmp_path, days):
    trades = tmp_path / "trades.csv"
    with open(trades, "w", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(TRADE_COLUMNS)
    covered = tmp_path / "covered.csv"
    covered.write_text("day\n" + "".join(f"{d.isoformat()}\n" for d in days))
    return str(trades), str(covered)


def _gate(tmp_path, days, start=None, end=None):
    trades, covered = _gate_files(tmp_path, days)
    return ["gate", "--trades", trades, "--covered-days", covered,
            "--start", (start or min(days)).isoformat(), "--end", (end or max(days)).isoformat(),
            "--all-sessions-usd", "4000", "--model-version", "v", "--results", "simulated",
            *SETTINGS, *PLACES]


def _gate_covered(tmp_path, days):
    # The range is open; one covered day outside it lies in the window.
    return _gate(tmp_path, days, start=date(2026, 6, 1), end=OPEN)


# Runs that touch a sealed input every command refuses: market data, decision records of the
# strategies, model trades. The execution reports are here only with a market-data file.
REFUSED = {"decide": _decide, "outcomes": _outcomes, "whatif": _whatif,
           "observers": _observers, "diff": _diff, "gate": _gate,
           "collar": _collar, "collar --bars": _collar_bars,
           "latency": _latency, "fill-quality --quotes": _fill_quality_quotes}
# Execution reports reading only the bot's own order, fill and timing records of sealed days.
OWN_RECORDS = {"refusals": _refusals, "latency --notices": _latency_notices,
               "slippage": _slippage, "fill-quality": _fill_quality}
ALL = {**REFUSED, **OWN_RECORDS}


def _builder(build):
    def make(where, days):
        where.mkdir(parents=True)
        (where / "open").mkdir()
        return build(where, days)
    return make


@pytest.mark.parametrize("name", sorted(ALL))
def test_every_command_passes_the_day_before_the_window(name, tmp_path):
    build = _builder(ALL[name])
    assert main([*build(tmp_path / "open", [OPEN]), "--out", str(tmp_path / "o")]) == 0


@pytest.mark.parametrize("name", sorted(REFUSED))
def test_a_sealed_input_is_refused(name, tmp_path, capsys):
    build = _builder(REFUSED[name])
    for n, day in enumerate(SEALED):
        out = tmp_path / f"s{n}"
        # Open days around it change nothing: nothing is skipped, the whole run is refused.
        assert main([*build(out / "in", [OPEN, day]), "--out", str(out / "o")]) == 2
        assert "research refused: sealed_window" in capsys.readouterr().err
        assert not (out / "o").exists()


@pytest.mark.parametrize("name", sorted(OWN_RECORDS))
def test_an_execution_report_reads_its_own_records_of_sealed_days(name, tmp_path):
    build = _builder(OWN_RECORDS[name])
    for n, day in enumerate(SEALED):
        out = tmp_path / f"s{n}"
        assert main([*build(out / "in", [OPEN, day]), "--out", str(out / "o")]) == 0


def test_an_execution_report_touching_sealed_market_data_is_refused(tmp_path, capsys):
    # fill-quality on sealed days of its own database passes; a sealed captured quote does not.
    (tmp_path / "db").mkdir()
    (tmp_path / "q").mkdir()
    database = _database(tmp_path / "db", [date(2026, 7, 1)])
    quotes = _capture(tmp_path / "q", [date(2026, 7, 1)])["quotes"]
    base = ["fill-quality", "--database", database, *SETTINGS, *PLACES]
    assert main([*base, "--out", str(tmp_path / "a")]) == 0
    assert main([*base, "--quotes", *quotes, "--out", str(tmp_path / "b")]) == 2
    assert "sealed_window: quotes of records" in capsys.readouterr().err
    assert not (tmp_path / "b").exists()


def _csv(path):
    with open(path, newline="") as handle:
        return {r["day"]: r for r in csv.DictReader(handle)}


def test_slippage_on_a_sealed_day_keeps_slippage_and_withholds_prices_and_pnl(tmp_path):
    (tmp_path / "in").mkdir()
    argv = _slippage(tmp_path / "in", [OPEN, date(2026, 11, 20)])
    assert main([*argv, "--out", str(tmp_path / "o")]) == 0
    trades = _csv(tmp_path / "o" / "slippage_trades.csv")
    sealed_row, open_row = trades["2026-11-20"], trades[OPEN.isoformat()]
    for column in ("stop_price", "stop_fill_price", "entry_price", "realized_loss_usd"):
        assert sealed_row[column] == "" and open_row[column] != ""
    assert sealed_row["slippage_usd"] == open_row["slippage_usd"] != ""
    assert sealed_row["slippage_percent"] != ""
    days = _csv(tmp_path / "o" / "slippage_days.csv")
    assert days["2026-11-20"]["realized_loss_usd"] == ""
    assert days["2026-11-20"]["stop_slippage_percent"] != ""


def test_the_replay_feed_excludes_sealed_days_and_refuses_the_operations_file(tmp_path):
    (tmp_path / "in").mkdir()
    argv = _slippage(tmp_path / "in", [OPEN, date(2026, 11, 20)])
    assert main([*argv, "--out", str(tmp_path / "o")]) == 0
    summary = json.loads((tmp_path / "o" / "slippage_summary.json").read_text())
    assert summary["format"] == MEASURED_FORMAT
    assert summary["days"] == [OPEN.isoformat()] and summary["measured_trades"] == 1
    assert summary["sealed_days_excluded"] == ["2026-11-20"]
    assert summary["realized_loss_usd"] is not None
    operations = json.loads((tmp_path / "o" / "slippage_operations.json").read_text())
    assert operations["format"] == OPERATIONS_FORMAT and operations["measured_trades"] == 2
    assert operations["sealed_days"] == ["2026-11-20"] and operations["realized_loss_usd"] is None
    assert load_measured_slippage(tmp_path / "o" / "slippage_summary.json", 1) >= 0
    with pytest.raises(MeasuredSlippageError, match="never a replay input"):
        load_measured_slippage(tmp_path / "o" / "slippage_operations.json", 1)


def test_fill_quality_on_a_sealed_day_withholds_absolute_prices(tmp_path):
    (tmp_path / "in").mkdir()
    argv = _fill_quality(tmp_path / "in", [OPEN, date(2026, 7, 1)])
    assert main([*argv, "--out", str(tmp_path / "o")]) == 0
    trades = _csv(tmp_path / "o" / "records" / "fill_quality_trades.csv")
    for column in ("entry_limit", "entry_fill"):  # the stop exits have no market exit price
        assert trades["2026-07-01"][column] == "" and trades[OPEN.isoformat()][column] != ""
    assert trades["2026-07-01"]["entry_vs_limit_usd"] != ""


def test_refusals_on_a_sealed_day_count_platform_causes_only(tmp_path):
    (tmp_path / "in").mkdir()
    argv = _refusals(tmp_path / "in", [OPEN, date(2026, 7, 1)])
    assert main([*argv, "--out", str(tmp_path / "o")]) == 0
    with open(tmp_path / "o" / "refusals.csv", newline="") as handle:
        rows = list(csv.DictReader(handle))
    sealed_rows = [r for r in rows if r["day"] == "2026-07-01"]
    assert [(r["strategy"], r["version"], r["role"], r["setup"], r["kind"], r["name"], r["count"])
            for r in sealed_rows] == [("", "", "", "", "platform_refusal", "daily loss ceiling",
                                       "1")]
    assert {r["kind"] for r in rows if r["day"] == OPEN.isoformat()} == {
        "enter", "platform_refusal"}
    assert all(r["strategy"] == "s" for r in rows if r["day"] == OPEN.isoformat())
    summary = json.loads((tmp_path / "o" / "refusals_summary.json").read_text())
    assert summary["platform_only_days"] == ["2026-07-01"]


def test_every_command_is_in_exactly_one_list():
    window, _ = sealed.load()
    parser_commands = set(_parser()._subparsers._group_actions[0].choices)  # noqa: SLF001
    assert parser_commands == COMMANDS
    assert window.sealed_commands | window.execution_commands == COMMANDS
    assert not window.sealed_commands & window.execution_commands
    assert window.execution_commands == {"fill-quality", "latency", "refusals", "slippage"}
    check_command("gate", COMMANDS)
    with pytest.raises(ResearchSettingsError, match="not in"):
        check_command("surprise", COMMANDS | {"surprise"})
    with pytest.raises(ResearchSettingsError, match="do not exist"):
        check_command("gate", COMMANDS - {"collar"})
    with pytest.raises(ResearchSettingsError, match="both lists"):
        window_from_mapping(_table(sealed_commands=["gate", "latency"]))


def test_own_records_of_a_sealed_command_are_refused():
    refuse_sealed_own_records("latency", "--notices", days=[date(2026, 7, 1)])
    with pytest.raises(SealedWindowRefused):
        refuse_sealed_own_records("outcomes", "--decisions", days=[date(2026, 7, 1)])


def test_gate_refuses_an_overlapping_range_before_opening_its_files(tmp_path, capsys):
    argv = ["gate", "--trades", str(tmp_path / "none.csv"), "--covered-days",
            str(tmp_path / "none.csv"), "--start", "2026-06-01", "--end", "2026-07-01",  # absent
            "--all-sessions-usd", "4000", "--model-version", "v", "--results", "simulated",
            *SETTINGS, *PLACES, "--out", str(tmp_path / "o")]
    assert main(argv) == 2
    err = capsys.readouterr().err
    assert "sealed_window: --start..--end" in err and "2026-07-01" in err


def test_gate_refuses_a_covered_day_in_the_window(tmp_path, capsys):
    argv = [*_gate_covered(tmp_path, [OPEN, date(2026, 7, 1)]), "--out", str(tmp_path / "o")]
    assert main(argv) == 2
    assert "sealed_window: --covered-days" in capsys.readouterr().err


def test_calibration_takes_no_dates(tmp_path):
    # Stored answers carry no day (docs/research.md): the command is not guarded; see the doc.
    answers = tmp_path / "a.csv"
    answers.write_text("outcome,p_layer1\n1,0.9\n0,0.1\n")
    assert main(["calibration", "--answers", str(answers), *SETTINGS, *PLACES,
                 "--out", str(tmp_path / "o")]) == 0
