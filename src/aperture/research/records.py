"""Decision records: one recorded strategy Decision per JSONL line.

This is the input of every research tool and the format the bot is expected to write when it
records decisions (`record_to_json` / `write_records`). One JSON object per line with exactly
the keys in `RECORD_KEYS`:

- `time`: when the decision was made, ISO-8601 with an explicit offset (stored as UTC).
- `bar_start`: start (UTC) of the last completed M1 bar the strategy saw; the decision bar.
- `symbol`, `strategy`, `version`, `role` (`active` / `observer`), `verdict`.
- `setup`, `trigger`, `invalidation`, `entry_low`, `entry_high`, `stop`, `quantity`:
  decimals as JSON strings, or null.
- `admission`: what the platform did with it. `taken` / `refused` for an active ENTER that
  reached admission, `not_evaluated` for an ENTER that never did (observer, replay),
  `not_applicable` for every other verdict.
- `refusal_cause`: non-empty string when `admission` is `refused`, otherwise null.
- `checks`: list of {rule, measured, operator, threshold, passed, note}.

Any deviation is a `RecordFileError` naming the file, line and cause; nothing is defaulted.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from pathlib import Path
from typing import Any

from aperture.research.errors import RecordFileError
from aperture.strategy.base import Check, Decision, Role, Verdict

M1 = timedelta(minutes=1)

DECIMAL_KEYS = ("trigger", "invalidation", "entry_low", "entry_high", "stop", "quantity")
RECORD_KEYS = frozenset(
    {
        "time",
        "bar_start",
        "symbol",
        "strategy",
        "version",
        "role",
        "verdict",
        "setup",
        *DECIMAL_KEYS,
        "admission",
        "refusal_cause",
        "checks",
    }
)
CHECK_KEYS = frozenset({"rule", "measured", "operator", "threshold", "passed", "note"})


class Admission(StrEnum):
    TAKEN = "taken"
    REFUSED = "refused"
    NOT_EVALUATED = "not_evaluated"
    NOT_APPLICABLE = "not_applicable"


@dataclass(frozen=True)
class DecisionRecord:
    time: datetime
    bar_start: datetime
    role: Role
    decision: Decision
    admission: Admission
    refusal_cause: str | None = None

    @property
    def symbol(self) -> str:
        return self.decision.symbol

    def problem(self) -> str | None:
        """Why this record breaks the format rules, or None."""
        verdict = self.decision.verdict
        for name, value in (("time", self.time), ("bar_start", self.bar_start)):
            if value.tzinfo is None:
                return f"{name} has no UTC offset"
        if self.bar_start.second or self.bar_start.microsecond:
            return "bar_start is not on a whole minute"
        if self.time < self.bar_start + M1:
            return "time is before the decision bar completed"
        if verdict is Verdict.ENTER:
            if self.admission is Admission.NOT_APPLICABLE:
                return "ENTER needs admission taken, refused or not_evaluated"
        elif self.admission is not Admission.NOT_APPLICABLE:
            return f"{verdict.value} must have admission not_applicable"
        if self.admission is Admission.TAKEN and self.role is not Role.ACTIVE:
            return "only the active role can have an ENTER taken"
        if self.admission is Admission.REFUSED:
            if not self.refusal_cause:
                return "refused without refusal_cause"
        elif self.refusal_cause is not None:
            return "refusal_cause set but admission is not refused"
        return None


def _decimal_or_none(key: str, raw: Any) -> Decimal | None:
    if raw is None:
        return None
    if not isinstance(raw, str):
        raise ValueError(f"{key} must be a string or null, got {type(raw).__name__}")
    try:
        value = Decimal(raw)
    except InvalidOperation as exc:
        raise ValueError(f"{key} is not a decimal: {raw!r}") from exc
    if not value.is_finite():
        raise ValueError(f"{key} is not finite: {raw!r}")
    return value


def _time(key: str, raw: Any) -> datetime:
    if not isinstance(raw, str):
        raise ValueError(f"{key} must be an ISO-8601 string")
    try:
        value = datetime.fromisoformat(raw)
    except ValueError as exc:
        raise ValueError(f"{key} is not ISO-8601: {raw!r}") from exc
    if value.tzinfo is None:
        raise ValueError(f"{key} has no UTC offset: {raw!r}")
    return value.astimezone(UTC)


def _text(key: str, raw: Any, optional: bool = False) -> str | None:
    if raw is None and optional:
        return None
    if not isinstance(raw, str) or not raw:
        raise ValueError(f"{key} must be a non-empty string")
    return raw


def _enum(key: str, raw: Any, kind: type[StrEnum]) -> Any:
    try:
        return kind(raw)
    except ValueError as exc:
        allowed = ", ".join(m.value for m in kind)
        raise ValueError(f"{key} {raw!r} is not one of: {allowed}") from exc


def _check(raw: Any) -> Check:
    if not isinstance(raw, dict) or set(raw) != CHECK_KEYS:
        raise ValueError(f"each check needs exactly the keys {sorted(CHECK_KEYS)}")
    if not isinstance(raw["passed"], bool):
        raise ValueError("check passed must be true or false")
    if not isinstance(raw["note"], str):
        raise ValueError("check note must be a string")
    return Check(
        rule=str(_text("check rule", raw["rule"])),
        measured=_decimal_or_none("check measured", raw["measured"]),
        operator=str(_text("check operator", raw["operator"])),
        threshold=_decimal_or_none("check threshold", raw["threshold"]),
        passed=raw["passed"],
        note=raw["note"],
    )


def record_from_mapping(data: Any) -> DecisionRecord:
    """Build a record from one parsed JSON object. Raises ValueError with the cause."""
    if not isinstance(data, dict):
        raise ValueError("record is not a JSON object")
    keys = set(data)
    if keys != RECORD_KEYS:
        missing = sorted(RECORD_KEYS - keys)
        unknown = sorted(keys - RECORD_KEYS)
        raise ValueError(f"keys mismatch: missing={missing} unknown={unknown}")
    if not isinstance(data["checks"], list):
        raise ValueError("checks must be a list")
    symbol = _text("symbol", data["symbol"])
    if symbol != symbol.strip():
        raise ValueError(f"symbol has surrounding whitespace: {symbol!r}")
    decision = Decision(
        strategy=str(_text("strategy", data["strategy"])),
        version=str(_text("version", data["version"])),
        symbol=str(symbol),
        verdict=_enum("verdict", data["verdict"], Verdict),
        checks=tuple(_check(c) for c in data["checks"]),
        setup=_text("setup", data["setup"], optional=True),
        **{key: _decimal_or_none(key, data[key]) for key in DECIMAL_KEYS},
    )
    record = DecisionRecord(
        time=_time("time", data["time"]),
        bar_start=_time("bar_start", data["bar_start"]),
        role=_enum("role", data["role"], Role),
        decision=decision,
        admission=_enum("admission", data["admission"], Admission),
        refusal_cause=_text("refusal_cause", data["refusal_cause"], optional=True),
    )
    problem = record.problem()
    if problem is not None:
        raise ValueError(problem)
    return record


def _str(value: Decimal | None) -> str | None:
    return None if value is None else str(value)


def record_to_json(record: DecisionRecord) -> str:
    """One JSONL line (without newline). Refuses a record that breaks the format rules."""
    problem = record.problem()
    if problem is not None:
        raise ValueError(f"cannot write decision record: {problem}")
    d = record.decision
    body = {
        "time": record.time.astimezone(UTC).isoformat(),
        "bar_start": record.bar_start.astimezone(UTC).isoformat(),
        "symbol": d.symbol,
        "strategy": d.strategy,
        "version": d.version,
        "role": record.role.value,
        "verdict": d.verdict.value,
        "setup": d.setup,
        **{key: _str(getattr(d, key)) for key in DECIMAL_KEYS},
        "admission": record.admission.value,
        "refusal_cause": record.refusal_cause,
        "checks": [
            {
                "rule": c.rule,
                "measured": _str(c.measured),
                "operator": c.operator,
                "threshold": _str(c.threshold),
                "passed": c.passed,
                "note": c.note,
            }
            for c in d.checks
        ],
    }
    return json.dumps(body, sort_keys=True)


def write_records(path: Path, records: Iterable[DecisionRecord]) -> None:
    with open(path, "w", encoding="utf-8") as handle:
        for record in records:
            handle.write(record_to_json(record) + "\n")


def load_records(paths: Sequence[Path]) -> tuple[DecisionRecord, ...]:
    """Read every file in order; blank lines are ignored, anything malformed is refused."""
    out: list[DecisionRecord] = []
    for path in paths:
        name = str(path)
        with open(path, encoding="utf-8") as handle:
            for number, text in enumerate(handle, start=1):
                if not text.strip():
                    continue
                try:
                    out.append(record_from_mapping(json.loads(text)))
                except json.JSONDecodeError as exc:
                    raise RecordFileError(name, number, f"invalid JSON: {exc.msg}") from exc
                except ValueError as exc:
                    raise RecordFileError(name, number, str(exc)) from exc
    return tuple(out)
