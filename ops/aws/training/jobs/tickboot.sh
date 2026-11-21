#!/bin/bash
# Job script of the auto-restart group tickr-download (fetched by the group's wrapper as scripts/tickboot.sh). Owner: research thread.
# WHOLE tick history with the FROZEN converter, fed by the fleet runner tickrun.py (same folder in the repo) (claims: several machines share one day list; a machine that is taken away loses only its days in flight).
# Contract with the wrapper: resumes from S3 (a day is done by its manifest); on SIGTERM stops inside a minute; exit 0 ONLY when every day is done.
# Settings are objects under s3://<bucket>/jobs/download/ written by the research thread before the start:
#   raw        OFF unless this object says "on". The owner's recorded decision (2026-11-20) is NOT to keep raw files; the research thread writes "on"
#              only after the owner chooses the archive option on the launch card. "on" = every raw day file is also stored under raw/ in the
#              DEEP_ARCHIVE class with a record under rawman/. Off = nothing is written to raw/ or rawman/, raw files are deleted after conversion.
#   duckdb     the engine version the Mac converts with (same version here, or the machine refuses)
#   runner.md5 the md5 of the reviewed tickrun.py (a different file in the bucket: the machine refuses)
#   machines   how many machines the group gets after a PASS (one digit, default 4; the group's own maximum still holds)
#   conv       converters per machine: 1 unless this object says 2 (see "Converters per machine" below)
#   crosscheck2-<threads>-<mem> written HERE: the result of the two-converter comparison for these settings (two Mac days converted at the same time)
#   crosscheck written HERE: the result of the Mac-against-cloud comparison of one day. The group starts with ONE machine; it compares first.
#              "PASS ..." raises the group and lets every machine work; "FAIL ..." stops everything (until := 0, desired := 0).
# Nothing here reads, prints or stores a credential: the Massive keys are read by the converter and the runner from the secret named in TICK_SECRET through the machine's role.
set -u
export HOME=/root PATH=/usr/local/bin:/root/.local/bin:$PATH AWS_DEFAULT_REGION=us-east-1
BK=tickr-research-407384449055; B=s3://$BK; J=$B/jobs/download; D=${TICK_DIR:-/data}; mkdir -p $D && cd $D
CONV_MD5=c1c1ead1751ba765f494af79235bddd7          # frozen converter (cand-wm2tick11c4d.py)
CMP_MD5=b4870fbe830c1fdc0e43d279ae638d11           # cmpday.py (the adopted comparison rule)
TYPES_MD5=fd1b9f017178a8edb6d2daf6234016fc         # types-5y.csv, the Mac's list
XDAY=2026-11-16                                    # the day compared with the Mac's files
XDAY2=2026-11-17                                   # the second day of the two-converter comparison (converted at the same time as XDAY)
TP=/Users/valentynd/Development/Trading/.scratch/massive-tools/research/types-5y.csv   # the SAME path string as on the Mac: the converter's skip test compares option TICK_TYPES literally
IID=$(T=$(curl -s -m 2 -X PUT http://169.254.169.254/latest/api/token -H 'X-aws-ec2-metadata-token-ttl-seconds: 60'); curl -s -m 2 -H "X-aws-ec2-metadata-token: $T" http://169.254.169.254/latest/meta-data/instance-id); IID=${IID:-unknown-$$}
LOG=$D/boot-$IID.log; say() { echo "$(date -u +%FT%TZ) $IID $*" >> $LOG; aws s3 cp $LOG $J/logs/boot-$IID.log --only-show-errors; }
stop_fleet() { say "FLEET STOP: $*"; echo 0 | aws s3 cp - $J/until --only-show-errors; aws autoscaling set-desired-capacity --auto-scaling-group-name tickr-download --desired-capacity 0 || true; }
trap ':' TERM   # the runner gets the same signal (process group) and ends by itself; this shell only waits for it

DV=$(aws s3 cp $J/duckdb - 2>/dev/null | tr -dc '0-9.'); [ -n "$DV" ] || { say "REFUSED: no engine version in jobs/download/duckdb"; exit 1; }
RUN_MD5_WANT=$(aws s3 cp $J/runner.md5 - 2>/dev/null | tr -dc '0-9a-f' | head -c 32); [ ${#RUN_MD5_WANT} -eq 32 ] || { say "REFUSED: no runner md5 in jobs/download/runner.md5"; exit 1; }
[ -x $D/v/bin/python ] || { uv venv $D/v --python 3.12 && uv pip install --python $D/v/bin/python boto3 "duckdb==$DV"; } >/dev/null 2>&1 || { say "REFUSED: python environment failed"; exit 1; }
PY=$D/v/bin/python
[ "$($PY -c 'import duckdb; print(duckdb.__version__)')" = "$DV" ] || { say "REFUSED: the engine version is not $DV"; exit 1; }
aws s3 cp $B/scripts/mac-split/cand-wm2tick11c4d.py $D/wm2tick-v11.py --only-show-errors && aws s3 cp $B/scripts/fleet/tickrun.py $D/tickrun.py --only-show-errors && aws s3 cp $B/scripts/ramp/cmpday.py $D/cmpday.py --only-show-errors && aws s3 cp $B/scripts/fleet/us-equity-closed-days.txt $D/closed-days.txt --only-show-errors || { say "REFUSED: a script could not be fetched"; exit 1; }
mkdir -p "$(dirname $TP)"; aws s3 cp $B/reference/types-5y.csv $TP --only-show-errors || { say "REFUSED: type list could not be fetched"; exit 1; }
m() { md5sum "$1" | cut -c1-32; }
say "type $(curl -s -m 2 -H "X-aws-ec2-metadata-token: $(curl -s -m 2 -X PUT http://169.254.169.254/latest/api/token -H 'X-aws-ec2-metadata-token-ttl-seconds: 60')" http://169.254.169.254/latest/meta-data/instance-type) cores $(nproc) mem $(free -g | awk 'NR==2{print $2}') GB disk free $(df -h $D | awk 'NR==2{print $4}') converter $(m $D/wm2tick-v11.py) runner $(m $D/tickrun.py) cmpday $(m $D/cmpday.py) types $(m $TP) duckdb $($PY -c 'import duckdb; print(duckdb.__version__)') python $($PY -V 2>&1)"
[ "$(m $D/wm2tick-v11.py)" = "$CONV_MD5" ] && [ "$(m $D/tickrun.py)" = "$RUN_MD5_WANT" ] && [ "$(m $D/cmpday.py)" = "$CMP_MD5" ] && [ "$(m $TP)" = "$TYPES_MD5" ] || { say "REFUSED: a file is not the frozen one"; exit 1; }
export TICK_SECRET=tickr-research/massive-flatfiles TICK_TYPES=$TP

# --- Mac against cloud, one day, BEFORE anything else. The group is started with ONE machine. That machine converts one day into fleet-test/ and
# compares it with the Mac's files of the same day. PASS: it raises the group to jobs/download/machines (default 4) and goes on to its own work.
# FAIL: until := 0, desired := 0; nothing but fleet-test/ was written. A machine never starts the runner without a PASS.
# The comparison is taken by a claim object. A machine that finds the claim of another waits for the result; a claim older than 45 minutes without
# a result (its machine was taken away) is taken over.
want_machines() { N=$(aws s3 cp $J/machines - 2>/dev/null | tr -dc '0-9' | head -c 1); echo "${N:-4}"; }
take_check() { $PY - <<PYEOF
import boto3, botocore, datetime, sys
s3 = boto3.client('s3'); K = 'jobs/download/crosscheck.claim'
def put(**kw):
    try: s3.put_object(Bucket='$BK', Key=K, Body=b'$IID', **kw); return True
    except botocore.exceptions.ClientError as e:
        if e.response['Error']['Code'] in ('PreconditionFailed', 'ConditionalRequestConflict', 'NoSuchKey'): return False
        raise
if put(IfNoneMatch='*'): sys.exit(0)
try: h = s3.head_object(Bucket='$BK', Key=K)
except botocore.exceptions.ClientError: sys.exit(0 if put(IfNoneMatch='*') else 1)
age = (datetime.datetime.now(datetime.timezone.utc) - h['LastModified']).total_seconds()
sys.exit(0 if age > 2700 and put(IfMatch=h['ETag']) else 1)
PYEOF
}
W0=$(date +%s)
while :; do
  R=$(aws s3 cp $J/crosscheck - 2>/dev/null | head -c 4)
  [ "$R" = PASS ] && break
  if [ "$R" = FAIL ]; then stop_fleet "an earlier comparison failed"; exit 1; fi
  [ -e /work/STOP ] && { say "stopped while waiting for the comparison"; exit 3; }
  if take_check; then
    T0=$(date +%s); rm -rf $D/wx $D/cmp; mkdir -p $D/wx $D/cmp/mac $D/cmp/aws; say "comparison of $XDAY with the Mac: started here"
    TICK_PX=$B/fleet-test/px TICK_THREADS=$(nproc) TICK_MEM=20GB $PY -W ignore $D/wm2tick-v11.py $XDAY $XDAY $D/wx $B/fleet-test/tk61 > $D/cross.out 2>&1; RC=$?
    say "comparison day converted rc=$RC in $(( $(date +%s) - T0 )) s: $(grep -h '^day \|^redo \|^skip \|FAILED\|REFUSED' $D/cross.out | tr '\n' ' ' | cut -c1-400)"
    [ -e /work/STOP ] && { say "comparison interrupted by a stop"; aws s3 rm $J/crosscheck.claim --only-show-errors; exit 3; }
    OK=1
    for f in tk61:tk px:px s10:s10; do aws s3 cp $B/${f%%:*}/2026/$XDAY.parquet $D/cmp/mac/${f##*:}.parquet --only-show-errors && aws s3 cp $B/fleet-test/${f%%:*}/2026/$XDAY.parquet $D/cmp/aws/${f##*:}.parquet --only-show-errors || OK=0; done
    if [ $RC -eq 0 ] && [ $OK -eq 1 ] && $PY $D/cmpday.py $D/cmp/mac $D/cmp/aws > $D/cmp.out 2>&1; then
      cat $D/cmp.out >> $LOG; echo "PASS $IID $(date -u +%FT%TZ)" | aws s3 cp - $J/crosscheck --only-show-errors
      # the deadline is read again just before the raise: a group is never raised after its deadline (or after a stop wrote 0 there)
      U=$(aws s3 cp $J/until - 2>/dev/null | tr -dc 0-9); M=$(want_machines)
      if [ -n "$U" ] && [ "$(date +%s)" -lt "$U" ]; then
        say "comparison PASS; group goes to $M machines"
        aws autoscaling set-desired-capacity --auto-scaling-group-name tickr-download --desired-capacity "$M" || say "the group could NOT be raised to $M: this machine works alone"
      else say "comparison PASS, but the deadline has passed or cannot be read: the group is NOT raised"; fi
    else
      cat $D/cmp.out >> $LOG 2>/dev/null; echo "FAIL $IID $(date -u +%FT%TZ)" | aws s3 cp - $J/crosscheck --only-show-errors
      stop_fleet "the cloud's files of $XDAY differ from the Mac's (or the day could not be made)"; exit 1
    fi
    rm -rf $D/wx $D/cmp
  else
    # time limit: two leases of the comparison claim. After it this machine gives up with a failure (counted by the wrapper), it never starts the runner.
    [ $(( $(date +%s) - W0 )) -gt ${CHECK_LIMIT_SEC:-5400} ] && { say "REFUSED: no comparison result after the time limit"; exit 1; }
    say "waiting: another machine holds the comparison"; sleep ${CHECK_WAIT_SEC:-30}
  fi
done

# --- the run. Two days are fetched ahead of the converter(s). Oldest day first (the Mac goes newest first; whoever meets a finished day skips it).
RAWSET=$(aws s3 cp $J/raw - 2>/dev/null | tr -dc 'a-z' | head -c 3)
[ "$RAWSET" = on ] && export RUN_RAW=$B/raw
say "runner starts, raw archive: ${RUN_RAW:-off}"
# Converters per machine: 1 unless jobs/download/conv says 2 (the switch is read once, at the start of a machine; writing 1 there puts
# every later machine back). With 2 each converter gets half of the cores, and a memory limit by the machine's size: 20GB each on a
# machine of 56 GB or more, 10GB each below that (the Mac runs the same converter with 10GB on the largest days). Limits and threads
# are not in the manifest options, so finished days stay finished.
NCONV=$(aws s3 cp $J/conv - 2>/dev/null | tr -dc 0-9 | head -c 1); [ "$NCONV" = 2 ] || NCONV=1
MEMG=$(free -g | awk 'NR==2{print $2}'); CTHR=$(nproc); CMEM=20GB
if [ "$NCONV" = 2 ]; then CTHR=$(( $(nproc) / 2 )); [ "$CTHR" -ge 1 ] || CTHR=1; [ "$MEMG" -ge 56 ] || CMEM=10GB; fi
say "converters $NCONV, each threads $CTHR mem $CMEM (machine $MEMG GB)"
# The first comparison was made with one converter, every core and 20GB. Two converters work with other threads and another limit, so
# before any day is made with them a machine converts $XDAY and $XDAY2 AT THE SAME TIME with exactly these settings into its own
# fleet-test2/<machine>/ and compares both with the Mac's files. The result is one object PER SETTING, jobs/download/crosscheck2-<threads>-<mem>:
# a PASS made with 20GB says nothing about 10GB, so a machine with other settings compares for itself. PASS: two converters.
# FAIL (a difference, a converter that could not finish): the switch is written back to 1 and this machine works with one converter.
if [ "$NCONV" = 2 ]; then
  X2=$J/crosscheck2-$CTHR-$CMEM; R2=$(aws s3 cp $X2 - 2>/dev/null | head -c 4)
  if [ "$R2" != PASS ] && [ "$R2" != FAIL ]; then
    P=$B/fleet-test2/$IID; T0=$(date +%s); OK=1; TM=; [ -x /usr/bin/time ] && TM="/usr/bin/time -v"
    say "two-converter comparison of $XDAY and $XDAY2 with the Mac: started here"
    for d in $XDAY $XDAY2; do
      rm -rf $D/wx-$d $D/cmp-$d; mkdir -p $D/wx-$d $D/cmp-$d/mac $D/cmp-$d/aws
      ( TICK_PX=$P/px TICK_THREADS=$CTHR TICK_MEM=$CMEM $TM $PY -W ignore $D/wm2tick-v11.py $d $d $D/wx-$d $P/tk61 > $D/cross-$d.out 2>&1; echo $? > $D/rc-$d ) &
    done
    wait
    [ -e /work/STOP ] && { say "two-converter comparison interrupted by a stop"; exit 3; }
    for d in $XDAY $XDAY2; do
      RC=$(cat $D/rc-$d 2>/dev/null); RC=${RC:-1}
      say "$d converted rc=$RC in $(( $(date +%s) - T0 )) s, free disk $(df -h $D | awk 'NR==2{print $4}'), peak kB $(grep -h 'Maximum resident' $D/cross-$d.out | tr -dc 0-9): $(grep -h '^day \|^redo \|^skip \|FAILED\|REFUSED' $D/cross-$d.out | tr '\n' ' ' | cut -c1-300)"
      [ "$RC" = 0 ] || OK=0
      for f in tk61:tk px:px s10:s10; do aws s3 cp $B/${f%%:*}/2026/$d.parquet $D/cmp-$d/mac/${f##*:}.parquet --only-show-errors && aws s3 cp $P/${f%%:*}/2026/$d.parquet $D/cmp-$d/aws/${f##*:}.parquet --only-show-errors || OK=0; done
      [ $OK -eq 1 ] && $PY $D/cmpday.py $D/cmp-$d/mac $D/cmp-$d/aws > $D/cmp-$d.out 2>&1 || OK=0
      cat $D/cmp-$d.out >> $LOG 2>/dev/null
      rm -rf $D/wx-$d $D/cmp-$d
    done
    if [ $OK -eq 1 ]; then R2=PASS; else R2=FAIL; echo 1 | aws s3 cp - $J/conv --only-show-errors; fi
    echo "$R2 $IID $(date -u +%FT%TZ) threads $CTHR mem $CMEM" | aws s3 cp - $X2 --only-show-errors
    say "two-converter comparison $R2"
  fi
  if [ "$R2" = FAIL ]; then NCONV=1; CTHR=$(nproc); CMEM=20GB; say "two-converter comparison failed: this machine works with ONE converter, threads $CTHR mem $CMEM"; fi
fi
TICK_PX=$B/px TICK_THREADS=$CTHR TICK_MEM=$CMEM RUN_MD5=$CONV_MD5 RUN_FETCH=2 RUN_CONV=$NCONV RUN_AHEAD=2 RUN_FLOOR_GB=40 RUN_CLAIM_MIN=20 RUN_CLOSED=$D/closed-days.txt RUN_SKIPTEST=1 STOP_FILE=/work/STOP RUN_LOG=$J/logs/tickrun-$IID.log \
  $PY -W ignore $D/tickrun.py $D/wm2tick-v11.py 2021-11-16 2026-11-19 0/1 $D/run $B/tk61 > $D/tickrun.out 2>&1
RC=$?
# A comparison that failed while this machine worked: its exit must not mark the job as finished.
[ "$(aws s3 cp $J/crosscheck - 2>/dev/null | head -c 4)" = FAIL ] && { say "runner ended rc=$RC, but the comparison failed: not finished"; exit 1; }
say "runner ended rc=$RC: $(tail -2 $D/run/tickrun.log | tr '\n' ' ' | cut -c1-400)"
exit $RC
