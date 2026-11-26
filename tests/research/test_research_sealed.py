"""The sealed exam window (config/research.toml [sealed], backlog R20): every research command
that takes dates or reads dated inputs refuses one that touches it, as a whole, with the cause
`sealed_window`; an input ending the day before the window opens passes."""

from __future__ import annotations

import csv
import tomllib
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal as D

import pytest
from research_helpers import HERE, NY, bar_json, flat, record, series, write_jsonl

from tickr.data.base import StreamNotice
from tickr.data.capture import CaptureWriter
from tickr.research import sealed
from tickr.research.__main__ import main
from tickr.research.errors import ResearchSettingsError
from tickr.research.gate import TRADE_COLUMNS
from tickr.research.records import write_records
from tickr.research.sealed import SealedWindowRefused, refuse_sealed, window_from_mapping
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
            for leg, side, price in (("entry", "BUY", "10.00"), ("stop", "SELL", "9.70")):
                store.execute(
                    "INSERT INTO orders(client_order_id, trade_id, leg, symbol, side, "
                    "order_type, quantity, stop_price, send_state, filled_quantity, "
                    "average_fill_price, updated_at) VALUES (?, ?, ?, 'ABC', ?, 'LIMIT', '10', "
                    "'9.80', 'acknowledged', '10', ?, ?)",
                    (f"t{n}-{leg}", f"t{n}", leg, side, price, closed))
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


COMMANDS = {"decide": _decide, "outcomes": _outcomes, "whatif": _whatif,
            "observers": _observers, "diff": _diff, "refusals": _refusals, "latency": _latency,
            "latency --notices": _latency_notices, "collar": _collar,
            "collar --bars": _collar_bars, "slippage": _slippage,
            "fill-quality": _fill_quality, "fill-quality --quotes": _fill_quality_quotes,
            "gate": _gate}


@pytest.mark.parametrize("name", sorted(COMMANDS))
def test_every_command_refuses_a_sealed_day_and_passes_the_day_before(name, tmp_path, capsys):
    def build(where, days):
        where.mkdir(parents=True)
        (where / "open").mkdir()
        return COMMANDS[name](where, days)

    assert main([*build(tmp_path / "open", [OPEN]), "--out", str(tmp_path / "o")]) == 0
    capsys.readouterr()
    for n, day in enumerate(SEALED):
        out = tmp_path / f"s{n}"
        # Open days around it change nothing: nothing is skipped, the whole run is refused.
        assert main([*build(out / "in", [OPEN, day]), "--out", str(out / "o")]) == 2
        assert "research refused: sealed_window" in capsys.readouterr().err
        assert not (out / "o").exists()


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
