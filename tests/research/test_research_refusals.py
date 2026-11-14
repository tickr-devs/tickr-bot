from __future__ import annotations

import csv
import json
from datetime import date
from decimal import Decimal

from research_helpers import DAY, HERE, NY, record

from aperture.research.__main__ import main
from aperture.research.records import Admission, write_records
from aperture.research.refusals import table
from aperture.strategy.base import Check, Role, Verdict


def _fail(rule: str) -> tuple[Check, ...]:
    return (Check("ok", Decimal(1), ">=", Decimal(0), True),
            Check(rule, Decimal(0), ">=", Decimal(1), False),
            Check("later", Decimal(0), ">=", Decimal(1), False))


NEXT = date(2026, 11, 11)
RECORDS = (
    record("10:00", Verdict.IGNORE, checks=_fail("relative_volume")),
    record("10:01", Verdict.IGNORE, checks=_fail("relative_volume")),
    record("10:02", Verdict.WATCH, setup="breakout", checks=_fail("spread")),
    record("10:03", Verdict.ENTER, setup="breakout", cause="daily loss ceiling"),
    record("10:04", Verdict.ENTER, setup="breakout", admission=Admission.TAKEN),
    record("10:05", Verdict.ENTER, setup="breakout", role=Role.OBSERVER, version="2",
           admission=Admission.NOT_EVALUATED),
    record("10:06", Verdict.HOLD),
    record("10:07", Verdict.IGNORE),  # nothing failed: not counted
    record("15:59", Verdict.IGNORE, day=NEXT, checks=_fail("spread")),
)


def _rows(records):
    return [(r.day, r.version, r.role, r.setup, r.kind, r.name, r.count)
            for r in table(records, NY)]


def test_counts_first_failed_check_platform_cause_and_entries_per_day_and_setup():
    assert _rows(RECORDS) == [
        (DAY, "1", "active", None, "first_failed_check", "relative_volume", 2),
        (DAY, "1", "active", "breakout", "first_failed_check", "spread", 1),
        (DAY, "1", "active", "breakout", "platform_refusal", "daily loss ceiling", 1),
        (DAY, "1", "active", "breakout", "enter", "refused", 1),
        (DAY, "1", "active", "breakout", "enter", "taken", 1),
        (DAY, "2", "observer", "breakout", "enter", "not_evaluated", 1),
        (NEXT, "1", "active", None, "first_failed_check", "spread", 1),
    ]


def test_day_is_the_session_date_in_the_exchange_zone():
    late = record("19:30", Verdict.IGNORE, checks=_fail("spread"))  # 00:30 UTC next day
    assert _rows([late])[0][0] == DAY


def test_cli_writes_csv_and_summary_and_filters_strategy(tmp_path, capsys):
    decisions = tmp_path / "d.jsonl"
    write_records(decisions, RECORDS)
    out = tmp_path / "r"
    assert main(["refusals", "--decisions", str(decisions), "--strategy", "s:1",
                 "--settings", str(HERE / "research_settings.toml"), "--out", str(out)]) == 0
    with open(out / "refusals.csv", newline="") as handle:
        rows = list(csv.DictReader(handle))
    assert rows[0] == {"day": "2026-11-10", "strategy": "s", "version": "1", "role": "active",
                       "setup": "", "kind": "first_failed_check", "name": "relative_volume",
                       "count": "2"}
    assert {r["version"] for r in rows} == {"1"}
    summary = json.loads((out / "refusals_summary.json").read_text())
    assert summary == {"days": ["2026-11-10", "2026-11-11"], "rows": 6,
                       "by_kind": {"first_failed_check": 4, "platform_refusal": 1, "enter": 2}}
    assert "by_kind" in capsys.readouterr().out
