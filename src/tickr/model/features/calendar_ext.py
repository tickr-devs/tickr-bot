"""The pinned training calendar, extended past its last day from the NYSE calendar.

`pinned/trading-days.txt` (md5-pinned training bytes) ends 2026-12-31 and is never edited. The
extension lies beside it: `trading-days-ext.txt` (same format: one session date per line, early
closes included as sessions), `early-close-days-ext.txt` and `exchange-closures-ext.txt` (the
formats of the training's sidecar files) and `calendar-ext.json` (source URL, fetch date, the
digests, the early-close times). Each file is md5-pinned here and checked against the sidecar
before use. `extend` appends the extension to the training list exactly once: it refuses an
extension that does not start after the training's last day or that overlaps it. A day past the
extension is a typed refusal (`calendar_uncovered`), never a guess. Early closes are sessions;
their close times are read from the sidecar, and `reconcile_bot_calendar` refuses when the bot's
own `config/calendar` disagrees about a closure or a close time.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

from tickr.model.errors import ModelRefused

PINNED_DIR = Path(__file__).with_name("pinned")
SIDECAR = "calendar-ext.json"
DAYS_FILE = "trading-days-ext.txt"
EARLY_FILE = "early-close-days-ext.txt"
CLOSURES_FILE = "exchange-closures-ext.txt"
# md5 of each extension file as built from the NYSE page on the sidecar's fetch date.
EXT_PINS = {
    DAYS_FILE: "9b11bd443b482c3ba8f6ac2908fa06fc",
    EARLY_FILE: "012834ee072149608e2dc03404727a94",
    CLOSURES_FILE: "843d4eba5058ce0c3a285d66c7b0c4a7",
}


@dataclass(frozen=True)
class Extension:
    days: tuple[str, ...]
    early_close: dict[str, str]  # session date -> close time of day, New York, "HH:MM"
    closures: tuple[str, ...]
    source_url: str
    fetched_on: str


def _lines(raw: bytes) -> list[str]:
    rows = (x.strip() for x in raw.decode("utf-8").split("\n"))
    return [x for x in rows if x and not x.startswith("#")]


def load(directory: Path = PINNED_DIR) -> Extension:
    """The extension, every file md5-checked against its pin and the sidecar."""
    raws: dict[str, bytes] = {}
    for name, pin in EXT_PINS.items():
        path = directory / name
        if not path.is_file():
            raise ModelRefused("calendar_ext_missing", name)
        raws[name] = path.read_bytes()
        if hashlib.md5(raws[name]).hexdigest() != pin:  # noqa: S324 - a pin, not security
            raise ModelRefused("calendar_ext_md5", name)
    side_path = directory / SIDECAR
    if not side_path.is_file():
        raise ModelRefused("calendar_ext_missing", SIDECAR)
    side = json.loads(side_path.read_text("utf-8"))
    if side.get("md5") != EXT_PINS:
        raise ModelRefused("calendar_ext_md5", f"{SIDECAR} records other digests")
    days = _lines(raws[DAYS_FILE])
    if not days or days != sorted(set(days)):
        raise ModelRefused("calendar_ext_unreadable", "days are not ascending and unique")
    times = dict(side["early_close_time_new_york"])
    early = _lines(raws[EARLY_FILE])
    if sorted(times) != early:
        raise ModelRefused("calendar_ext_unreadable", "early-close dates differ from the sidecar")
    closures = [x[:10] for x in _lines(raws[CLOSURES_FILE])]
    if set(early) - set(days) or set(closures) & set(days):
        raise ModelRefused("calendar_ext_unreadable", "early closes must be sessions, closures not")
    if (side["covers"]["first"], side["covers"]["last"]) != (days[0], days[-1]):
        raise ModelRefused("calendar_ext_unreadable", "sidecar coverage differs from the days")
    return Extension(tuple(days), times, tuple(closures), side["source_url"], side["fetched_on"])


def extend(training: list[str], ext: Extension | None = None) -> list[str]:
    """`training` (the md5-checked pinned list) followed by the extension, once."""
    ext = ext or load()
    if training and ext.days[0] <= training[-1]:
        raise ModelRefused("calendar_ext_overlap",
                           f"extension starts {ext.days[0]}, training ends {training[-1]}")
    return [*training, *ext.days]


def require_covered(day: str, calendar: list[str]) -> None:
    """Refuse a date after the last covered day (`calendar` as `extend` returned it)."""
    if not calendar or day > calendar[-1]:
        raise ModelRefused("calendar_uncovered", f"{day} is after the last covered day")


def early_close(day: str, ext: Extension | None = None) -> str | None:
    """The early close time (New York) of an extension session, else None. Only the extension
    carries close times; a day outside its coverage is not answered here."""
    ext = ext or load()
    return ext.early_close.get(day)


def reconcile_bot_calendar(config_dir: Path, ext: Extension | None = None) -> None:
    """Refuse when the bot's `us-equity-<year>.json` files disagree with the extension about a
    closure, an early close or its time, for every year both cover."""
    ext = ext or load()
    for path in sorted(Path(config_dir).glob("us-equity-*.json")):
        data = json.loads(path.read_text("utf-8"))
        first, last = data["coverage_start"], data["coverage_end"]
        if last < ext.days[0] or first > ext.days[-1]:
            continue
        closed = {e["day"] for e in data["exceptions"] if e["day_kind"] == "CLOSED"}
        half = {e["day"]: e["regular_close"] for e in data["exceptions"]
                if e["day_kind"] == "HALF_DAY"}
        mine_closed = {d for d in ext.closures if first <= d <= last}
        mine_half = {d: t for d, t in ext.early_close.items() if first <= d <= last}
        # closures on weekends do not occur in the extension (weekdays only)
        if closed != mine_closed or half != mine_half:
            raise ModelRefused("calendar_disagrees", f"{path.name}: closures or early closes")
