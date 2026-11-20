"""Fleet runner of the tick history (ops/aws/training/jobs/tickrun.py), in its local test mode.

The same claim rules run on files instead of S3 objects (exclusive create, overwrite only of
the object one has seen). The converter is a stub: it needs the day's two raw files, writes the
day's table and adds a line to a ledger, so a day converted twice is visible.
"""

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

RUNNER = Path(__file__).resolve().parents[2] / "ops/aws/training/jobs/tickrun.py"
DAYS = ["2021-11-15", "2021-11-16", "2021-11-17", "2021-11-18", "2021-11-19"]  # Mon..Fri
STUB = """
import os, sys, time
dt, _, wd, out = sys.argv[1:5]
raw = [os.path.join(wd, "%s-%s.csv.gz" % (dt, k)) for k in ("trades", "quotes")]
assert all(os.path.exists(f) for f in raw)
time.sleep(float(os.environ.get("STUB_SLEEP", "0.2")))
os.makedirs(os.path.join(out, dt[:4]), exist_ok=True)
with open(os.path.join(out, "ledger.txt"), "a") as f:
    f.write(dt + "\\n")
if not os.environ.get("STUB_NO_TABLE"):
    open(os.path.join(out, dt[:4], dt + ".parquet"), "w").write(dt)
[os.remove(f) for f in raw]
print("day", dt)
"""
WAIT = 30  # seconds a test waits for a runner at most


def setup(tmp_path, days=DAYS, closed=()):
    fake = tmp_path / "fake"
    fake.mkdir()
    for d in days:
        for kind in ("trades", "quotes"):
            (fake / f"{d}-{kind}.csv.gz").write_bytes(f"{d} {kind}".encode())
    stub = tmp_path / "stub.py"
    stub.write_text(STUB)
    (tmp_path / "out" / "claims").mkdir(parents=True)
    # the calendar file: every day of the week that got no files is listed as closed
    (tmp_path / "closed.txt").write_text("# test\n" + "\n".join(closed))
    return stub


def start(tmp_path, stub, name, **env):
    e = dict(os.environ)
    e.update(
        RUN_FAKE=str(tmp_path / "fake"),
        RUN_FETCH="2",
        RUN_CONV="2",
        RUN_AHEAD="1",
        RUN_FLOOR_GB="0",
        RUN_WAIT_SEC="0.3",
        RUN_CLAIM_MIN="0.02",
        RUN_RAW=str(tmp_path / "raw"),
        RUN_CLOSED=str(tmp_path / "closed.txt"),
    )
    e.update(env)
    log = open(tmp_path / f"{name}.log", "w")
    args = [sys.executable, str(RUNNER), str(stub), DAYS[0], DAYS[-1], "0/1"]
    return subprocess.Popen(
        args + [str(tmp_path / name), str(tmp_path / "out")], env=e, stdout=log, stderr=log
    )


def tables(tmp_path):
    return sorted(p.stem for p in (tmp_path / "out").glob("*/*.parquet"))


def ledger(tmp_path):
    f = tmp_path / "out" / "ledger.txt"
    return sorted(f.read_text().split()) if f.exists() else []


def claims(tmp_path):
    return sorted(p.stem for p in (tmp_path / "out" / "claims").iterdir())


def test_two_machines_convert_every_day_once(tmp_path):
    stub = setup(tmp_path)
    a, b = start(tmp_path, stub, "a"), start(tmp_path, stub, "b")
    assert a.wait(WAIT) == 0 and b.wait(WAIT) == 0
    assert ledger(tmp_path) == DAYS  # each day once, by one machine
    assert tables(tmp_path) == DAYS
    assert claims(tmp_path) == []
    assert sorted(p.stem for p in (tmp_path / "rawman").glob("*/*.json")) == DAYS
    for kind in ("trades", "quotes"):
        assert len(list((tmp_path / "raw" / kind).glob("*/*.csv.gz"))) == len(DAYS)


