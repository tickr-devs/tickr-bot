"""Protection tightened while the bot runs (backlog S5: tightening protection applies at once).

A running bot looks at its config file once per loop pass, before any strategy work. The file's
SHA-256 is the version check: an unchanged digest costs one read and nothing else, and each new
digest is examined exactly once. A new version is validated in full exactly as at startup
(`config.loads` on the very bytes that were digested, so a file caught half-written is an invalid
file, never a torn mix). It is adopted only when it differs from the last valid version the bot
examined (at first: the config it started with) in protection settings alone (`protection_specs`),
and every protection value that moved is stricter than the one in force, by the direction the
settings schema records (`settings_schema.Loosens`). Then all of them are adopted together;
otherwise none is: a looser value, an invalid or unreadable file, or a change of any other key is
ignored for the rest of this run (a typed `protection-change-ignored` row and an alert). Loosening
and every other setting take effect at the next start, as before (api/settingsapply.py). A change of
another key is reported once: a later version that differs from it in protection settings alone (the
settings API's next tightening) is adopted, while the other key keeps its started value until the
next start, so the apply answer's `restart_needed` holds for that change only.

What is adopted feeds entry admission only (`EntryDesk.adopt`): a tightening affects new
entries, never the protection of a trade already open (its broker stop stays as placed) and
never a working entry. The bot's status shows the values in force.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from dataclasses import dataclass, fields, replace
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

from tickr import config as config_module
from tickr.config import Config, RiskConfig
from tickr.settings_schema import PLATFORM, RECORDS, Applies, Loosens, Setting

ADOPTED = "protection-adopted"  # journal kind: stricter protection values now in force
IGNORED = "protection-change-ignored"  # journal kind: a new config version not adopted
RISK = "risk"
_NOT_SECTIONS = frozenset({RISK, "root", "digest", "renamed_keys"})


class IgnoredCause(StrEnum):
    UNREADABLE = "unreadable"  # the file could not be read
    INVALID = "invalid"  # the full validation of the startup refuses it
    LOOSENS = "loosens"  # a protection value is looser than the one in force
    OTHER_KEYS = "other_keys"  # a setting outside live protection differs from the start


def protection_specs() -> tuple[Setting, ...]:
    """The settings a running bot may tighten: the platform's protection settings, those the
    schema applies at once and that have a loosening direction. All live in [risk]."""
    specs = tuple(s for s in RECORDS if s.owner == PLATFORM and s.applies is Applies.AT_ONCE
                  and s.loosens is not Loosens.NEVER)
    names = {f.name for f in fields(RiskConfig)}
    for spec in specs:
        table, _, name = spec.key.partition(".")
        if table != RISK or name not in names:
            raise ValueError(f"protection setting {spec.key} is not a [risk] key")
    return specs


def attribute(spec: Setting) -> str:
    return spec.key.partition(".")[2]


def stricter(spec: Setting, old: Any, new: Any) -> bool:
    """True when `new` protects more than `old`: lower where raising loosens, higher where
    lowering loosens (on/off: on is higher). Where 0 = off and raising loosens (a loss lock),
    turning it on (0 to a value) or lowering it to another value above 0 is stricter; raising
    it or turning it off (to 0) is not."""
    if spec.loosens is Loosens.RAISE:
        return new < old
    if spec.loosens is Loosens.LOWER:
        return new > old
    if spec.loosens is Loosens.RAISE_OR_OFF:
        return new != 0 and (old == 0 or new < old)
    return False


def toml_text(value: Any) -> str:
    return str(value).lower() if isinstance(value, bool) else str(value)


@dataclass(frozen=True)
class Adoption:
    digest_before: str
    digest_after: str
    changes: tuple[tuple[str, Any, Any], ...]  # dotted key, value before, value adopted

    def journal_body(self) -> dict[str, Any]:
        return {"digest_before": self.digest_before, "digest_after": self.digest_after,
                "changes": [{"key": k, "before": toml_text(b), "after": toml_text(a)}
                            for k, b, a in self.changes]}


@dataclass(frozen=True)
class Ignored:
    digest: str | None  # None when the file could not be read
    cause: IgnoredCause
    detail: str

    def journal_body(self) -> dict[str, Any]:
        return {"digest": self.digest, "cause": self.cause.value, "detail": self.detail}

    def alert_text(self) -> str:
        return (f"settings file change not adopted ({self.cause.value}): {self.detail}. "
                "The protection in force stays as it is until the next start.")


Reader = Callable[[Path], bytes]


class LiveProtection:
    """The protection values in force for new entries, and the check that tightens them."""

    def __init__(self, started: Config, path: Path,
                 read: Reader = lambda p: p.read_bytes()) -> None:
        self.started = started  # the config this run started with
        # The last valid version examined: other keys are compared with it, so a change of one
        # (ignored, never adopted) is reported once and does not block later tightenings.
        self._last_valid = started
        self.path = path
        self._read = read
        self.specs = protection_specs()
        self.risk: RiskConfig = started.risk  # in force for new entries
        self.adopted_at: dict[str, datetime] = {}  # dotted key -> when it was tightened
        self._in_force = started.digest  # digest of the version the values in force came from
        self._seen = started.digest  # last digest examined (adopted or ignored)
        self._unreadable = False  # the last read failed (journaled once per stretch)

    @property
    def config(self) -> Config:
        """The started config with the protection values in force."""
        return replace(self.started, risk=self.risk)

    def check(self, now: datetime) -> Adoption | Ignored | None:
        """Look at the file once; None when there is nothing new to report."""
        try:
            raw = self._read(self.path)
        except OSError as exc:
            if self._unreadable:
                return None
            self._unreadable = True
            return Ignored(None, IgnoredCause.UNREADABLE, f"{type(exc).__name__}")
        self._unreadable = False
        digest = hashlib.sha256(raw).hexdigest()
        if digest == self._seen:
            return None
        self._seen = digest
        try:
            found = config_module.loads(raw, self.path, self.started.root)
        except (ValueError, TypeError) as exc:  # ConfigError and a bad encoding are ValueError
            return Ignored(digest, IgnoredCause.INVALID, str(exc))
        other = self._other_changes(found)
        self._last_valid = found
        if other:
            return Ignored(digest, IgnoredCause.OTHER_KEYS,
                           "changed outside live protection: " + ", ".join(other))
        moved: list[tuple[Setting, Any, Any]] = []
        for spec in self.specs:
            name = attribute(spec)
            old, new = getattr(self.risk, name), getattr(found.risk, name)
            if new != old:
                moved.append((spec, old, new))
        if looser := [f"{s.key} {toml_text(o)} -> {toml_text(n)}" for s, o, n in moved
                      if not stricter(s, o, n)]:
            return Ignored(digest, IgnoredCause.LOOSENS,
                           "looser than in force: " + ", ".join(looser))
        if not moved:
            return None  # back to values already in force, or only comments changed
        self.risk = replace(self.risk, **{attribute(s): n for s, _, n in moved})
        self.adopted_at.update(dict.fromkeys((s.key for s, _, _ in moved), now))
        before, self._in_force = self._in_force, digest
        return Adoption(before, digest, tuple((s.key, o, n) for s, o, n in moved))

    def _other_changes(self, found: Config) -> list[str]:
        """Keys of `found` outside live protection that differ from the last valid version."""
        live = {attribute(s) for s in self.specs}
        other = [f"[{f.name}]" for f in fields(Config)
                 if f.name not in _NOT_SECTIONS
                 and getattr(found, f.name) != getattr(self._last_valid, f.name)]
        other += [f"{RISK}.{f.name}" for f in fields(RiskConfig)
                  if f.name not in live
                  and getattr(found.risk, f.name) != getattr(self._last_valid.risk, f.name)]
        return other

    def in_force(self) -> tuple[tuple[str, Any, datetime | None], ...]:
        """Every live protection setting: dotted key, value in force, when it was tightened
        (None: since the start)."""
        return tuple((s.key, getattr(self.risk, attribute(s)), self.adopted_at.get(s.key))
                     for s in self.specs)
