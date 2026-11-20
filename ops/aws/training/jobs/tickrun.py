"""Fleet runner for the tick history (group tickr-download). Owner: research thread.

The FROZEN converter is used as it is, one invocation per day; this file only feeds it. Inside
one converter process a day is downloaded and then converted in sequence, so cores idle during
downloads. Here downloads run ahead in their own threads and the converters only convert (the
converter skips its download when the day's two raw files already stand in its work dir; the
runner puts them there, complete files only, moved in by rename).

Several machines share one day list:
- A day is taken by a CLAIM object, claims/<name>/<day>.json, created with a conditional write
  (If-None-Match: only one machine can create it) and refreshed while the machine works.
  A claim not refreshed for RUN_CLAIM_MIN minutes is free; it is taken over with a conditional
  overwrite (If-Match on the stale object), so only one machine can take it over.
- Only the claim holder writes a day's files. A machine that finds its claim taken kills that
  day's converter and drops the day.
- A day is DONE when the manifest of this very converter exists, the three stored tables have
  the sizes the manifest records and, with RUN_RAW, the raw-archive record exists. A machine
  that is taken away leaves nothing marked done; its days are redone in full by another.
- SIGTERM, or the file named by STOP_FILE: no new day, converters are killed, own claims are
  deleted, exit code 3 inside a minute.
- Exit code 0 ONLY when every day of the list is done or a holiday, whoever did it. While other
  machines hold days, this one waits and looks again. Exit 1: failures or a time limit.

RUN_RAW (s3://bucket/prefix; a local dir in test mode) is the one switch of the raw archive:
each downloaded raw file is also stored there in the DEEP_ARCHIVE class, its stored size is
read back, and <RUN_RAW>man/<year>/<day>.json records source and stored size and ETag. A day
whose tables are done but whose raw record is missing is downloaded and archived only.

usage: python tickrun.py <converter.py> <first date> <last date> <k/n> <base dir> <out>
  k/n: this machine takes every n-th day starting at k (0/1 = all, the claims do the sharing)
  out: s3://bucket/prefix, or a local dir in test mode
env: RUN_FETCH (parallel day downloads, default 4), RUN_CONV (parallel converters, default 2),
  RUN_AHEAD (downloaded days allowed to wait, default 4), RUN_FLOOR_GB (free disk kept,
  default 40), RUN_STOP_MIN (no new day after this many minutes; default none), RUN_LOG
  (s3://bucket/key: the log is copied there every 30 s), RUN_SKIPTEST=1 (after the first
  finished day the converter is invoked on it again and must print skip), RUN_FAKE (dir with
  <date>-trades.csv.gz and <date>-quotes.csv.gz: test mode, files are copied from there),
  RUN_MD5 (the converter must have this md5, else nothing starts), RUN_ORDER=desc (newest day
  first), RUN_CLAIM_MIN (default 20), RUN_CLAIMS (claim set name, default ticks), RUN_WAIT_SEC
  (pause before looking again at days other machines hold, default 60), RUN_RAW (see above),
  RUN_CLOSED (required: file of weekdays the exchanges were closed, one date per line; a day
  without source files is a holiday only when it stands there, else it is a failed day).
  Passed on to the converter untouched: TICK_SECRET TICK_TYPES TICK_PX TICK_THREADS TICK_MEM
  and the rest of the environment. Credentials: names only, nothing is printed.
"""

import datetime
import hashlib
import json
import os
import queue
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
import uuid

