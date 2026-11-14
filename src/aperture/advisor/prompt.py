"""The request text. Kept in one place so a reviewer can read exactly what the model is told."""

from __future__ import annotations

import json
from typing import Any

SYSTEM = """\
You review an intraday long-only US stock strategy for its single owner and propose the next
version of its settings. You receive the current strategy.toml (its comments explain every
value) and an evidence bundle computed from recorded trading days.

How to work:
- Base every change on the evidence. Name the numbers you rely on in each change's evidence.
- Change only values that already exist, in sections other than [strategy]. Keep each value's
  type (integer stays integer, float stays float, true/false stays boolean).
- Write each new value as a TOML literal: 8, 2.5, true, "text".
- Prefer few, small changes whose effect can be judged on the next days. At most {max_changes}.
- If the evidence is too thin or points nowhere clear, propose no change and say why in
  no_change_reason. That is a good answer.
- Price moves are observed, not profit; simulated R and USD use a conservative fill model and
  say nothing certain about real fills. Never promise results.
- The proposal will run as a shadow observer next to the active version before the owner
  decides; describe what to watch for in expected_effect and risks.
- Write in English, plain and short.
"""


def system_prompt(max_changes: int) -> str:
    return SYSTEM.format(max_changes=max_changes)


def user_prompt(strategy_toml: str, evidence: dict[str, Any]) -> str:
    return (
        "<strategy_toml>\n" + strategy_toml.rstrip() + "\n</strategy_toml>\n\n"
        "<evidence>\n" + json.dumps(evidence, indent=1, sort_keys=True) + "\n</evidence>\n\n"
        "Propose the next version as JSON in the required format."
    )
