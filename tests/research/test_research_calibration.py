"""R29: reliability table, Brier score and expected calibration error on synthetic answers."""

from __future__ import annotations

import csv
import json
from decimal import Decimal

import pytest
from research_helpers import HERE

from tickr.research.__main__ import main
from tickr.research.calibration import calibration

SETTINGS = str(HERE / "research_settings.toml")
CLOCK = ["--replay-settings", str(HERE / "replay_settings.toml"),
         "--config", str(HERE.parent.parent / "config" / "bot.toml")]


def _answers(path, header, rows, day="2026-06-30"):
    """Stored answers with a `day` column (the day before the sealed window) added last."""
    lines = [",".join([*header, "day"]), *(",".join([*r, day]) for r in rows)]
    path.write_text("\n".join(lines) + "\n")
    return path


def test_table_brier_and_ece(tmp_path):
    rows = [("0.05", "0"), ("0.05", "0"), ("0.75", "1"), ("0.75", "0"), ("0.75", "1"),
            ("0.75", "1"), ("1", "1")]
    path = _answers(tmp_path / "a.csv", ["p_layer1", "outcome"], rows)
    (report,) = calibration(path, 10)
    assert (report.answers, report.skipped_blank) == (7, 0)
    by = {b.index: b for b in report.buckets}
    assert by[0].count == 2 and by[0].observed_rate == 0
    assert by[0].gap == Decimal("-0.05")
    assert by[7].count == 4 and by[7].mean_predicted == Decimal("0.75")
    assert by[7].observed_rate == Decimal("0.75") and by[7].gap == 0
    assert by[9].count == 1 and by[9].gap == 0  # p = 1 lands in the last bucket
    assert by[3].count == 0 and by[3].gap is None
    assert report.ece == Decimal("0.05") * 2 / 7
    squared = 2 * Decimal("0.05") ** 2 + Decimal("0.25") ** 2 * 3 + Decimal("0.75") ** 2
    assert report.brier == squared / 7
    assert report.base_rate == Decimal(4) / 7


def test_blank_cells_skipped_and_columns_separate(tmp_path):
    path = _answers(tmp_path / "a.csv", ["time", "p_layer1", "p_layer2", "outcome"],
                    [("t1", "0.9", "", "1"), ("t2", "0.9", "0.1", "0")])
    one, two = calibration(path, 10)
    assert (one.column, one.answers, one.skipped_blank) == ("p_layer1", 2, 0)
    assert (two.column, two.answers, two.skipped_blank) == ("p_layer2", 1, 1)


@pytest.mark.parametrize(
    ("header", "row", "cause"),
    [
        (["p_a", "outcome"], ("1.5", "1"), "outside 0..1"),
        (["p_a", "outcome"], ("0.5", "2"), "not 0 or 1"),
        (["p_a", "outcome"], ("x", "1"), "not a number"),
        (["p_a", "outcome"], ("NaN", "1"), "not finite"),
        (["p_a", "label"], ("0.5", "1"), "no 'outcome' column"),
        (["a", "outcome"], ("0.5", "1"), "no probability column"),
    ],
)
def test_bad_input_refused(tmp_path, capsys, header, row, cause):
    path = _answers(tmp_path / "a.csv", header, [row])
    code = main(["calibration", "--answers", str(path), "--settings", SETTINGS,
                 "--out", str(tmp_path / "o"), *CLOCK])
    assert code == 2
    err = capsys.readouterr().err
    assert "research refused" in err
    assert cause in err


def test_cli_writes_csv_and_json(tmp_path):
    path = _answers(tmp_path / "a.csv", ["p_layer1", "outcome"],
                    [("0.2", "0"), ("0.2", "1"), ("0.8", "1")])
    out = tmp_path / "o"
    assert main(["calibration", "--answers", str(path), "--settings", SETTINGS,
                 "--out", str(out), *CLOCK]) == 0
    with open(out / "calibration.csv", newline="") as handle:
        rows = list(csv.DictReader(handle))
    assert len(rows) == 10
    assert rows[2]["count"] == "2" and rows[2]["observed_rate"] == "0.5000"
    assert rows[2]["gap"] == "0.3000"
    summary = json.loads((out / "calibration_summary.json").read_text())
    assert summary["columns"]["p_layer1"]["answers"] == 3


def test_answers_need_a_day_column(tmp_path, capsys):
    path = tmp_path / "a.csv"
    path.write_text("p_a,outcome\n0.5,1\n")
    assert main(["calibration", "--answers", str(path), "--settings", SETTINGS,
                 "--out", str(tmp_path / "o"), *CLOCK]) == 2
    assert "no 'day' column" in capsys.readouterr().err


def test_a_bad_day_is_refused(tmp_path, capsys):
    path = _answers(tmp_path / "a.csv", ["p_a", "outcome"], [("0.5", "1")], day="30/06/2026")
    assert main(["calibration", "--answers", str(path), "--settings", SETTINGS,
                 "--out", str(tmp_path / "o"), *CLOCK]) == 2
    assert "is not a YYYY-MM-DD date" in capsys.readouterr().err