conv, first, last, part, base, out = sys.argv[1:7]
E = os.environ.get
NF = int(E("RUN_FETCH", "4"))
NC = int(E("RUN_CONV", "2"))
AHEAD = int(E("RUN_AHEAD", "4"))
FLOOR = float(E("RUN_FLOOR_GB", "40"))
STOPMIN = float(E("RUN_STOP_MIN", "0"))
FAKE = E("RUN_FAKE")
LOGTO = E("RUN_LOG")
RAWTO = E("RUN_RAW")
CLAIM_SEC = float(E("RUN_CLAIM_MIN", "20")) * 60
ME = f"{socket.gethostname()}-{uuid.uuid4().hex[:8]}"
STOPF = E("STOP_FILE")
SEAL = ("2026-07-01", "2026-08-14")
HARD_LAST = "2026-11-19"
T0 = time.time()
L = threading.Lock()
os.makedirs(base, exist_ok=True)
RAW = os.path.join(base, "raw")
os.makedirs(RAW, exist_ok=True)
LOG = os.path.join(base, "tickrun.log")
MD5 = hashlib.md5(open(conv, "rb").read()).hexdigest()


def say(*a):
    with L:
        s = time.strftime("%H:%M:%S ", time.gmtime()) + " ".join(str(x) for x in a)
        print(s, flush=True)
        open(LOG, "a").write(s + "\n")


if E("RUN_MD5") and E("RUN_MD5") != MD5:
    say("REFUSED: converter md5", MD5, "is not the expected one")
    sys.exit(1)
if not E("RUN_CLOSED") or not os.path.exists(E("RUN_CLOSED")):
    say("REFUSED: RUN_CLOSED must name the file of closed days (us-equity-closed-days.txt)")
    sys.exit(1)
CLOSED = {x.strip() for x in open(E("RUN_CLOSED")) if x.strip() and not x.lstrip().startswith("#")}
if not FAKE and not out.startswith("s3://"):
    say("REFUSED: a local output dir is test mode and needs RUN_FAKE")
    sys.exit(1)
k, n = [int(x) for x in part.split("/")]
d = datetime.date.fromisoformat(first)
end = datetime.date.fromisoformat(min(last, HARD_LAST))
days = []
while d <= end:
    if d.weekday() < 5 and not (SEAL[0] <= d.isoformat() <= SEAL[1]):
        days.append(d.isoformat())
    d += datetime.timedelta(days=1)
days = days[k::n]
S3 = out.startswith("s3://")
px = E("TICK_PX")
if E("RUN_ORDER") == "desc":
    days.reverse()
