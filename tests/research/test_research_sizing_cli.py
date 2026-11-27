"""The sizing command reads its rounding from the replay settings, like the other reports."""

from __future__ import annotations

from research_helpers import HERE

from tickr.research.__main__ import main

CLOCK = ["--replay-settings", str(HERE / "replay_settings.toml"),
         "--config", str(HERE.parent.parent / "config" / "bot.toml")]


def test_sizing_needs_no_research_settings(tmp_path):
    answers = tmp_path / "a.csv"
    answers.write_text("outcome,p_a,day,session,result_pct\n1,0.8,2026-01-05,regular,1.0\n")
    code = main(["sizing", "--answers", str(answers), "--all-sessions-usd", "4000",
                 "--min-usd", "500", "--max-usd", "1000", "--out", str(tmp_path / "o"), *CLOCK])
    assert code == 0
    assert (tmp_path / "o" / "sizing.csv").exists()
