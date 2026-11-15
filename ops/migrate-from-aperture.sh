#!/bin/sh
# One-time move of the owner's Mac from the old product name (Aperture) to Tickr.
#
# PULL AND MIGRATE IN ONE SITTING, FLAT (no open trade), OUTSIDE MARKET HOURS: after a pull
# without this script the old launchd jobs fail at import (package `aperture` is gone) and
# nothing alerts. Run from the repository root, right after pulling the rename:
#   sh ops/migrate-from-aperture.sh            # dry run: checks everything, prints each step
#   sh ops/migrate-from-aperture.sh --apply    # does it, then verifies
#
# What it moves: local.aperture.* launchd jobs -> local.tickr.* (old plists kept in
# <data_dir>/launchd-pre-tickr/), ~/.config/aperture -> ~/.config/tickr with APERTURE_* variable
# names renamed to TICKR_* (values untouched, never printed; a 0600 copy of the file is kept as
# env.pre-tickr), <data_dir>/aperture.sqlite3 (checkpointed first) -> tickr.sqlite3,
# backups aperture-*.sqlite3 -> tickr-*, ~/Library/Logs/Aperture -> ~/Library/Logs/Tickr, and the
# venv's installed package (aperture -> tickr). The old lock file stays: the new bot refuses to
# start while anybody holds it.
#
# It refuses, before changing anything: off macOS; without .venv; on a New York weekday between
# 04:00 and 20:00 (extended hours); while the old bot job runs or the old lock records a live pid;
# with a trade not closed in the old database; when both old and new databases, or both old and
# new config folders, exist. Before unloading anything it closes the broker token folders
# (chmod 700) and the env file (chmod 600) to other accounts. After unloading the old jobs it
# refuses while any process still holds the old lock or has the old database open (then nothing
# is scheduled: stop that process and run the script again). Every step is skipped when already done, so a rerun is safe.
#
# The repository folder may keep its old name; if you rename it, recreate .venv first (its
# scripts hold absolute paths). The iOS app has a new bundle id and link scheme (tickr://): pair
# the phone again with `python -m tickr.api pair`; the old app and its Keychain item (service
# local.aperture.ios.api) stay on the phone until you delete the old app.
set -eu

APPLY=0
[ "${1:-}" = "--apply" ] && APPLY=1
refuse() { echo "refused: $*" >&2; exit 1; }

[ "$(uname)" = "Darwin" ] || refuse "macOS only"
[ -f config/bot.toml ] || refuse "run from the repository root"
[ -x .venv/bin/python ] || refuse "no .venv here; create it as docs/operations.md says, then run this again"
PY=.venv/bin/python

UID_NOW=$(id -u)
AGENTS="$HOME/Library/LaunchAgents"
JOBS="preflight bot watchdog api test-alert"
OLD_ENV_DIR="$HOME/.config/aperture"
NEW_ENV_DIR="$HOME/.config/tickr"
OLD_LOGS="$HOME/Library/Logs/Aperture"
NEW_LOGS="$HOME/Library/Logs/Tickr"
# Seconds to let unloaded jobs exit before checking that nothing holds the old files.
EXIT_WAIT_SECONDS=5
DATA=$("$PY" -c 'import tomllib; print(tomllib.load(open("config/bot.toml", "rb"))["run"]["data_dir"])') \
  || refuse "cannot read [run] data_dir from config/bot.toml"

run() {
  echo "+ $*"
  if [ "$APPLY" = 1 ]; then "$@"; fi
}

loaded() { launchctl print "gui/$UID_NOW/$1" >/dev/null 2>&1; }

# 0. Outside market hours (New York, weekdays 04:00-20:00 covers pre- and post-market).
"$PY" -c '
import datetime, sys, zoneinfo
now = datetime.datetime.now(zoneinfo.ZoneInfo("America/New_York"))
sys.exit(1 if now.weekday() < 5 and 4 <= now.hour < 20 else 0)
' || refuse "New York market hours (weekday 04:00-20:00); run it in the evening or at the weekend"

# 1. Nothing of the old code running, nothing open, nothing ambiguous.
if launchctl print "gui/$UID_NOW/local.aperture.bot" 2>/dev/null | grep -q 'pid = '; then
  refuse "local.aperture.bot is running; stop it (or wait for the day to end) first"
fi
if [ -f "$DATA/aperture.lock" ]; then
  pid=$(tr -cd '0-9' < "$DATA/aperture.lock")
  if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
    refuse "$DATA/aperture.lock records live pid $pid; stop that process first"
  fi
fi
if [ -d "$OLD_ENV_DIR" ] && [ -e "$NEW_ENV_DIR" ]; then
  refuse "both $OLD_ENV_DIR and $NEW_ENV_DIR exist; keep one by hand"
