from __future__ import annotations

import csv
import json

from research_helpers import HERE, bar_json, flat, series, write_jsonl

from aperture.research.__main__ import main

ROOT = HERE.parent.parent
BARS = series("10:00", [flat("10.00"), ("10.00", "10.20", "10.00", "10.03"),
                        ("10.03", "10.30", "10.03", "10.30"), flat("10.30"), flat("10.40"),
                        flat("10.40")])
CLOCK = ["--replay-settings", str(HERE / "replay_settings.toml"),
         "--config", str(ROOT / "config" / "bot.toml")]


def _values(tmp_path, version, edge):
    path = tmp_path / f"v{version}.toml"
    path.write_text(f'[strategy]\nversion = "{version}"\nedge = "{edge}"\n')
    return str(path)


def _rows(path):
    with open(path, newline="") as handle:
        return list(csv.DictReader(handle))


def test_decide_outcomes_whatif_diff_end_to_end(tmp_path, capsys):
    bars = str(write_jsonl(tmp_path / "bars.jsonl", [bar_json(b) for b in BARS]))
    for version, edge in (("1", "0"), ("2", "0.05")):
        code = main(["decide", "--bars", bars, *CLOCK, "--strategy", "research_helpers",
                     "--values", _values(tmp_path, version, edge),
                     "--out", str(tmp_path / f"v{version}")])
        assert code == 0
    v1 = str(tmp_path / "v1" / "decisions.jsonl")
    v2 = str(tmp_path / "v2" / "decisions.jsonl")

    assert main(["outcomes", "--bars", bars, "--decisions", v1,
                 "--settings", str(HERE / "research_settings.toml"),
                 "--config", str(ROOT / "config" / "bot.toml"),
                 "--out", str(tmp_path / "o")]) == 0
    outcome_rows = _rows(tmp_path / "o" / "outcomes.csv")
    assert {r["label"] for r in outcome_rows} == {"observed"}
    assert [r["horizon_minutes"] for r in outcome_rows] == ["1", "2", "5"]
    assert outcome_rows[0]["move_percent"] == "2.6919"  # (10.30 - 10.03) / 10.03

    assert main(["whatif", "--bars", bars, "--decisions", v1, *CLOCK,
                 "--out", str(tmp_path / "w")]) == 0
    (whatif_row,) = _rows(tmp_path / "w" / "whatif.csv")
    assert (whatif_row["label"], whatif_row["outcome"], whatif_row["quantity"]) == (
        "simulated", "filled", "10")
    summary = json.loads((tmp_path / "w" / "whatif_summary.json").read_text())
    assert summary["label"] == "simulated" and summary["trades"] == 1

    assert main(["diff", "--baseline", v1, "--candidate", v2,
                 "--settings", str(HERE / "research_settings.toml"),
                 "--out", str(tmp_path / "d")]) == 0
    diff_summary = json.loads((tmp_path / "d" / "diff_summary.json").read_text())
    assert diff_summary["by_kind"]["verdict_changed"] == 2
    assert "verdict_changed" in capsys.readouterr().out


def test_bad_input_is_refused_with_exit_code_2(tmp_path, capsys):
    decisions = write_jsonl(tmp_path / "d.jsonl", ['{"time": 1}'])
    bars = write_jsonl(tmp_path / "bars.jsonl", [bar_json(b) for b in BARS])
    code = main(["outcomes", "--bars", str(bars), "--decisions", str(decisions),
                 "--settings", str(ROOT / "config" / "research.toml"),
                 "--out", str(tmp_path / "o")])
    assert code == 2
    assert "research refused" in capsys.readouterr().err
    code = main(["whatif", "--bars", str(bars), "--decisions", str(decisions), *CLOCK,
                 "--quantity", "abc", "--out", str(tmp_path / "w")])
    assert code == 2
