"""Size by model confidence (backlog R30): the pure function, the config refusals, the what-if."""

from __future__ import annotations

import csv
import json
import re
import shutil
from decimal import Decimal as D
from pathlib import Path

import pytest

from tickr import config
from tickr.research.__main__ import main
from tickr.risk.confidence_sizing import SizingRefused, size_usd

ROOT = Path(__file__).resolve().parents[2]
BOT = ROOT / "config" / "bot.toml"
CAP = config.load(BOT).risk.position_notional_cap_usd


def test_linear_between_the_bounds():
    low, high = D(200), D(800)
    assert size_usd(D(0), low, high, CAP) == 200
    assert size_usd(D("0.5"), low, high, CAP) == 500
    assert size_usd(D(1), low, high, CAP) == 800


@pytest.mark.parametrize("probability,cause", [
    (None, "probability_missing"), (D("-0.01"), "probability_out_of_range"),
    (D("1.01"), "probability_out_of_range"), (D("NaN"), "probability_not_finite"),
])
def test_refuses_bad_probability(probability, cause):
    with pytest.raises(SizingRefused) as caught:
        size_usd(probability, D(100), D(200), CAP)
    assert caught.value.cause == cause


@pytest.mark.parametrize("low,high", [(D(0), D(100)), (D(300), D(200)), (D(100), CAP + 1)])
def test_refuses_bounds_outside_the_cap(low, high):
    with pytest.raises(SizingRefused) as caught:
        size_usd(D("0.5"), low, high, CAP)
    assert caught.value.cause == "bounds_outside_cap"


def test_shipped_config_is_off():
    sizing = config.load(BOT).confidence_sizing
    assert (sizing.by_confidence, sizing.min_usd, sizing.max_usd) == (False, 0, 0)


def _load_with(tmp_path, sizing: str, model_enabled: bool = False):
    (tmp_path / "config").mkdir()
    text = BOT.read_text("utf-8")
    for line in sizing.splitlines():
        key = line.split(" = ")[0]
        text, count = re.subn(rf"^{key} = .*$", line, text, flags=re.M)
        assert count == 1
    assert "enabled = false" in text
    if model_enabled:
        text = text.replace("enabled = false", "enabled = true", 1)
    target = tmp_path / "config" / "bot.toml"
    target.write_text(text, "utf-8")
    shutil.copytree(ROOT / "config" / "calendar", tmp_path / "config" / "calendar")
    return config.load(target, root=tmp_path)


def test_on_is_refused_without_the_probability(tmp_path):
    with pytest.raises(config.ConfigError, match="probability is not available"):
        _load_with(tmp_path, "by_confidence = true\nmin_usd = 100\nmax_usd = 500")


@pytest.mark.parametrize("bounds", ["min_usd = 100\nmax_usd = 5000", "min_usd = 600\nmax_usd = 500",
                                    "min_usd = 0\nmax_usd = 500"])
def test_bad_bounds_are_refused_when_on(tmp_path, bounds):
    # The model switch itself is refused today, so the bounds check is exercised directly.
    cfg = config.load(BOT)
    from dataclasses import replace
    sizing = replace(cfg.confidence_sizing, by_confidence=True)
    low, high = (D(x.split("=")[1]) for x in bounds.splitlines())
    with pytest.raises(config.ConfigError, match="min_usd and max_usd"):
        config.check_sizing(replace(sizing, min_usd=low, max_usd=high), cfg.risk,
                            replace(cfg.model, enabled=True))


def test_sound_bounds_pass_the_check():
    from dataclasses import replace
    cfg = config.load(BOT)
    config.check_sizing(
        replace(cfg.confidence_sizing, by_confidence=True, min_usd=D(200), max_usd=CAP),
        cfg.risk, replace(cfg.model, enabled=True))


def test_bounds_are_unused_while_off(tmp_path):
    cfg = _load_with(tmp_path, "by_confidence = false\nmin_usd = 9\nmax_usd = 99999")
    assert cfg.confidence_sizing.max_usd == 99999


def test_whatif_flat_against_sized(tmp_path, capsys):
    answers = tmp_path / "answers.csv"
    answers.write_text(
        "day,session,p_layer1,outcome,result_pct\n"
        "2026-11-02,regular,1,1,2\n"        # win at full confidence
        "2026-11-02,regular,0,0,-1\n"       # loss at no confidence
        "2026-11-03,regular,0.5,0,-2\n"     # loss
        "2026-11-03,extended,1,1,3\n")      # all-sessions line only
    out = tmp_path / "out"
    assert main(["sizing", "--answers", str(answers), "--all-sessions-usd", "4000",
                 "--min-usd", "200", "--max-usd", "800",
                 "--config", str(BOT), "--out", str(out)]) == 0
    summary = json.loads((out / "sizing_summary.json").read_text())
    main_line, all_line = summary["main_session"], summary["all_sessions"]
    # Flat at 1000: +20, -10, -20. Sized: 800 -> +16, 200 -> -2, 500 -> -10.
    assert (D(main_line["flat"]["earned"]), D(main_line["flat"]["lost"])) == (20, 30)
    assert (D(main_line["sized"]["earned"]), D(main_line["sized"]["lost"])) == (16, 12)
    assert main_line["sized"]["days_in_plus"] == 1 and main_line["sized"]["trades"] == 3
    # All sessions at 4000: the extended win at 3 percent, flat +120 (sized 3200 -> +96).
    assert all_line["flat"]["trades"] == 4 and D(all_line["flat"]["earned"]) == 200
    assert D(all_line["sized"]["earned"]) == 64 + 96
    with open(out / "sizing.csv", newline="") as handle:
        assert len(list(csv.DictReader(handle))) == 4


def test_whatif_refuses_bounds_not_chosen(tmp_path, capsys):
    answers = tmp_path / "answers.csv"
    answers.write_text("day,session,p_layer1,outcome,result_pct\n2026-11-02,regular,0.5,1,1\n")
    assert main(["sizing", "--answers", str(answers), "--all-sessions-usd", "4000",
                 "--config", str(BOT),
                 "--out", str(tmp_path / "o")]) == 2
    assert "bounds_outside_cap" in capsys.readouterr().err


def test_blank_probability_and_unnamed_column_are_refused(tmp_path, capsys):
    answers = tmp_path / "answers.csv"
    answers.write_text("day,session,p_a,p_b,outcome,result_pct\n2026-11-02,regular,0.5,,1,1\n")
    args = ["sizing", "--answers", str(answers), "--all-sessions-usd", "4000", "--min-usd", "100",
            "--max-usd", "500", "--config", str(BOT),
            "--out", str(tmp_path / "o")]
    assert main(args) == 2
    assert "--probability-column" in capsys.readouterr().err
    assert main([*args, "--probability-column", "p_b"]) == 2
    assert "probability_missing" in capsys.readouterr().err
    assert main([*args, "--probability-column", "p_a"]) == 0
