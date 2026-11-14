"""Historical replay: run a strategy bar by bar over recorded M1 bars. Results are SIMULATED.

Entry points: `load_bars`, `load_settings`, `replay`, `write_all`; CLI `python -m aperture.replay`.
"""

from aperture.replay.bars import load_bars
from aperture.replay.engine import LABEL, ReplayResult, replay
from aperture.replay.report import summarize, write_all
from aperture.replay.settings import ReplaySettings, load_settings

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
