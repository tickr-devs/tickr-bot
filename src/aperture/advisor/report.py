"""The markdown report of one advisor run, and the idea note. Always written, whatever the
outcome."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from aperture.advisor.proposal import Idea, Proposal

if TYPE_CHECKING:
    from aperture.advisor.run import Tuning

TITLES = {
    "proposed": "Proposed new version",
    "idea": "New strategy idea",
    "no_change": "No change proposed",
    "refused": "Proposal refused",
}


def _cell(text: Any) -> str:
    return " ".join(str(text).split()).replace("|", "\\|")


def _idea_lines(idea: Idea) -> list[str]:
    lines = []
    for title, text in (("Hypothesis", idea.hypothesis), ("Entry", idea.entry_rule),
                        ("Exit", idea.exit_rule), ("Filters", idea.filters),
                        ("Evidence", idea.evidence), ("How to test", idea.test_plan)):
        lines += [f"**{title}.** {text or 'not given'}", ""]
    return lines


def render_idea(idea: Idea, tuning: Tuning) -> str:
    """idea.md: what a developer needs to write the module, and how it is judged."""
    module = idea.name.replace("-", "_")
    return "\n".join([
        f"# Strategy idea: {idea.name}",
        "",
        "Written by the AI advisor. Not code and not applied.",
        "",
        *_idea_lines(idea),
        "## Next steps",
        "",
        f"1. A developer writes `src/aperture/strategy/{module}.py` (`create(values)`, a "
        "Decision with its checks; it never sends orders) and "
        f"`strategies/{idea.name}/strategy.toml` with every number commented, in a pull "
        "request with its own review.",
        "2. Backtest it on the same data: `python -m aperture.backtest run ... --strategy "
        f"aperture.strategy.{module} --values strategies/{idea.name}/strategy.toml`.",
        f"3. The idea came from {tuning.tuned_on}; results there prove nothing. Whoever writes "
        "the module has usually seen this window's hold-out numbers, so judge it on sessions "
        "after the window (a fresh fetch) or on observer days, not on this hold-out.",
        "",
    ])


def render_report(
    *,
    status: str,
    cause: str,
    proposal: Proposal | None,
    new_identity: str | None,
    target: Path | None,
    out: Path,
    model: str,
    base: str,
    folder: str,
    tuning: Tuning,
    now: datetime,
) -> str:
    lines = [
        f"# Strategy advisor: {TITLES[status]}",
        "",
        f"- Base: {base} (folder `{folder}`)",
        f"- Evidence: {tuning.label}",
        f"- Model: {model}",
        f"- Run: {now:%Y-%m-%d %H:%M} UTC",
        "- **Not applied.** Results in the evidence are observed price moves or simulated "
        "trades (gross, conservative fills), never real profit.",
        "",
    ]
    if status == "refused":
        lines += ["## Why it was refused", "", cause, "",
                  "The raw answer is in `response.json`.", ""]
    if proposal is not None:
        lines += ["## Summary", "", proposal.summary, ""]
        if proposal.changes:
            lines += ["## Changes", "",
                      "| Value | Now | Proposed | Reason | Evidence |",
                      "|---|---|---|---|---|"]
            for c in proposal.changes:
                lines.append(f"| `[{c.section}] {c.key}` | {c.old!r} | {c.new!r} | "
                             f"{_cell(c.reason)} | {_cell(c.evidence)} |")
            lines += ["", "## Expected effect", "", proposal.expected_effect, "", "## Risks", ""]
            lines += [f"- {r}" for r in proposal.risks] or ["- none named"]
            lines.append("")
        elif proposal.idea is None:
            lines += ["## Why no change", "", proposal.no_change_reason, ""]
        if proposal.idea is not None:
            lines += [f"## New strategy idea: {proposal.idea.name}", "",
                      *_idea_lines(proposal.idea),
                      "Next steps are in `idea.md`.", ""]
    if status == "proposed" and target is not None:
        relative = target.relative_to(out)
        try_it = (
            [f"1. Backtest it on the same data: `python -m aperture.backtest run ... --values "
             f"{relative}/strategy.toml`.",
             f"2. `python -m aperture.advisor.judge --run {out} --results <its results>` "
             "compares it with the base on the withheld hold-out.",
             f"3. If it holds there, copy `{relative}` to `strategies/{target.name}/` and run "
             "it as an observer."]
            if tuning.backtest else
            [f"1. Copy `{relative}` to `strategies/{target.name}/` in the repository.",
             f'2. Add `"{target.name}"` to `[strategy] observers` in `config/bot.toml`.',
             "3. After some days, compare it with `python -m aperture.research observers`."]
        )
        lines += [
            "## How to try it",
            "",
            f"The new version is **{new_identity}** in `{relative}/strategy.toml`.",
            *try_it,
            "",
            f"Hold-out rule: this version was tuned on {tuning.tuned_on}. Judge it only on "
            f"{tuning.judge_on}; results on the tuning days prove nothing.",
            "",
        ]
    lines += ["## Evidence headline", "", *[f"- {line}" for line in tuning.headline], "",
              "Full numbers: `evidence.json`. Exact request: `request.txt`.", ""]
    return "\n".join(lines)
