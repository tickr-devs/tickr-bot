"""Historical replay: run a strategy bar by bar over recorded M1 bars. Results are SIMULATED.

Entry points: `load_bars`, `load_settings`, `replay`, `write_all`; CLI `python -m tickr.replay`.
"""

from tickr.replay.bars import load_bars
from tickr.replay.engine import LABEL, ReplayResult, replay
from tickr.replay.report import summarize, write_all
from tickr.replay.settings import ReplaySettings, load_settings

__all__ = [
    "LABEL",
    "ReplayResult",
    "ReplaySettings",
    "load_bars",
    "load_settings",
    "replay",
    "summarize",
    "write_all",
]