ALL_DAYS = list(days)
if S3:
    import boto3
    import botocore

    def s3():
        return boto3.client("s3")

    pxb, _, pxp = px[5:].partition("/")
    mpre = (
        pxp.rstrip("/")[:-2] + "manifest"
        if pxp.rstrip("/").endswith("px")
        else pxp.rstrip("/") + "-manifest"
    )
    ob = out[5:].partition("/")[0]
    CPRE = "claims/" + E("RUN_CLAIMS", "ticks")
    tkp = out[5:].partition("/")[2].rstrip("/")
    s1p = (
        pxp.rstrip("/")[:-2] + "s10" if pxp.rstrip("/").endswith("px") else pxp.rstrip("/") + "-s10"
    )

    # the runner's own test: the manifest of this very converter exists AND the three stored
    # tables have the sizes it records. (The converter stores the tk table AFTER the manifest:
    # a machine taken away between the two leaves a manifest without its table; such a day is
    # not done.)
    def tabled(dt):
        try:
            man = json.loads(
                s3().get_object(Bucket=pxb, Key=f"{mpre}/{dt[:4]}/{dt}.json")["Body"].read()
            )
        except botocore.exceptions.ClientError as e:
            if e.response["Error"]["Code"] in ("404", "NoSuchKey", "NotFound"):
                return False
            raise
        if man.get("script_md5") != MD5:
            return False
        for nm, b_, p_ in (("tk", ob, tkp), ("px", pxb, pxp.rstrip("/")), ("s10", pxb, s1p)):
            try:
                h = s3().head_object(Bucket=b_, Key=f"{p_}/{dt[:4]}/{dt}.parquet")
            except botocore.exceptions.ClientError as e:
                if e.response["Error"]["Code"] in ("404", "NoSuchKey", "NotFound"):
                    return False
                raise
            if h["ContentLength"] != (man.get("files", {}).get(nm) or {}).get("bytes"):
                return False
        return True

    if RAWTO:
        rwb, _, rwp = RAWTO[5:].partition("/")
        rwp = rwp.rstrip("/")

    def rawed(dt):
        try:
            s3().head_object(Bucket=rwb, Key=f"{rwp}man/{dt[:4]}/{dt}.json")
            return True
        except botocore.exceptions.ClientError as e:
            if e.response["Error"]["Code"] in ("404", "NoSuchKey", "NotFound"):
                return False
            raise

    def rawput(
        fn, dt, kind
    ):  # store, then read the stored size back; a different size is a failure of the day
        key = f"{rwp}/{kind}/{dt[:4]}/{dt}.csv.gz"
        s3().upload_file(
            fn,
            rwb,
            key,
            ExtraArgs={"StorageClass": "DEEP_ARCHIVE"},
            Config=boto3.s3.transfer.TransferConfig(
                multipart_chunksize=256 * 1024 * 1024, max_concurrency=8
            ),
        )
        h = s3().head_object(Bucket=rwb, Key=key)
        if h["ContentLength"] != os.path.getsize(fn) or h.get("StorageClass") != "DEEP_ARCHIVE":
            raise RuntimeError(
                f"stored raw {key}: size {h['ContentLength']} class {h.get('StorageClass')}, "
                f"local size {os.path.getsize(fn)}"
            )
        return {"key": key, "bytes": h["ContentLength"], "etag": h["ETag"].strip('"')}

    def rawman(dt, rec):
        s3().put_object(
            Bucket=rwb, Key=f"{rwp}man/{dt[:4]}/{dt}.json", Body=json.dumps(rec).encode()
        )

    # claims. cput: create only if absent (etag None) or overwrite only if the object still
    # has this etag. Returns the new etag, or None when another machine was first.
    def cput(dt, etag):
        kw = {"IfMatch": etag} if etag else {"IfNoneMatch": "*"}
        try:
            return s3().put_object(
                Bucket=ob,
                Key=f"{CPRE}/{dt}.json",
                Body=json.dumps({"by": ME, "utc": time.time()}).encode(),
                **kw,
            )["ETag"]
        except botocore.exceptions.ClientError as e:
            if e.response["Error"]["Code"] in (
                "PreconditionFailed",
                "412",
                "ConditionalRequestConflict",
                "409",
                "NoSuchKey",
                "404",
            ):
                return None
            raise

    def cget(dt):  # (etag, age in seconds by the store's own clock) or None
        try:
            h = s3().head_object(Bucket=ob, Key=f"{CPRE}/{dt}.json")
            return h["ETag"], (
                datetime.datetime.now(datetime.UTC) - h["LastModified"]
            ).total_seconds()
        except botocore.exceptions.ClientError as e:
            if e.response["Error"]["Code"] in ("404", "NoSuchKey", "NotFound"):
                return None
            raise

    def cdel(dt):
        try:
            s3().delete_object(Bucket=ob, Key=f"{CPRE}/{dt}.json")
        except Exception as e:
            print("claim delete failed", dt, type(e).__name__, flush=True)