fi
if [ -f "$DATA/aperture.sqlite3" ]; then
  [ -e "$DATA/tickr.sqlite3" ] && refuse "both $DATA/aperture.sqlite3 and $DATA/tickr.sqlite3 exist; decide which one is real by hand"
  open_trades=$("$PY" -c '
import sqlite3, sys
db = sqlite3.connect("file:" + sys.argv[1] + "?mode=ro", uri=True)
has = db.execute("SELECT 1 FROM sqlite_master WHERE name = ?", ("trades",)).fetchone()
print(db.execute("SELECT COUNT(*) FROM trades WHERE state != ?", ("closed",)).fetchone()[0] if has else 0)
' "$DATA/aperture.sqlite3") || refuse "cannot read $DATA/aperture.sqlite3"
  [ "$open_trades" = 0 ] || refuse "$open_trades trade(s) not closed in $DATA/aperture.sqlite3; migrate only when flat"
fi

# 2. The venv first: if the install fails, the old jobs are still loaded and nothing changed.
#    A venv made by uv has no pip: uv installs into it instead.
#    uv takes --python after the subcommand (`uv pip install --python X`), never before it.
if [ -x .venv/bin/pip ]; then
  run .venv/bin/pip install -q -e ".[webull]"
  uninstall() { run .venv/bin/pip uninstall -y -q aperture; }
elif command -v uv >/dev/null 2>&1; then
  # Read-only probe, also in a dry run: proves this uv accepts the form used below.
  uv pip list --python .venv/bin/python >/dev/null 2>&1 \
    || refuse "uv cannot read .venv (uv pip list --python .venv/bin/python failed)"
  run uv pip install --python .venv/bin/python -q -e ".[webull]"
  uninstall() { run uv pip uninstall --python .venv/bin/python -q aperture; }
else
  refuse ".venv has no pip and uv is not installed; cannot reinstall the package"
fi
if "$PY" -c 'import importlib.metadata as m; m.version("aperture")' >/dev/null 2>&1; then
  uninstall
fi

# 2b. Secret files closed to other accounts, before anything is unloaded: preflight refuses a
#     broker token folder with any group or other permission (token-dir-open), and render-launchd
#     (step 7) refuses such an env file. Permissions only, never contents; safe to repeat.
TOKEN_DIRS=$("$PY" -c '
import tomllib
envs = tomllib.load(open("config/bot.toml", "rb"))["environments"]
print("\n".join(e["token_dir"] for e in envs.values()))
') || refuse "cannot read [environments.*] token_dir from config/bot.toml"
for dir in $TOKEN_DIRS; do
  if [ -d "$DATA/$dir" ]; then run chmod 700 "$DATA/$dir"; fi
done
for f in "$OLD_ENV_DIR/env" "$NEW_ENV_DIR/env"; do
  if [ -f "$f" ]; then run chmod 600 "$f"; fi
done

# 3. Unload the old launchd jobs and keep their plists aside.
for job in $JOBS; do
  plist="$AGENTS/local.aperture.$job.plist"
  if loaded "local.aperture.$job"; then
    run launchctl bootout "gui/$UID_NOW/local.aperture.$job"
  fi
  if [ -f "$plist" ]; then
    run mkdir -p "$DATA/launchd-pre-tickr"
    run mv "$plist" "$DATA/launchd-pre-tickr/"
  fi
done

# 3b. With the old jobs unloaded (the watchdog cannot start the bot again), nothing may hold the
#     old lock or have the old database open. A dry run only reports it (the old API job, still
#     loaded then, keeps the database open).
busy() {
  echo "refused: $1; the old jobs are already unloaded: stop that process, then run this script again" >&2
  if [ "$APPLY" = 1 ]; then exit 1; fi
}
[ "$APPLY" = 1 ] && sleep "$EXIT_WAIT_SECONDS"
for f in "$DATA/aperture.lock" "$DATA/aperture.sqlite3" "$DATA/aperture.sqlite3-wal" "$DATA/aperture.sqlite3-shm"; do
  if [ -e "$f" ] && lsof -- "$f" >/dev/null 2>&1; then
    busy "a process has $f open (lsof -- $f shows which)"
  fi
done
if [ -e "$DATA/aperture.lock" ] && ! "$PY" -c 'import fcntl,os,sys; fd=os.open(sys.argv[1], os.O_RDWR); fcntl.flock(fd, fcntl.LOCK_EX|fcntl.LOCK_NB)' "$DATA/aperture.lock" 2>/dev/null; then
  busy "$DATA/aperture.lock is held by a running process"
fi

# 4. Env file folder, then the APERTURE_* variable names inside it: only names at the start of an
#    assignment (`NAME=` or `export NAME=`), values untouched; a 0600 copy is kept first.
env_now="$NEW_ENV_DIR/env"
if [ -d "$OLD_ENV_DIR" ]; then
  run mv "$OLD_ENV_DIR" "$NEW_ENV_DIR"
  [ "$APPLY" = 1 ] || env_now="$OLD_ENV_DIR/env"  # a dry run moved nothing
fi
NAMES='^[[:space:]]*(export[[:space:]]+)?APERTURE_[A-Za-z0-9_]*='
if [ -f "$env_now" ] && grep -Eq "$NAMES" "$env_now"; then
  [ -e "$NEW_ENV_DIR/env.pre-tickr" ] || run cp -p "$NEW_ENV_DIR/env" "$NEW_ENV_DIR/env.pre-tickr"
  run chmod 600 "$NEW_ENV_DIR/env.pre-tickr"
  run sed -i '' -E 's/^([[:space:]]*(export[[:space:]]+)?)APERTURE_([A-Za-z0-9_]*=)/\1TICKR_\3/' "$NEW_ENV_DIR/env"
  run chmod 600 "$NEW_ENV_DIR/env"
fi

# 5. Database: checkpoint the WAL into the main file, then move any leftover -wal/-shm before the
#    main file, so an interruption never leaves tickr.sqlite3 without its last commits (the bot
#    and preflight also refuse while any aperture.sqlite3* file is left).
if [ -f "$DATA/aperture.sqlite3" ]; then
  run "$PY" -c 'import sqlite3, sys; db = sqlite3.connect(sys.argv[1]); busy = db.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()[0]; db.close(); sys.exit("refused: checkpoint blocked by another connection" if busy else 0)' "$DATA/aperture.sqlite3"
fi
for suffix in -wal -shm ""; do
  if [ -e "$DATA/aperture.sqlite3$suffix" ]; then
    [ -e "$DATA/tickr.sqlite3$suffix" ] && refuse "$DATA/tickr.sqlite3$suffix already exists"
    run mv "$DATA/aperture.sqlite3$suffix" "$DATA/tickr.sqlite3$suffix"
  fi
done
for old in "$DATA"/backups/aperture-*.sqlite3; do
  [ -e "$old" ] || continue
  new="$DATA/backups/tickr-${old##*/aperture-}"
  [ -e "$new" ] || run mv "$old" "$new"
done

# 6. Log folder.
if [ -d "$OLD_LOGS" ] && [ ! -e "$NEW_LOGS" ]; then
  run mv "$OLD_LOGS" "$NEW_LOGS"
fi
run mkdir -p "$NEW_LOGS"

# 7. Render and load the new launchd jobs (the bot job starts only on its schedule). A job
#    already loaded by an earlier, interrupted run is unloaded and loaded again.
run "$PY" -m tickr.runtime render-launchd --python "$PWD/.venv/bin/python" \
  --env-file "$NEW_ENV_DIR/env" --log-dir "$NEW_LOGS" --out "$DATA/launchd"
for job in $JOBS; do
  if loaded "local.tickr.$job"; then
    run launchctl bootout "gui/$UID_NOW/local.tickr.$job"
  fi
  run cp "$DATA/launchd/local.tickr.$job.plist" "$AGENTS/"
  run launchctl bootstrap "gui/$UID_NOW" "$AGENTS/local.tickr.$job.plist"
done

if [ "$APPLY" = 0 ]; then
  echo "dry run only: all checks passed; run again with --apply"
  exit 0
fi

# 8. Verify: every job loaded, the package imports, preflight passes (run as the job runs it).
failed=0
for job in $JOBS; do
  loaded "local.tickr.$job" || { echo "check failed: local.tickr.$job is not loaded" >&2; failed=1; }
done
"$PY" -c 'import tickr' || { echo "check failed: the venv cannot import tickr" >&2; failed=1; }
code=0
/bin/sh -c 'set -a; . "$0"; set +a; exec "$1" -m tickr.runtime --config "$2" preflight' \
  "$NEW_ENV_DIR/env" "$PY" "$PWD/config/bot.toml" || code=$?
case "$code" in
  0) echo "preflight: ready" ;;
  3) echo "preflight: no session today; it checks again on the next trading morning" ;;
  *) echo "check failed: preflight exited $code (its JSON line above says why)" >&2; failed=1 ;;
esac
[ "$failed" = 0 ] || exit 1
echo "done. Now confirm alerts still reach the phone under the new labels:"
echo "  launchctl kickstart gui/$UID_NOW/local.tickr.test-alert"
