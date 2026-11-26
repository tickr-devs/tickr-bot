"""Fleet runner of the tick history (ops/aws/training/jobs/tickrun.py), in its local test mode.

The same claim rules run on files instead of S3 objects (exclusive create, overwrite only of
the object one has seen). The converter is a stub: it needs the day's two raw files, writes the
day's table and adds a line to a ledger, so a day converted twice is visible.
"""

import io
import json
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
    e = dict(  # named variables only (rule 10): the whole environment is never passed on
        PATH=os.environ["PATH"],
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


def _conv_settings(tmp_path, switch, mem_gb, cores=16):
    """Runs the job script's converter-settings lines with aws, free and nproc replaced."""
    root = Path(__file__).resolve().parents[2]
    text = (root / "ops/aws/training/jobs/tickboot.sh").read_text().split("\n")
    a = next(i for i, x in enumerate(text) if x.startswith("NCONV="))
    b = next(i for i, x in enumerate(text) if x.startswith('say "converters'))
    stubs = tmp_path / "bin"
    stubs.mkdir(parents=True)
    for name, body in (
        ("aws", f'printf "%s" "{switch}"' if switch is not None else "exit 1"),
        ("free", f'echo header; echo "Mem: {mem_gb} 1 1 1 1 1"'),
        ("nproc", f"echo {cores}"),
    ):
        f = stubs / name
        f.write_text("#!/bin/sh\n" + body + "\n")
        f.chmod(0o755)
    script = "J=x\n" + "\n".join(text[a:b]) + '\necho "$NCONV $CTHR $CMEM"\n'
    env = {"PATH": f"{stubs}:{os.environ['PATH']}"}
    return subprocess.run(
        ["sh", "-c", script], env=env, capture_output=True, text=True, check=True
    ).stdout.split()


def _conv_settings_one_core():
    import tempfile

    with tempfile.TemporaryDirectory() as d:
        return _conv_settings(Path(d) / "x", "2", 30, cores=1)


def test_job_script_runs_one_converter_unless_the_switch_says_two(tmp_path):
    assert _conv_settings(tmp_path / "a", None, 30) == ["1", "16", "20GB"]
    assert _conv_settings(tmp_path / "b", "1\n", 61) == ["1", "16", "20GB"]
    assert _conv_settings(tmp_path / "c", "on", 61) == ["1", "16", "20GB"]


def test_two_converters_share_cores_and_memory_by_machine_size(tmp_path):
    assert _conv_settings(tmp_path / "a", "2\n", 30) == ["2", "8", "10GB"]
    assert _conv_settings(tmp_path / "b", "2", 61) == ["2", "8", "20GB"]


def test_two_converter_result_is_kept_per_setting():
    root = Path(__file__).resolve().parents[2]
    text = (root / "ops/aws/training/jobs/tickboot.sh").read_text()
    code = [x for x in text.split("\n") if "crosscheck2" in x and not x.startswith("#")]
    assert code and all("$J/crosscheck2-$CTHR-$CMEM" in x for x in code)
    assert _conv_settings_one_core() == ["2", "1", "10GB"]


def test_small_machine_gets_swap_before_two_converters_or_falls_back():
    root = Path(__file__).resolve().parents[2]
    text = (root / "ops/aws/training/jobs/tickboot.sh").read_text().split("\n")
    swap = next(i for i, x in enumerate(text) if "swapon $SW" in x)
    check = next(i for i, x in enumerate(text) if "X2=$J/crosscheck2" in x)
    run = next(i for i, x in enumerate(text) if "tickrun.py $D/wm2tick-v11.py" in x)
    # the swap file (or the fall-back to one converter) comes before any use of two converters
    assert swap < check < run
    block = "\n".join(text[swap : swap + 4])
    assert "NCONV=1; CTHR=$(nproc); CMEM=20GB" in block


def _pass_settings(tmp_path, value):
    """Runs the job script's pass lines with aws replaced; returns (exit code, output)."""
    root = Path(__file__).resolve().parents[2]
    text = (root / "ops/aws/training/jobs/tickboot.sh").read_text().split("\n")
    a = next(i for i, x in enumerate(text) if x.startswith("PASSV="))
    b = next(i for i, x in enumerate(text) if x.startswith("esac"))
    key = next(x for x in text if x.startswith("OLDER_TYPES_KEY="))
    stubs = tmp_path / "bin"
    stubs.mkdir(parents=True)
    f = stubs / "aws"
    f.write_text(
        "#!/bin/sh\n" + (f'printf "%s" "{value}"' if value is not None else "exit 1") + "\n"
    )
    f.chmod(0o755)
    script = (
        'say() { echo "$*"; }\nJ=x; B=s3://b; D=/data; TP=/mac/types-5y.csv\n'
        + key
        + "\n"
        + "\n".join(text[a : b + 1])
        + '\necho "$FIRST $LAST $ROOT $CLAIMS $RTP"\n'
    )
    env = {"PATH": f"{stubs}:{os.environ['PATH']}"}
    r = subprocess.run(["sh", "-c", script], env=env, capture_output=True, text=True)
    return r.returncode, r.stdout.strip()


def test_pass_switch_sets_dates_root_claims_and_type_list(tmp_path):
    assert _pass_settings(tmp_path / "a", "open\n") == (
        0,
        "2021-11-16 2026-11-19 s3://b ticks /mac/types-5y.csv",
    )
    assert _pass_settings(tmp_path / "b", "older\n") == (
        0,
        "2019-11-01 2021-11-15 s3://b/older ticks-older /mac/types-2019-11-01_2021-11-15.csv",
    )


def test_pass_switch_refuses_a_missing_or_unknown_value(tmp_path):
    for i, v in enumerate((None, "", "old", "olderx", "2")):
        rc, out = _pass_settings(tmp_path / str(i), v)
        assert rc == 1 and out.startswith("REFUSED: jobs/download/pass")


def test_runner_line_takes_everything_from_the_pass():
    root = Path(__file__).resolve().parents[2]
    text = (root / "ops/aws/training/jobs/tickboot.sh").read_text().split("\n")
    i = next(i for i, x in enumerate(text) if "tickrun.py $D/wm2tick-v11.py" in x)
    line = text[i - 1] + text[i]
    for part in (
        "TICK_TYPES=$RTP ",
        "TICK_PX=$ROOT/px ",
        "RUN_CLAIMS=$CLAIMS ",
        "$FIRST $LAST 0/1 $D/run $ROOT/tk61 ",
    ):
        assert part in line
    # the older list is fetched and checked against its pinned md5 before the runner
    j = next(i for i, x in enumerate(text) if "$OLDER_TYPES_KEY $RTP" in x)
    assert j < i and '"$OLDER_TYPES_MD5"' in text[j]


def test_closed_days_cover_the_older_range():
    import datetime

    root = Path(__file__).resolve().parents[2]
    listed = {
        x.strip()
        for x in (root / "ops/aws/training/jobs/us-equity-closed-days.txt").read_text().split("\n")
        if x.strip() and not x.startswith("#")
    }
    d, end, weekdays = datetime.date(2019, 11, 1), datetime.date(2021, 11, 15), []
    while d <= end:
        if d.weekday() < 5:
            weekdays.append(d.isoformat())
        d += datetime.timedelta(days=1)
    closed = [x for x in weekdays if x in listed]
    # the source lists 514 day files in this range (Massive listing, 2026-11-21)
    assert (len(weekdays), len(closed)) == (532, 18)
    assert all(datetime.date.fromisoformat(x).weekday() < 5 for x in listed)


def test_type_list_builder_keeps_active_and_delisted_in_range(tmp_path):
    import json

    root = Path(__file__).resolve().parents[2]
    act = [{"ticker": "AA", "type": "CS", "active": True}, {"ticker": "EE", "type": "ETF"}]
    ina = [
        {"ticker": "OLD", "type": "CS", "delisted_utc": "2019-10-31T00:00:00Z"},
        {"ticker": "MID", "type": "ADRC", "delisted_utc": "2020-10-05T00:00:00Z"},
        {"ticker": "NOTYPE", "delisted_utc": "2021-01-08T00:00:00Z"},
        {"ticker": "W", "type": "WARRANT", "delisted_utc": "2021-01-08T00:00:00Z"},
    ]
    (tmp_path / "a.jsonl").write_text("\n".join(json.dumps(x) for x in act))
    (tmp_path / "i.jsonl").write_text("\n".join(json.dumps(x) for x in ina))
    subprocess.run(
        [
            sys.executable,
            str(root / "ops/aws/training/jobs/build_types.py"),
            str(tmp_path / "a.jsonl"),
            str(tmp_path / "i.jsonl"),
            "2019-11-01",
            str(tmp_path / "o.csv"),
        ],
        check=True,
        capture_output=True,
    )
    assert (tmp_path / "o.csv").read_bytes() == b"ticker,type\r\nAA,CS\r\nMID,ADRC\r\n"


REF_MD5 = {  # the bucket's reference snapshots of 2026-11-20 the two lists were built from
    "tickers-active.jsonl": "824c4aeb7e730eea3d4aebfb7af30ed4",
    "tickers-inactive.jsonl": "bd3ceda56781ebe61d9b177ffa2e9b13",
}


def test_builder_gives_the_five_year_list_byte_for_byte(tmp_path):
    """Needs the two snapshot files (11 MB, not in the repo): TICK_REF_DIR names their folder."""
    import hashlib

    import pytest

    ref = os.environ.get("TICK_REF_DIR")
    if not ref:
        pytest.skip("TICK_REF_DIR is not set (reference snapshots live in the research bucket)")
    root = Path(__file__).resolve().parents[2]
    md5 = lambda f: hashlib.md5(Path(f).read_bytes()).hexdigest()  # noqa: E731
    files = [str(Path(ref) / x) for x in REF_MD5]
    assert [md5(x) for x in files] == list(REF_MD5.values())
    builder = str(root / "ops/aws/training/jobs/build_types.py")
    for first, want in (
        ("2021-11-16", "fd1b9f017178a8edb6d2daf6234016fc"),
        ("2019-11-01", "f0252a5219836974438f1ac3944217e9"),
    ):
        out = str(tmp_path / first)
        subprocess.run(
            [sys.executable, builder, *files, first, out], check=True, capture_output=True
        )
        assert md5(out) == want


class _ClientError(Exception):
    def __init__(self, code):
        super().__init__(code)
        self.response = {"Error": {"Code": code}}


class _FakeS3:
    """In-memory claim store with S3's conditional delete: If-Match must equal the object's
    current ETag, else 412 PreconditionFailed; a missing key is 404 NoSuchKey."""

    def __init__(self):
        self.objects = {}
        self.deletes = []
        self.puts = []  # every write the runner sent
        self.holder = {}  # key -> the "by" field of the stored claim
        # None, or the write's answer is lost: "after" it was applied, or "before" it was
        self.put_fault = None
        self.fail_reads = False

    def put_object(self, Bucket, Key, Body, IfMatch=None, IfNoneMatch=None):
        self.puts.append((Key, IfMatch))
        if self.put_fault == "before":
            raise TimeoutError("read timeout, the write never arrived")
        if IfMatch is not None and self.objects.get(Key) != IfMatch:
            raise _ClientError("PreconditionFailed")
        self.objects[Key] = f'"e{len(self.puts)}"'
        self.holder[Key] = json.loads(Body)["by"]
        if self.put_fault == "after":
            raise TimeoutError("read timeout after the write was applied")
        return {"ETag": self.objects[Key]}

    def get_object(self, Bucket, Key):
        if self.fail_reads:
            raise TimeoutError("read timeout")
        if Key not in self.objects:
            raise _ClientError("NoSuchKey")
        body = json.dumps({"by": self.holder.get(Key)}).encode()
        return {"ETag": self.objects[Key], "Body": io.BytesIO(body)}

    def delete_object(self, Bucket, Key, IfMatch):
        self.deletes.append((Bucket, Key, IfMatch))
        if Key not in self.objects:
            raise _ClientError("NoSuchKey")
        if self.objects[Key] != IfMatch:
            raise _ClientError("PreconditionFailed")
        del self.objects[Key]


def _s3_release(fake):
    """The runner's S3 cdel and its release(), compiled from the file with a fake client."""
    import ast
    import threading
    import types

    tree = ast.parse(RUNNER.read_text())
    s3_block = next(
        n for n in tree.body if isinstance(n, ast.If) and getattr(n.test, "id", "") == "S3"
    )
    picked = [n for n in s3_block.body if isinstance(n, ast.FunctionDef) and n.name == "cdel"]
    picked += [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "release"]
    said = []
    ns = {
        "s3": lambda: fake,
        "ob": "bucket",
        "CPRE": "claims/ticks",
        "botocore": types.SimpleNamespace(
            exceptions=types.SimpleNamespace(ClientError=_ClientError)
        ),
        "MINE": {},
        "L": threading.Lock(),
        "CLK": threading.Lock(),
        "say": lambda *a: said.append(" ".join(str(x) for x in a)),
    }
    exec(compile(ast.Module(body=picked, type_ignores=[]), str(RUNNER), "exec"), ns)
    return ns, said


def test_s3_release_deletes_only_the_claim_version_it_holds():
    fake = _FakeS3()
    ns, said = _s3_release(fake)
    key = "claims/ticks/2021-11-15.json"
    fake.objects[key] = '"e-mine"'
    ns["MINE"]["2021-11-15"] = '"e-mine"'
    ns["release"]("2021-11-15")
    assert key not in fake.objects and said == []
    assert fake.deletes == [("bucket", key, '"e-mine"')]


def test_s3_release_of_a_claim_taken_over_is_refused_and_left_alone():
    fake = _FakeS3()
    ns, said = _s3_release(fake)
    key = "claims/ticks/2021-11-15.json"
    fake.objects[key] = '"e-other"'  # another machine took the stale claim over
    ns["MINE"]["2021-11-15"] = '"e-mine"'
    ns["release"]("2021-11-15")
    assert fake.objects[key] == '"e-other"'
    assert fake.deletes == [("bucket", key, '"e-mine"')]  # one conditional request, no plain one
    assert len(said) == 1 and said[0].startswith("REFUSED: claim delete of 2021-11-15")
    assert ns["MINE"] == {}


def test_s3_release_of_a_claim_already_gone_is_quiet():
    fake = _FakeS3()
    ns, said = _s3_release(fake)
    ns["MINE"]["2021-11-15"] = '"e-mine"'
    ns["release"]("2021-11-15")
    assert said == [] and len(fake.deletes) == 1


def _s3_refresh_and_stop(fake):
    """The runner's S3 claim store, refresher(), release() and stop_now(), compiled from the
    file with a fake client. The refresher runs one round: its second sleep ends it."""
    import ast
    import threading
    import types

    tree = ast.parse(RUNNER.read_text())
    s3_block = next(
        n for n in tree.body if isinstance(n, ast.If) and getattr(n.test, "id", "") == "S3"
    )
    names = {"cput", "cread", "cdel"}
    picked = [n for n in s3_block.body if isinstance(n, ast.FunctionDef) and n.name in names]
    picked += [
        n
        for n in tree.body
        if isinstance(n, ast.FunctionDef) and n.name in ("release", "refresher", "stop_now")
    ]
    sleeps = []

    class _OneRound(Exception):
        pass

    def sleep(_):
        sleeps.append(1)
        if len(sleeps) > 1:
            raise _OneRound

    said = []
    ns = {
        "s3": lambda: fake,
        "ob": "bucket",
        "CPRE": "claims/ticks",
        "ME": "host-mine",
        "CLAIM_SEC": 60.0,
        "botocore": types.SimpleNamespace(
            exceptions=types.SimpleNamespace(ClientError=_ClientError)
        ),
        "json": json,
        "os": os,
        "signal": signal,
        "time": types.SimpleNamespace(sleep=sleep, time=time.time),
        "MINE": {},
        "LOST": set(),
        "PROCS": {},
        "ST": {"stop": False},
        "L": threading.Lock(),
        "CLK": threading.Lock(),
        "say": lambda *a: said.append(" ".join(str(x) for x in a)),
    }
    exec(compile(ast.Module(body=picked, type_ignores=[]), str(RUNNER), "exec"), ns)

    def one_round():
        try:
            ns["refresher"]()
        except _OneRound:
            pass

    return ns, said, one_round


KEY = "claims/ticks/2021-11-17.json"


def _held(fake, ns, holder="host-mine"):
    fake.objects[KEY] = '"e0"'
    fake.holder[KEY] = holder
    ns["MINE"]["2021-11-17"] = '"e0"'


def test_refresh_write_applied_but_answer_lost_is_still_released_on_sigterm():
    fake = _FakeS3()
    ns, said, one_round = _s3_refresh_and_stop(fake)
    _held(fake, ns)
    fake.put_fault = "after"
    one_round()  # the rewrite lands (new etag), its answer is lost
    assert fake.objects[KEY] == '"e1"' and fake.puts == [(KEY, '"e0"')]  # sent once, no resend
    assert ns["MINE"] == {"2021-11-17": '"e1"'}  # the stored etag of my own claim is adopted
    ns["stop_now"]("SIGTERM")
    assert KEY not in fake.objects  # released: no machine waits for it to go stale
    assert fake.deletes == [("bucket", KEY, '"e1"')]
    assert not any(s.startswith("REFUSED") for s in said)


def test_refresh_failure_with_another_holder_drops_the_day_without_deleting():
    fake = _FakeS3()
    ns, said, one_round = _s3_refresh_and_stop(fake)
    _held(fake, ns, holder="host-other")  # another machine took the stale claim over
    fake.put_fault = "before"
    one_round()
    assert ns["MINE"] == {} and ns["LOST"] == {"2021-11-17"}
    assert any(s.startswith("CLAIM LOST 2021-11-17") for s in said)
    ns["stop_now"]("SIGTERM")
    assert fake.objects[KEY] == '"e0"' and fake.deletes == []  # left to its holder
    assert fake.puts == [(KEY, '"e0"')]


def test_refresh_failure_with_the_claim_gone_drops_the_day():
    fake = _FakeS3()
    ns, said, one_round = _s3_refresh_and_stop(fake)
    ns["MINE"]["2021-11-17"] = '"e0"'
    fake.put_fault = "before"
    one_round()
    assert ns["MINE"] == {} and ns["LOST"] == {"2021-11-17"}


def test_refresh_failure_with_the_write_not_applied_keeps_my_claim():
    fake = _FakeS3()
    ns, said, one_round = _s3_refresh_and_stop(fake)
    _held(fake, ns)
    fake.put_fault = "before"
    one_round()
    assert ns["MINE"] == {"2021-11-17": '"e0"'} and ns["LOST"] == set()
    ns["stop_now"]("SIGTERM")
    assert KEY not in fake.objects


def test_refresh_failure_and_read_back_failure_keep_the_held_etag():
    fake = _FakeS3()
    ns, said, one_round = _s3_refresh_and_stop(fake)
    _held(fake, ns)
    fake.put_fault = "after"
    fake.fail_reads = True
    one_round()
    assert ns["MINE"] == {"2021-11-17": '"e0"'} and ns["LOST"] == set()  # as before the fix
    assert fake.puts == [(KEY, '"e0"')]  # one send, no resend
