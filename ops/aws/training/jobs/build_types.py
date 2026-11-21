"""Builds the type list the tick converter filters by (TICK_TYPES): columns ticker,type.

Rule: from the two ticker snapshots of the reference data (active and inactive), keep type CS or
ADRC when the ticker is active or was delisted on or after the first day of the range. A ticker
without a type is not kept. Run with the first day 2021-11-16 it gives reference/types-5y.csv
byte for byte (checked 2026-11-21); the older pass uses the first day 2019-11-01.

usage: build_types.py tickers-active.jsonl tickers-inactive.jsonl FIRST_DAY out.csv
"""

import csv
import json
import sys

active, inactive, first, out = sys.argv[1:5]
KEPT = ("CS", "ADRC")
rows = {}
for name, default in ((active, True), (inactive, False)):
    for line in open(name):
        if not line.strip():
            continue
        j = json.loads(line)
        if j.get("type") not in KEPT:
            continue
        if j.get("active", default) or (j.get("delisted_utc") or "")[:10] >= first:
            rows[j["ticker"]] = j["type"]
with open(out, "w", newline="") as f:
    w = csv.writer(f)
    w.writerow(["ticker", "type"])
    for t in sorted(rows):
        w.writerow([t, rows[t]])
print(len(rows))
