"""The markdown report of one advisor run. Always written, whatever the outcome."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date, datetime
from pathlib import Path
from typing import Any

from aperture.advisor.proposal import Proposal

TITLES = {
    "proposed": "Proposed new version",
    "no_change": "No change proposed",
    "refused": "Proposal refused",
}


def _cell(text: Any) -> str:
    return " ".join(str(text).split()).replace("|", "\\|")


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
    days: Sequence[date],
    now: datetime,
    evidence: dict[str, Any],
) -> str:
    lines = [
        f"# Strategy advisor: {TITLES[status]}",
        "",
        f"- Base: {base} (folder `{folder}`)",
        f"- Days: {len(days)} ({days[0].isoformat()} .. {days[-1].isoformat()})",
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
        else:
            lines += ["## Why no change", "", proposal.no_change_reason, ""]
    if status == "proposed" and target is not None:
        relative = target.relative_to(out)
        lines += [
            "## How to try it",
            "",
            f"The new version is **{new_identity}** in `{relative}/strategy.toml`.",
            f"1. Copy `{relative}` to `strategies/{target.name}/` in the repository.",
            f'2. Add `"{target.name}"` to `[strategy] observers` in `config/bot.toml`.',
            "3. After some days, compare it with `python -m aperture.research observers`.",
            "",
        ]
    lines += [
        "## Evidence headline",
        "",
        f"- Decisions: {evidence['decisions']} over {evidence['symbols']} symbols; "
        f"verdicts {evidence['verdicts']}",
        "- First failed check (IGNORE/WATCH): "
        f"{evidence['first_failed_check_on_ignore_and_watch']}",
        f"- ENTER refusals: {evidence['enter_refusal_causes']}",
        f"- Not-taken ENTERs simulated: {evidence['not_taken_enters_simulated']['trades']} trades, "
        f"total R {evidence['not_taken_enters_simulated']['total_r']}",
        "",
        "Full numbers: `evidence.json`. Exact request: `request.txt`.",
        "",
    ]
    return "\n".join(lines)