def test_rerun_skips_everything_and_missing_files_are_a_holiday(tmp_path):
    stub = setup(tmp_path, DAYS[:3], closed=DAYS[3:])  # two closed days, no files
    assert start(tmp_path, stub, "a").wait(WAIT) == 0
    assert start(tmp_path, stub, "a").wait(WAIT) == 0
    assert ledger(tmp_path) == DAYS[:3]


def test_raw_switch_off_keeps_no_raw(tmp_path):
    stub = setup(tmp_path)
    p = start(tmp_path, stub, "a", RUN_RAW="")
    assert p.wait(WAIT) == 0
    assert tables(tmp_path) == DAYS and not (tmp_path / "raw").exists()


def test_tables_done_raw_record_missing_is_archived_only(tmp_path):
    stub = setup(tmp_path)
    assert start(tmp_path, stub, "a", RUN_RAW="").wait(WAIT) == 0
    assert start(tmp_path, stub, "a").wait(WAIT) == 0
    assert ledger(tmp_path) == DAYS  # nothing was converted again
    assert sorted(p.stem for p in (tmp_path / "rawman").glob("*/*.json")) == DAYS


def test_sigterm_stops_fast_and_releases_claims(tmp_path):
    stub = setup(tmp_path)
    p = start(tmp_path, stub, "a", STUB_SLEEP="60")
    deadline = time.time() + WAIT
    while not claims(tmp_path) and time.time() < deadline:
        time.sleep(0.1)
    time.sleep(1)
    p.send_signal(signal.SIGTERM)
    assert p.wait(WAIT) == 3
    assert claims(tmp_path) == [] and tables(tmp_path) == []


def test_days_of_a_dead_machine_are_taken_over(tmp_path):
    stub = setup(tmp_path)
    old, fresh = (tmp_path / "out" / "claims" / f"{d}.json" for d in DAYS[:2])
    old.write_text("dead")
    os.utime(old, (time.time() - 3600,) * 2)
    fresh.write_text("dead")  # goes stale while the runner works on the other days
    assert start(tmp_path, stub, "a").wait(WAIT) == 0
    assert ledger(tmp_path) == DAYS and claims(tmp_path) == []


def test_never_exits_zero_while_another_machine_holds_a_day(tmp_path):
    stub = setup(tmp_path)
    held = tmp_path / "out" / "claims" / f"{DAYS[0]}.json"
    held.write_text("alive")
    p = start(tmp_path, stub, "a", RUN_RAW="")
    t0 = time.time()
    while time.time() - t0 < 4:  # the other machine keeps its claim fresh
        os.utime(held)
        time.sleep(0.2)
    assert p.poll() is None  # own share finished long ago, the runner still waits
    assert ledger(tmp_path) == DAYS[1:]
    # the other machine finishes its day: table first, then the claim goes
    (tmp_path / "out" / "2021" / f"{DAYS[0]}.parquet").write_text("x")
    held.unlink()
    assert p.wait(WAIT) == 0
    assert ledger(tmp_path) == DAYS[1:]


def test_lost_claim_kills_the_converter_and_writes_nothing(tmp_path):
    stub = setup(tmp_path, DAYS[:1], closed=DAYS[1:])
    p = start(tmp_path, stub, "a", STUB_SLEEP="60", RUN_CLAIM_MIN="0.05")
    mine = tmp_path / "out" / "claims" / f"{DAYS[0]}.json"
    deadline = time.time() + WAIT
    while not mine.exists() and time.time() < deadline:
        time.sleep(0.1)
    time.sleep(1)
    mine.write_text("other")  # another machine took the day over
    t0 = time.time()
    log = tmp_path / "a.log"
    while "CLAIM LOST" not in log.read_text() and time.time() - t0 < WAIT:
        os.utime(mine)
        time.sleep(0.2)
    assert "CLAIM LOST" in log.read_text()
    p.send_signal(signal.SIGTERM)
    assert p.wait(WAIT) == 3
    assert mine.read_text() == "other"  # the other machine's claim is not deleted
    assert tables(tmp_path) == [] and "FAILED" not in log.read_text()