else:
    # local test mode: the same rules on files (exclusive create; the etag is the file's
    # modification time)
    CD = os.path.join(out, "claims")
    os.makedirs(CD, exist_ok=True)

    def tabled(dt):
        return os.path.exists(os.path.join(out, dt[:4], dt + ".parquet"))

    def rawed(dt):
        return os.path.exists(os.path.join(RAWTO + "man", dt[:4], dt + ".json"))

    def rawput(fn, dt, kind):
        o = os.path.join(RAWTO, kind, dt[:4], dt + ".csv.gz")
        os.makedirs(os.path.dirname(o), exist_ok=True)
        shutil.copy(fn, o)
        if os.path.getsize(o) != os.path.getsize(fn):
            raise RuntimeError("stored raw size differs")
        return {
            "key": o,
            "bytes": os.path.getsize(o),
            "etag": hashlib.md5(open(o, "rb").read()).hexdigest(),
        }

    def rawman(dt, rec):
        o = os.path.join(RAWTO + "man", dt[:4], dt + ".json")
        os.makedirs(os.path.dirname(o), exist_ok=True)
        open(o, "w").write(json.dumps(rec))

    CL = threading.Lock()

    def cput(dt, etag):
        f = os.path.join(CD, dt + ".json")
        with CL:
            try:
                if etag is None:
                    fd = os.open(f, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                    os.write(fd, ME.encode())
                    os.close(fd)
                else:
                    if str(os.stat(f).st_mtime_ns) != etag:
                        return None
                    open(f, "w").write(ME)
                return str(os.stat(f).st_mtime_ns)
            except (FileExistsError, FileNotFoundError):
                return None

    def cget(dt):
        try:
            st = os.stat(os.path.join(CD, dt + ".json"))
            return str(st.st_mtime_ns), time.time() - st.st_mtime
        except FileNotFoundError:
            return None

    def cdel(dt):
        try:
            os.remove(os.path.join(CD, dt + ".json"))
        except OSError:
            pass


def done(dt):
    return tabled(dt) and (not RAWTO or rawed(dt))


# day -> etag of my claim; days whose claim another machine took (only the claim holder may
# write a day's files)
MINE = {}
LOST = set()
HOLIDAYS = set()  # days with no source file, found by this run
DONE_HERE = set()  # days this run converted or archived with success


def claim(dt):
    e = cput(dt, None)
    if e is None:
        c = cget(dt)
        if c is None:
            e = cput(dt, None)
        elif c[1] > CLAIM_SEC:
            e = cput(dt, c[0])
            if e:
                say(
                    "took over the stale claim of", dt, "(not refreshed for %.0f min)" % (c[1] / 60)
                )
    if e:
        with L:
            MINE[dt] = e
            LOST.discard(dt)  # claimed again (the other holder died): an ordinary day again
    return bool(e)


def release(dt):
    """Give a day back. The claim object is deleted only while it still carries my mark: a
    machine that was slow and lost the day must not delete the new holder's claim."""
    with L:
        had = MINE.pop(dt, None)
    if not had:
        return
    try:
        c = cget(dt)
    except Exception as e:
        print("claim read failed", dt, type(e).__name__, flush=True)
        return
    if c and c[0] == had:
        cdel(dt)


def refresher():
    while not ST.get("end"):
        time.sleep(min(CLAIM_SEC / 4, 300))
        with L:
            mine = list(MINE.items())
        for dt, e in mine:
            try:
                e2 = cput(dt, e)
            except Exception as x:
                print("claim refresh failed", dt, type(x).__name__, flush=True)
                continue
            lost = False
            with L:
                if dt in MINE:
                    if e2:
                        MINE[dt] = e2
                    else:
                        MINE.pop(dt, None)
                        LOST.add(dt)
                        lost = True
                        pr = [p for p, d_ in PROCS.values() if d_ == dt]
            if lost:
                for p in pr:
                    try:
                        os.killpg(p.pid, signal.SIGKILL)
                    except Exception:
                        pass
                say(
                    "CLAIM LOST",
                    dt,
                    "(another machine holds it now: this machine drops the day and writes nothing "
                    f"more for it; converters killed: {len(pr)})",
                )


def src():
    """Massive flat-file client. Keys come from the secret named in TICK_SECRET (machine role)
    or from the two environment variables; they are never printed or stored."""
    if E("TICK_SECRET"):
        sec = json.loads(
            boto3.client("secretsmanager").get_secret_value(SecretId=E("TICK_SECRET"))[
                "SecretString"
            ]
        )
        a, b = sec["MASSIVE_S3_ACCESS_KEY_ID"], sec["MASSIVE_S3_SECRET_ACCESS_KEY"]
    else:
        a, b = os.environ["MASSIVE_S3_ACCESS_KEY_ID"], os.environ["MASSIVE_S3_SECRET_ACCESS_KEY"]
    return boto3.client(
        "s3",
        endpoint_url="https://files.massive.com",
        aws_access_key_id=a,
        aws_secret_access_key=b,
        config=botocore.config.Config(signature_version="s3v4"),
    )


PROCS = {}
LATER = []  # LATER: days another machine holds, looked at again at the end
ST = dict(
    next=0,
    fetching=0,
    ready=0,
    converting=0,
    done=0,
    failed=0,
    failrow=0,
    skipped=0,
    holiday=0,
    others=0,
    archived=0,
    lost=0,
    gb=0.0,
    stop=False,
    tested=False,
)
READY = queue.Queue()
DL = []  # DL: (end time, bytes, seconds) of every downloaded file


def free():
    return shutil.disk_usage(base).free / 1e9


def halted():
    # Two failures in a row pause the run for good: a later good day must not un-pause it
    # (the fetchers have ended by then and the converters would wait for ever).
    if ST["failrow"] >= 2:
        ST["paused"] = True
    return (
        ST["stop"]
        or ST.get("paused")
        or (STOPF and os.path.exists(STOPF))
        or (STOPMIN and time.time() - T0 > STOPMIN * 60)
    )


def source_has(c, dt):
    """Which of the day's two files the source holds (test mode: files in RUN_FAKE)."""
    out_ = {}
    for kind in ("trades", "quotes"):
        if FAKE:
            out_[kind] = os.path.exists(os.path.join(FAKE, f"{dt}-{kind}.csv.gz"))
            continue
        try:
            c.head_object(
                Bucket="flatfiles", Key=f"us_stocks_sip/{kind}_v1/{dt[:4]}/{dt[5:7]}/{dt}.csv.gz"
            )
            out_[kind] = True
        except botocore.exceptions.ClientError as e:
            if e.response["Error"]["Code"] not in ("404", "NoSuchKey", "NotFound"):
                raise
            out_[kind] = False
    return out_


def fetcher(i):
    c = None if FAKE else src()
    while True:
        with L:
            if halted() or ST["next"] >= len(days):
                return
            if ST["fetching"] + ST["ready"] >= AHEAD + NF or free() < FLOOR:
                dt = None
            else:
                dt = days[ST["next"]]
                ST["next"] += 1
                ST["fetching"] += 1
        if dt is None:
            time.sleep(2)
            continue
        try:
            if done(dt):
                with L:
                    ST["skipped"] += 1
                    ST["fetching"] -= 1
                say("skip", dt, "(done by this build)")
                continue
            if not claim(dt):
                with L:
                    ST["others"] += 1
                    ST["fetching"] -= 1
                    LATER.append(dt)
                continue
            if done(dt):  # finished by another machine between the two looks
                release(dt)
                with L:
                    ST["skipped"] += 1
                    ST["fetching"] -= 1
                continue
            tot = 0
            t0 = time.time()
            rec = {"date": dt, "by": ME, "files": {}}
            there = source_has(c, dt)
            # A holiday is a day the calendar file lists as closed AND that has no file. Every
            # other combination is a failed day with its line: a session with a file missing
            # (not published, a moved key), or files on a day listed as closed.
            if dt in CLOSED and not any(there.values()):
                release(dt)
                with L:
                    ST["holiday"] += 1
                    HOLIDAYS.add(dt)
                    ST["fetching"] -= 1
                say("no file", dt, "(holiday: listed as closed, no trades and no quotes file)")
                continue
            if dt in CLOSED:
                raise RuntimeError(f"listed as closed but the source has files: {there}")
            if not all(there.values()):
                raise RuntimeError(f"a session, and not complete at the source: {there}")
            for kind in ("trades", "quotes"):
                fn = os.path.join(RAW, f"{dt}-{kind}.csv.gz")
                tmp = fn + ".part"
                t1 = time.time()
                if FAKE:
                    f0 = os.path.join(FAKE, f"{dt}-{kind}.csv.gz")
                    shutil.copy(f0, tmp)
                else:
                    sk = f"us_stocks_sip/{kind}_v1/{dt[:4]}/{dt[5:7]}/{dt}.csv.gz"
                    h0 = c.head_object(Bucket="flatfiles", Key=sk)
                    want = h0["ContentLength"]
                    rec["files"][kind] = {
                        "source_key": sk,
                        "source_etag": h0["ETag"].strip('"'),
                        "source_bytes": want,
                    }
                    c.download_file("flatfiles", sk, tmp)
                    if os.path.getsize(tmp) != want:
                        raise RuntimeError(
                            f"size of {sk} is {os.path.getsize(tmp)}, the object has {want}"
                        )
                # a file is renamed (made visible) only when it is complete
                os.rename(tmp, fn)
                b = os.path.getsize(fn)
                tot += b
                with L:
                    DL.append((time.time(), b, time.time() - t1))
            if RAWTO and not rawed(dt):
                t2 = time.time()
                for kind in ("trades", "quotes"):
                    rec["files"].setdefault(kind, {})["stored"] = rawput(
                        os.path.join(RAW, f"{dt}-{kind}.csv.gz"), dt, kind
                    )
                rec["made_utc"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
                rawman(dt, rec)
                say("raw archived", dt, f"GB {tot / 1e9:.2f} sec {time.time() - t2:.0f}")
            if tabled(
                dt
            ):  # tables were done before (by the Mac): this pass only archived the raw files
                for kind in ("trades", "quotes"):
                    os.remove(os.path.join(RAW, f"{dt}-{kind}.csv.gz"))
                release(dt)
                with L:
                    ST["fetching"] -= 1
                    ST["archived"] += 1
                    ST["gb"] += tot / 1e9
                continue
            with L:
                ST["fetching"] -= 1
                ST["ready"] += 1
                ST["gb"] += tot / 1e9
            say(
                "fetched",
                dt,
                f"GB {tot / 1e9:.2f} sec {time.time() - t0:.0f} "
                f"MB/s {tot / 1e6 / max(time.time() - t0, 1e-9):.1f} (stream {i})",
            )
            READY.put((dt, tot))
        except Exception as e:
            release(dt)
            for kind in ("trades", "quotes"):
                for x in ("", ".part"):
                    try:
                        os.remove(os.path.join(RAW, f"{dt}-{kind}.csv.gz{x}"))
                    except OSError:
                        pass
            with L:
                ST["fetching"] -= 1
                ST["failed"] += 1
                ST["failrow"] += 1
            say("FETCH FAILED", dt, type(e).__name__, str(e)[:300])


def run1(w, dt, wd):
    mac = sys.platform == "darwin"
    tm = (
        ["/usr/bin/time", "-l" if mac else "-v"] if os.path.exists("/usr/bin/time") else []
    )  # peak memory: Linux prints kilobytes after the words, macOS prints bytes before them
    p = subprocess.Popen(
        tm + [sys.executable, conv, dt, dt, wd, out],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    with L:
        PROCS[w] = (p, dt)
    so, se = p.communicate()
    o = so + se
    with L:
        PROCS.pop(w, None)
    rss = [x for x in o.split("\n") if "maximum resident set size" in x.lower()]
    lines = [x for x in so.split("\n") if x.strip()]
    gbs = (
        (int(rss[0].split()[0]) / 1e9 if mac else int(rss[0].split()[-1]) / 1e6)
        if rss
        else float("nan")
    )
    return p.returncode, lines, gbs, o


def converter(w):
    wd = os.path.join(base, f"w{w}")
    os.makedirs(wd, exist_ok=True)
    while True:
        try:
            dt, tot = READY.get(timeout=2)
        except queue.Empty:
            with L:
                if (halted() or ST["next"] >= len(days)) and ST["fetching"] == 0 and READY.empty():
                    return
            continue
        with L:
            ST["ready"] -= 1
            ST["converting"] += 1
        for kind in ("trades", "quotes"):
            os.rename(
                os.path.join(RAW, f"{dt}-{kind}.csv.gz"),
                os.path.join(wd, f"{dt}-{kind}.csv.gz"),
            )
        t0 = time.time()
        if dt in LOST:
            rc, lines, rss, o = 1, [], float("nan"), ""
        else:
            rc, lines, rss, o = run1(w, dt, wd)
        if dt in LOST:  # not a failure of the day: another machine does it
            for f in os.listdir(wd):
                if f.startswith(dt):
                    os.remove(os.path.join(wd, f))
            release(dt)  # holds nothing after a real loss; never leaves a claim behind
            with L:
                ST["converting"] -= 1
                ST["lost"] += 1
                LATER.append(dt)
            continue
        good = rc == 0 and any(x.startswith(("day ", "redo ")) for x in lines)
        release(dt)
        if ST["stop"]:  # killed by the stop: not a failure of the day
            for f in os.listdir(wd):
                if f.startswith(dt):
                    os.remove(os.path.join(wd, f))
            return
        with L:
            ST["converting"] -= 1
            if good:
                DONE_HERE.add(dt)
                ST["done"] += 1
                ST["failrow"] = 0
            else:
                ST["failed"] += 1
                ST["failrow"] += 1
        say(
            f"converter {w}",
            "OK" if good else f"FAILED rc={rc}",
            f"wall {time.time() - t0:.0f} s",
            f"sec per raw GB {(time.time() - t0) / max(tot / 1e9, 1e-9):.0f}",
            f"peak GB {rss:.1f} |",
            " | ".join(lines[-3:])[:700],
        )
        if not good:
            say("  output tail:", o[-1200:].replace("\n", " / "))
            for f in os.listdir(wd):
                if f.startswith(dt):
                    os.remove(os.path.join(wd, f))
        elif E("RUN_SKIPTEST") == "1":
            with L:
                first_ = not ST["tested"]
                ST["tested"] = True
            if first_:
                rc2, l2, _, _ = run1(w, dt, wd)
                say(
                    "SKIP TEST on",
                    dt,
                    f": rc={rc2}",
                    "|",
                    " | ".join(l2)[:400],
                    "| PASS"
                    if rc2 == 0 and any(x.strip() == "skip " + dt for x in l2)
                    else "| FAIL",
                )


def monitor():
    while not ST.get("end"):
        time.sleep(30)
        now = time.time()
        if STOPF and os.path.exists(STOPF) and not ST["stop"]:
            stop_now("stop file")
        with L:
            rec = [(b, s) for t, b, s in DL if now - t <= 300]
            a = dict(ST)
        try:
            load = open("/proc/loadavg").read().split()[0]
            mem = [x for x in open("/proc/meminfo") if x.startswith(("MemTotal", "MemAvailable"))]
            used = (int(mem[0].split()[1]) - int(mem[1].split()[1])) / 1e6
        except Exception:
            load = "?"
            used = float("nan")
        mbs = a["gb"] * 1e3 / max(now - T0, 1)
        per_file = sum(b for b, s in rec) / 1e6 / max(sum(s for b, s in rec), 1e-9)
        say(
            f"STATE min {(now - T0) / 60:.1f} load {load} of {os.cpu_count()}",
            f"used GB {used:.1f} free disk GB {free():.0f}",
            f"| fetching {a['fetching']} ready {a['ready']} converting {a['converting']}",
            f"done {a['done']} failed {a['failed']} skipped {a['skipped']}",
            f"holidays {a['holiday']} held by others {a['others']} raw-only {a['archived']}",
            f"dropped {a['lost']}",
            f"| downloaded GB {a['gb']:.1f} = {mbs:.0f} MB/s since start,",
            f"files finished in the last 5 min: {len(rec)}, {per_file:.0f} MB/s per file",
        )
        if a["failrow"] >= 2:
            say("PAUSED: 2 failures in a row; nothing new is started")
        if LOGTO:
            try:
                b_, _, k_ = LOGTO[5:].partition("/")
                s3().upload_file(LOG, b_, k_)
            except Exception as e:
                print("log upload failed", type(e).__name__, flush=True)


# no new day; converters are killed with their process groups; own claims are deleted so another
# machine takes the days at once
def stop_now(why):
    ST["stop"] = True
    say("STOP:", why)
    with L:
        ps = [p for p, _ in PROCS.values()]
        mine = list(MINE)
    for p in ps:
        try:
            os.killpg(p.pid, signal.SIGKILL)
        except Exception:
            pass
    for dt in mine:
        release(dt)
    say(f"STOP: {len(ps)} converters killed, {len(mine)} claims released")


signal.signal(
    signal.SIGTERM, lambda *a: threading.Thread(target=stop_now, args=("SIGTERM",)).start()
)
say(
    "START converter md5",
    MD5,
    "days",
    len(days),
    days[:1],
    days[-1:],
    "part",
    part,
    "fetch",
    NF,
    "conv",
    NC,
    "ahead",
    AHEAD,
    "threads",
    E("TICK_THREADS"),
    "mem",
    E("TICK_MEM"),
    "cores",
    os.cpu_count(),
)
th = [threading.Thread(target=fetcher, args=(i,)) for i in range(NF)] + [
    threading.Thread(target=converter, args=(i,)) for i in range(NC)
]
mon = threading.Thread(target=monitor, daemon=True)
mon.start()
threading.Thread(target=refresher, daemon=True).start()
while True:
    [x.start() for x in th]
    [x.join() for x in th]
    with L:
        LATER.clear()
    if halted():
        later = ["not checked"]
        break
    # Not the counters of this run: every day of the list that is not a holiday is read again
    # in the store. Only a list with nothing left lets the runner end with exit code 0.
    later = [x for x in ALL_DAYS if x not in HOLIDAYS and not done(x)]
    bad = [x for x in later if x in DONE_HERE]
    if bad:  # the converter said "done" and the store does not show the day: never loop on it
        ST["failed"] += len(bad)
        say("FAILED: converted here but not done in the store:", " ".join(bad[:10]))
    if not later or ST["failed"]:
        break
    say("waiting:", len(later), "days are not done yet (held by other machines); looking again")
    time.sleep(
        float(E("RUN_WAIT_SEC", "60"))
    )  # their claims end by a finished day or by going stale
    days = later
    ST["next"] = 0
    th = [threading.Thread(target=fetcher, args=(i,)) for i in range(NF)] + [
        threading.Thread(target=converter, args=(i,)) for i in range(NC)
    ]
el = time.time() - T0
say(
    f"END min {el / 60:.1f} done {ST['done']} failed {ST['failed']} skipped {ST['skipped']}",
    f"holidays {ST['holiday']} raw-only {ST['archived']} dropped {ST['lost']}",
    f"| downloaded GB {ST['gb']:.1f} | days per hour {ST['done'] * 3600 / el:.1f}",
    f"| raw GB per hour {ST['gb'] * 3600 / el:.0f}",
)
ST["end"] = True
if LOGTO:
    try:
        b_, _, k_ = LOGTO[5:].partition("/")
        s3().upload_file(LOG, b_, k_)
    except Exception:
        pass
with L:
    mine = list(MINE)
for dt in mine:
    release(dt)
rc = (
    3
    if ST["stop"] or (STOPF and os.path.exists(STOPF))
    else 0
    if ST["failed"] == 0 and not halted() and not later
    else 1
)
say(
    "EXIT",
    rc,
    "(0 = every day of the list is done or a holiday; 3 = stopped on request;",
    "1 = failures or a time limit)",
)
sys.exit(rc)