def test_exit_zero_needs_every_day_done_in_the_store(tmp_path):
    stub = setup(tmp_path)
    # the converter reports success and stores no table: the counters say done, the store not
    p = start(tmp_path, stub, "a", STUB_NO_TABLE="1", RUN_RAW="")
    assert p.wait(WAIT) == 1
    assert "converted here but not done in the store" in (tmp_path / "a.log").read_text()


def test_lost_day_is_done_here_after_the_other_machine_dies(tmp_path):
    stub = setup(tmp_path, DAYS[:1], closed=DAYS[1:])
    p = start(tmp_path, stub, "a", STUB_SLEEP="2", RUN_CLAIM_MIN="0.03", RUN_RAW="")
    mine = tmp_path / "out" / "claims" / f"{DAYS[0]}.json"
    deadline = time.time() + WAIT
    while not mine.exists() and time.time() < deadline:
        time.sleep(0.05)
    mine.write_text("other")  # taken once by another machine, which then dies (no refresh)
    assert p.wait(WAIT) == 0
    log = (tmp_path / "a.log").read_text()
    assert "CLAIM LOST" in log and "took over the stale claim" in log
    assert ledger(tmp_path) == DAYS[:1] and claims(tmp_path) == []


def test_one_file_of_a_day_missing_is_a_failure_not_a_holiday(tmp_path):
    stub = setup(tmp_path)
    (tmp_path / "fake" / f"{DAYS[1]}-quotes.csv.gz").unlink()
    p = start(tmp_path, stub, "a", RUN_RAW="")
    assert p.wait(WAIT) == 1
    assert "a session, and not complete at the source" in (tmp_path / "a.log").read_text()
    assert DAYS[1] not in ledger(tmp_path)


def test_session_without_files_is_a_failure_not_a_holiday(tmp_path):
    stub = setup(tmp_path, DAYS[:3], closed=DAYS[3:4])  # the last day is a session, no files
    p = start(tmp_path, stub, "a", RUN_RAW="")
    assert p.wait(WAIT) == 1
    assert "a session, and not complete at the source" in (tmp_path / "a.log").read_text()
    assert ledger(tmp_path) == DAYS[:3]


def test_files_on_a_day_listed_as_closed_are_a_failure(tmp_path):
    stub = setup(tmp_path, closed=DAYS[:1])
    p = start(tmp_path, stub, "a", RUN_RAW="")
    assert p.wait(WAIT) == 1
    assert "listed as closed but the source has files" in (tmp_path / "a.log").read_text()
    assert DAYS[0] not in ledger(tmp_path)


def test_closed_days_file_matches_the_repo_calendar():
    import json

    root = Path(__file__).resolve().parents[2]
    listed = [
        x.strip()
        for x in (root / "ops/aws/training/jobs/us-equity-closed-days.txt").read_text().split("\n")
        if x.strip() and not x.startswith("#")
    ]
    assert listed == sorted(set(listed))
    cal = json.loads((root / "config/calendar/us-equity-2026.json").read_text())
    closed_2026 = [
        e["day"] for e in cal["exceptions"] if e["day_kind"] == "CLOSED" and e["day"] <= listed[-1]
    ]
    assert [x for x in listed if x.startswith("2026")] == closed_2026


def test_two_failures_in_a_row_end_the_run_with_exit_one(tmp_path):
    # day 1 is good and converts slowly; days 2 and 3 are sessions without files
    stub = setup(tmp_path, [DAYS[0], DAYS[3], DAYS[4]])
    p = start(tmp_path, stub, "a", RUN_RAW="", RUN_FETCH="1", STUB_SLEEP="2")
    # the good day finishes AFTER the two failures: it must not un-pause the run
    assert p.wait(WAIT) == 1
    assert ledger(tmp_path) == DAYS[:1]
