# Operations on the Mac

How the owner runs Aperture under launchd. Nothing here is installed from the cloud: the owner
renders the job files, reads them, and loads them. Manual trades and the Sandbox probe day
(stop the bot job first) are in docs/manual-trade.md.

## Jobs

| Job | Label | When | Relaunch |
|---|---|---|---|
| preflight | `local.aperture.preflight` | `[runtime] preflight_start_local` on `launch_weekdays` | never |
| bot | `local.aperture.bot` | `[runtime] bot_start_local` on `launch_weekdays` | never (a crash raises an alert) |
| watchdog | `local.aperture.watchdog` | `[runtime] watchdog_start_local` on `launch_weekdays`; runs until its last check | never |

The bot job has one starter besides its schedule: near the flatten time the watchdog may start it once a day (see "Watchdog", `watchdog-restarted-bot`).
| test-alert | `local.aperture.test-alert` | only on `launchctl kickstart` (grants the Messages permission, see below) | never |
| api | `local.aperture.api` | at login, always on, loopback only | launchd keeps it alive |

Times in `[runtime]` are the **Mac's local time**. The calendar decides whether there is a
session: on weekends, market closures and half days (unless `half_days_enabled`) preflight reports
"no session today" and the bot job exits without starting the bot. If the Mac is not on New York
time, check the start times around daylight-saving changes (US and other countries switch on
different dates); the bot must start before 09:30 ET.

The bot job runs `python -m aperture.runtime run-bot`, which runs preflight again, then starts the
bot as a child process. launchd stops the job with SIGTERM; the supervisor forwards one SIGTERM to
the bot, whose handler only sets the stop-entries flag; the main loop then cancels working entries,
leaves resting stops at the broker and exits. If the bot is still running
`stop_alert_after_seconds` after the SIGTERM, the supervisor alerts `bot-did-not-stop` and keeps
waiting; launchd sends SIGKILL at `bot_exit_timeout_seconds`.

## Stopping the bot safely

1. `launchctl kill TERM gui/$(id -u)/local.aperture.bot`
2. Before logging out, shutting down or `launchctl bootout`, wait for the `"record": "bot-exit"` line in `~/Library/Logs/Aperture/bot.log`
   (`"clean": true`). A `bot-did-not-stop` alert means the bot is stuck: check orders and the
   position in the Webull app before doing anything else.
3. Do not log out, restart or shut down the Mac while the bot has a working order or an open
   position. Logout and shutdown also stop the job with SIGTERM, but macOS does not fully honor
   `ExitTimeOut` then. A resting stop stays at the broker either way; a working entry order may not be
   cancelled.

`aperture.cli run` (the bot job's command) recovers and reconciles, keeps the positions it owns
protected, acts on API commands (entry-stop on/off, cancel-own-orders, flatten), asks every open
trade to exit at the session's flatten time (at once when there is no session or no calendar;
see `flatten-after-close` for a start after the close),
and writes `data/status.json` on every loop pass. On SIGTERM it cancels working entries, leaves
stops resting and exits 0, or 5 if `shutdown_deadline_seconds` passed with work pending. Once per
completed minute in the regular session it runs the active strategy on the open position's
symbol, the strategy's WATCH symbols and the latest discovery candidates; ENTER decisions go
through the platform risk checks before any order is sent, and every decision is written to
`data/decisions/<environment>/<date>/decisions.jsonl`. Without a trading calendar it never opens
a trade and status shows `entries_not_wired`.

From the apps, `POST /commands/stop-bot` stops the bot the same way and makes the stop stick,
but only when the bot is flat: with a position open, an entry working, or a close in progress,
it is refused and the bot keeps running (send emergency or flatten first, wait for every trade
to close, then stop-bot). A
SIGTERM does not have this check: it leaves an open position under its DAY stop, and nothing
flattens it while the bot is down. When obeyed, the bot writes `data/<[runtime] stop_marker_file>` and every later start is refused (preflight
cause `stopped-by-operator`, with an alert) until you run `aperture clear-stop`. See
docs/api.md, "Commands".

## Exit codes

`python -m aperture.runtime preflight` and `run-bot`:

| Code | Meaning |
|---|---|
| 0 | ready (preflight) / bot exited cleanly (run-bot) |
| 1 | a check failed (alert sent); for run-bot also "bot killed by a signal" |
| 2 | config invalid (alert sent if `[alerts]` itself is readable) |
| 3 | no session today (not a failure, no alert) |
| other | run-bot: the bot's own non-zero exit code (alert "bot-crashed") |

Every run prints JSON lines (`"record": "preflight" | "alert" | "bot-exit"`) to the job log.

## Preflight checks

1. Config valid: loads, every key commented, active strategy file present, launchd schedule valid,
   and `[orders] shutdown_deadline_seconds` < `stop_alert_after_seconds` < `bot_exit_timeout_seconds`.
2. Data folder exists; free disk on it at least `[runtime] min_free_disk_mb`.
3. Calendar knows today (New York date).
4. Credential variables named in the active `[environments.*]` table are set. Only the names are
   checked and reported, never the values.
5. This machine has an alert channel (cause `alert-channel-missing`): today only macOS. On
   Linux, including Docker, the start is refused; see `docs/deploy-server.md`.
6. The iMessage recipient variable named by `[alerts] imessage_recipient_variable` is set
   (cause `alert-recipient-missing`; blocks the start, because the phone is the alert channel
   that reaches you away from the Mac). Presence only, never the value.
7. Broker token folder `<data_dir>/<token_dir>` exists.
7b. No operator stop marker (cause `stopped-by-operator`): a stop-bot command from the apps
   left `<data_dir>/<[runtime] stop_marker_file>`; `aperture clear-stop` removes it.
7c. The database, when it exists, holds runs of this environment only (cause
   `database-other-environment`; `database-unreadable` when it cannot be opened). After
   changing `[run] environment`, move `data/aperture.sqlite3` aside and keep it: the apps then
   no longer show the other environment's days.
8. The active strategy builds and `[market_data] history_sessions` covers its history need. A
   failure is reported, but the bot still starts with entries off so it can protect positions.
9. Broker token usable (only when no earlier check blocks the start and today has a session):
   the Webull SDK creates or checks the token. If Webull says it is PENDING, preflight alerts `token-needs-confirmation`
   and waits up to `[environments.*] token_check_duration_seconds` for you to approve it in the
   Webull app. INVALID, EXPIRED, not approved in time, or any other token error fails the check
   with cause `token-unusable`. Where Webull has token checking off, this is one config read.
   `aperture run` alerts the same way (journaled as an `alert` record) if its token waits.

All checks are reported; the first failure is the recorded cause.

## One-time setup

1. Clone the repo and create the venv:
   ```sh
   cd ~/Development/Aperture
   python3.12 -m venv .venv && .venv/bin/pip install -e ".[webull]"
   ```
2. Credentials file outside the repo, readable only by you. One `NAME=value` per line, using the
   variable names from `config/bot.toml` (for the active environment):
   ```sh
   mkdir -p ~/.config/aperture && touch ~/.config/aperture/env && chmod 600 ~/.config/aperture/env
   open -e ~/.config/aperture/env
   ```
   The jobs load it with `sh` (`set -a; . file`), so Aperture code never reads the values. Quote a
   value with single quotes if it contains spaces or `$`.
   Add your iMessage address here too, under the name in `[alerts] imessage_recipient_variable`:
   `APERTURE_IMESSAGE_TO='+15551234567'` (or your Apple ID email). It is personal data: never
   put it in the repo or in a plist. To test by hand in Terminal:
   `set -a; . ~/.config/aperture/env; set +a` first.
   **Deadline:** `APERTURE_IMESSAGE_TO` must be in this file before the first preflight run
   (08:30 Mac time on Monday with the default `[runtime] preflight_start_local`). Without it
   preflight fails with `alert-recipient-missing` and the bot job does not start the bot.
3. Data and token folders: `mkdir -p data/webull-token-sandbox` (`[environments.sandbox]
   token_dir`; `data/webull-token-production` before Production).
4. Alerts: sign in to iMessage in the Messages app on this Mac (Messages > Settings > iMessage)
   with an Apple ID, then, after loading the env file as above, run
   `.venv/bin/python -m aperture.runtime alert preflight-failed "test"` once. You should get a
   notification on the Mac and an iMessage on the phone. macOS shows notifications from
   `osascript` under "Script Editor"; allow them in System Settings > Notifications if nothing
   appears. The first iMessage send asks to let Terminal control Messages: allow it. This only
   covers Terminal; the launchd jobs need their own permission (see "Alerts from launchd"
   below). The first JSON line is the Mac notification (`"delivery": "shown"`,
   `"pending": ["imessage"]`); a second line with `"channel": "imessage"` follows with the
   iMessage result. A failed send is never retried; the next alert tries again.
5. Run preflight by hand: `.venv/bin/python -m aperture.runtime preflight; echo $?`.

## Install the jobs

```sh
cd ~/Development/Aperture
mkdir -p ~/Library/Logs/Aperture
.venv/bin/python -m aperture.runtime render-launchd \
    --python "$PWD/.venv/bin/python" \
    --env-file ~/.config/aperture/env \
    --log-dir ~/Library/Logs/Aperture \
    --out data/launchd
# Read the five files in data/launchd, then:
for job in preflight bot watchdog api test-alert; do
  cp data/launchd/local.aperture.$job.plist ~/Library/LaunchAgents/
  launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/local.aperture.$job.plist
done
```

After changing `[runtime]` values, render again, then
`launchctl bootout gui/$(id -u)/local.aperture.<job>` and `bootstrap` the changed jobs.

### Alerts from launchd (do this once, at the Mac)

macOS grants the Automation permission ("allow ... to control Messages") per calling program.
Allowing it for Terminal does not cover the launchd jobs, and a job cannot click "OK": without
the permission every iMessage from the bot and the watchdog fails (recorded as
`imessage: osascript exited with code 1`) while the Mac notification still works. So, while you
are at the Mac, after loading `local.aperture.test-alert.plist`:

```sh
launchctl kickstart gui/$(id -u)/local.aperture.test-alert
```

A dialog asks to allow control of Messages: click OK. You should get a notification and an
iMessage "test alert from launchd". Check `~/Library/Logs/Aperture/test-alert.log`: the
`"channel": "imessage"` line must say `"delivery": "shown"`. The test-alert job runs the same
program chain as the bot and watchdog jobs (`/bin/sh`, then the venv python) and has no
schedule, so it can stay installed; kickstart it again after recreating the venv or upgrading
macOS. If no dialog appears and the send fails, open System Settings > Privacy & Security >
Automation and allow Messages for the listed program.

## Everyday commands

```sh
launchctl print gui/$(id -u)/local.aperture.preflight      # state and last exit code
launchctl kickstart gui/$(id -u)/local.aperture.preflight  # run preflight now
launchctl kill TERM gui/$(id -u)/local.aperture.bot        # stop the bot cleanly
launchctl bootout gui/$(id -u)/local.aperture.bot          # remove a job
tail -f ~/Library/Logs/Aperture/bot.log
```

To look at the apps with a full made-up day (never Webull, its own folder): `aperture demo`,
see [demo.md](demo.md).

## Disk upkeep: `aperture maintain`

What grows under `data/`, per environment and New York date: `capture/<env>/<date>/`
(`quotes.jsonl` is by far the largest: every stream quote; then `bars.jsonl`, `screener.jsonl`,
`notices.jsonl`) and `decisions/<env>/<date>/decisions.jsonl`. The database (`aperture.sqlite3`)
grows slowly and is never pruned: the journal is the audit trail. `history/` is a cache of
prior-session bars, the backup folder keeps one file, `watchdog.jsonl` gets a few lines a day;
launchd's logs in `~/Library/Logs/Aperture` are not ours to rotate.

Never run it during the trading session, not even as a dry run: it takes the bot's
single-instance lock and writes its own pid into the lock file, so a bot job starting meanwhile
is refused and the watchdog and `status` see a pid that is not the bot. Run it with the bot
stopped, after the close or on a non-trading day:

```sh
.venv/bin/python -m aperture.cli maintain          # dry run: prints what it would archive and why it keeps the rest
.venv/bin/python -m aperture.cli maintain --apply  # does it
```

- Each `*.jsonl` of a finished day folder is gzipped in place to `*.jsonl.gz` (written, read back
  and compared, then renamed; the plain file goes last). Research, the API's `/bars` and `/days`,
  and `export_replay_bars` read both forms; give research tools the plain name or the `.gz` name.
- A day folder is kept (one printed cause) when it is within the last `[run]
  maintain_keep_sessions` sessions before today (today included), on or after the day of a trade
  still open in the database, without its `day-end` journal row, or when a plain file sits beside
  a `.gz` that is unreadable or holds other bytes (look at both, remove the wrong one).
- `--apply` also runs a SQLite WAL checkpoint; it deletes no row and never vacuums.
- Refused (exit 3, nothing touched) while the bot holds its lock, when the calendar cannot count
  the sessions, or when the database is missing or has another schema. Exit 1 means one file
  failed to archive: the run stopped there and that file is left as it was.

## The Mac must be awake

launchd runs a calendar job missed during sleep when the Mac wakes, which may be after the open.
Schedule a wake before the preflight time, for example
`sudo pmset repeat wakeorpoweron MTWRF 08:20:00` (Mac local time).

While the bot job and the watchdog job run, each starts `/usr/bin/caffeinate -i -s -w <its own
pid>` beside itself (in the plist's shell line, before `exec`): a no-sleep assertion that ends
when the job's process ends. It is a side process, not a wrapper, so launchd's SIGTERM still goes
straight to the supervisor. Limits:

- `-i` stops idle sleep on battery and power adapter; `-s` (system sleep) works only on the
  power adapter. Keep the Mac on the power adapter on trading days.
- Closing the lid sleeps the Mac anyway (unless it is on power with an external display,
  keyboard and mouse: clamshell mode). Do not close the lid on a trading day.
- A low battery, a forced shutdown, or choosing Sleep in the Apple menu still sleep the Mac.
- `pmset -g assertions` shows `caffeinate` holding `PreventUserIdleSystemSleep` while a job runs.

## Alerts

`python -m aperture.runtime alert <kind> <text>` shows one alert. Kinds (spec section 11):
`unprotected-position`, `stop-rejected`, `unknown-order-status`, `foreign-exposure`,
`broker-reads-failing`, `stream-lost-with-position`, `daily-ceiling-reached`, `bot-crashed`,
`bot-did-not-start`, `bot-did-not-stop` (added in review), `preflight-failed`, `token-needs-confirmation`,
`exit-refused`, `observers-stopped`, `not-flat-after-flatten`, `exit-not-filled`,
`flatten-after-close`, and from
the watchdog `watchdog-not-flat`, `watchdog-cannot-check`, `bot-down-with-open-trade`,
`watchdog-restarted-bot`, `watchdog-restart-refused`, `watchdog-restart-failed`. Every alert goes to
the Mac notification and to iMessage (`[alerts] imessage_recipient_variable`, see setup step 4).
The iMessage is sent on a background thread, so the bot's loop never waits for Messages.app; its
result is journaled as its own `alert` record (`"channel": "imessage"`) once known. An alert
counts as shown, for repeat suppression, when either channel delivered it.
The same kind and text inside `[alerts] dedupe_window_seconds` is shown once; every occurrence is
still recorded (in the bot's journal, the watchdog's journal, or the job log for these commands).

### `broker-reads-failing`

Entries are refused while a positions or open-orders read fails (`broker-read-failed`, or
`broker-read-failed:open-orders`, which only a successful reconciliation clears). The bot
still reads order Detail and keeps the stops; it closes no trade and sends no closing order
until positions can be read again.

### `unknown-order-status` on a closing order or stop

A sell whose send outcome is unknown is not given up on while the account holds fewer shares
than the bot's orders explain: it may have filled. If the alert stays, look in the Webull app.
If the shares were sold there by hand and the bot's order never arrived, the bot keeps waiting
for it and the rest of the position may have no stop: close it in the Webull app.

### `refused: the database holds runs of environment ...`

One database belongs to one environment (spec section 10). After changing `[run] environment`,
move `data/aperture.sqlite3` aside (keep it) or point `[run] data_dir` at another folder; the
bot and the manual commands refuse to start on another environment's records.

## Fill to stop

While a trade has an entry working, filled shares not yet covered by a stop that Detail shows
working, or an exit in progress, the bot does no discovery and runs no strategy: each pass is
only the Detail and positions reads and the protection step. One `evaluation-skipped` row marks
the start of each such stretch. A pass whose market upkeep, discovery and strategy took longer
than `[runtime] evaluation_budget_seconds` journals `evaluation-slow`.

When a decision sends an entry (or asks an exit) in the middle of a minute, the symbols left in
that minute are put aside (`evaluation-stopped`, stage `protect-first`, with `remaining` and
their quotes at that moment). On the first pass the strategy may run again they are evaluated
on that same bar and those quotes, for the record only: their ENTERs are recorded
`not_evaluated` and nothing is sent. An `evaluation-remainder` row marks that this was done
(once, also across a restart). If their bars or history cannot be read, an
`evaluation-stopped` row with stage `remainder-...` names the symbols left without a record.

Each trade that had a fill gets one `protection-latency` row: `fill_observed_at` (first Detail
showing filled shares), `stop_sent_at`, `stop_verified_at`, `observed_to_sent_seconds` and
`observed_to_verified_seconds`. Webull Detail has no fill time, so fill -> fill observed is not
measured (`fill_to_observed_seconds` is null). Only the first fill of a trade is measured.
`closed_before_verified` is true when the trade closed before any stop was seen working.

## End of the day

`[session] flatten_grace_seconds` after the flatten time (15:55 ET on a normal day), the bot
checks the latest broker reads (positions and open orders read after the flatten time, never
our records alone) and journals one `day-end` row per trading day: `broker_flat`,
`own_positions`, `own_open_orders`, `trades_today`, `trades_closed`, `open_trades`, `agree`
(broker positions and open orders equal what our records expect, nothing foreign) and
`day_clean`. If the reads failed, `broker_read` is false and `day_clean` is null: the day is
not counted as clean. Preflight refuses a grace not above `[orders] reconcile_interval_seconds`.
The row is in the journal only; `status.json` (status-v1) has no field for it, because v1
refuses unknown keys and adding one would be a version 2.

### `not-flat-after-flatten`

The broker still shows a position of ours, or an order of ours still working, after the flatten
time plus the grace. The usual cause is a closing MARKET order that did not fill (a halt, no
liquidity). The alert repeats every `[alerts] dedupe_window_seconds` while it lasts.

1. Open the Webull app and look at the symbol named in the alert: position, working orders,
   and whether the stock is halted.
2. The protective stop is a DAY order: it expires at the 16:00 ET close. After that the
   position has no broker-side stop. Close it in the Webull app before the close if you can.
3. If it cannot be closed today, it stays overnight without a broker-side stop: decide in the
   Webull app (sell in extended hours, or place your own stop for the next session). The bot
   does not resolve this on its own; review the journal before the next session.

### `exit-not-filled`

A closing order (MARKET SELL) is still not filled `[orders] max_exit_seconds` after it was
sent. The bot sends nothing more: no second close, no cancel. The shares in that order have no
broker stop while it works (the stop was cancelled before the close was sent).

1. Open the Webull app: is the stock halted, is the order still working, how much filled?
2. If the order is still working, let it work or cancel it in the app. If it ends (filled,
   cancelled, expired), the bot sees it: filled closes the trade; otherwise the bot puts a stop
   back on what is still held and sends no other close.
3. When you have looked, `aperture clear-fence exit-not-filled:<trade>`. If the trade is still
   open, the bot then sends one new closing order.

### `flatten-after-close`

A close was due (the session flatten, or any other exit) but it is outside the regular session:
from `[session] close_order_cutoff_seconds` (60 s) before the regular close, 15:59 ET on a
normal day and 12:59 ET on a half day (the calendar's early close), until the next regular
open, or on a day without a session. What Webull does with a MARKET SELL, DAY, session CORE
sent then is unproven (docs/webull-facts.md, F6b): it may reject it, or queue it and sell at
the next open with nobody deciding. So the bot sends no closing order: the trade stays
`exiting`, its broker stop stays resting (never cancelled for this), entries are blocked, and
the operator fence `market-closed:<trade>` carries the one cause. The usual way here is a bot
that started late, after the close (a manual start, or a restart by a supervisor), with a trade
still open. An operator `flatten` or `emergency` in that time is refused the same way (the
command record says "no closing order is sent now"); `aperture exit` and `aperture resume`
stop with exit 4 and print the fence.

1. Open the Webull app: the position and its stop. The stop is a DAY order and expires at the
   close; after that the position has no broker-side stop.
2. Close it in the Webull app (extended hours, or at the next open), or place your own stop
   for the next session.
3. The fence does not clear itself, so nothing is sold at the next open on its own. If you
   want the bot to close the trade instead, `aperture clear-fence market-closed:<trade>`
   during the regular session: the bot then sends one closing order. Cleared outside the
   session, the fence is set again at once with the same alert.

## Watchdog

The bot's own day-end check dies with the bot. The protective stop is a DAY order and launchd
never relaunches a crashed bot, so after a crash nothing would flatten at 15:55 ET and the
position would go overnight without a broker-side stop. The watchdog job (`python -m aperture.runtime watchdog`)
is a separate process that covers this:

- It starts at `[runtime] watchdog_start_local` on `launch_weekdays` and exits after its last
  check. Weekends and market closures: exit 3, no alert. Half days: it runs (a position after
  the flatten time is wrong on any day).
- It never takes the bot's lock, opens the bot's database read-only, and only ever reads
  positions and open orders: it cannot place, cancel or replace an order.
- Its times come from the calendar, not the clock: `watchdog_after_flatten_seconds` after the
  flatten time (15:57:30 ET on a normal day) and `watchdog_before_close_seconds` before the
  regular close (15:59 ET). Preflight refuses values that do not fall after the bot's own
  day-end check and before the close. Started late (the Mac woke after these times), it checks
  once at once.
- One restart a day (below): from `[runtime] watchdog_restart_before_flatten_seconds` before
  the flatten time (15:50 ET on a normal day), if the bot is down while its database shows an
  open trade, the watchdog starts the bot job once. It still sends nothing itself.
- Every check and alert is one JSON line in `data/watchdog.jsonl` (`[runtime]
  watchdog_journal_file`) and in `~/Library/Logs/Aperture/watchdog.log`.
- Exit 0: all checks clear; 1: something alerted; 2: config invalid (alert sent); 3: no session.
- On a machine with no alert channel (not macOS) it writes a `watchdog-alert-channel-refused`
  line (`alert-channel-missing`), still runs and journals, and never exits 0, so the scheduler
  shows a failed job.
- If the iMessage recipient variable is not set, the watchdog writes a
  `watchdog-alert-channel-refused` line (`alert-recipient-missing`) and still runs, alerting on
  the Mac only; every iMessage attempt records the same cause.

### `watchdog-not-flat`

At a watchdog check after the flatten time the broker shows a position or an open order, ours
or not. The text names each one. The alert repeats at the second check (the text names the
time). Most likely the bot crashed or its closing order did not fill.

1. Open the Webull app now. Look at the position and the working orders.
2. Close the position in the Webull app before 16:00 ET if you can. If you cannot, place a
   stop by hand: the bot's stop is DAY and expires at the close, after which the position has
   no broker-side stop.
3. Cancel orders you do not want working. Do not start the bot to fix it; review the journal
   and the bot log before the next session.

### `watchdog-cannot-check`

The watchdog could not read the broker (network, token, Webull error), the bot's database, or
the calendar, so it does not know whether you are flat. Open the Webull app and check positions
and orders yourself, as above. Then look at `watchdog.log` for the cause.

### `bot-down-with-open-trade`

During the regular session the bot does not count as running while its database shows an
open trade. Running means both: its lock file names a live process, and `data/status.json`
(written every loop pass) is at most `[runtime] watchdog_status_stale_seconds` old. The text
says which failed: "no live pid in its lock file" (crashed or stopped), "status file older
than ..." (hung, or the pid now belongs to another process after a crash or reboot), "no status
file" or "status file unreadable". Nothing will flatten that trade at 15:55 ET.
A mid-session restart of the bot can raise this alert once while the new bot starts and
recovers (no live pid, or a status file not yet rewritten); if the next check is quiet and
`data/status.json` is fresh again, the bot is back.

1. Open the Webull app: is the position still there, and is its stop working?
2. Close the position by hand, or keep it with its stop and close it before 16:00 ET yourself.
3. Do not restart the bot over an open position without reading the bot log; startup
   reconciles first, but you decide. If you do nothing, the watchdog starts the bot job once
   at 15:50 ET (below).

### One restart a day: `watchdog-restarted-bot`

From `[runtime] watchdog_restart_before_flatten_seconds` before the flatten time (300 s:
15:50 ET on a normal day, 12:50 ET on a half day) until its last check, a watchdog bot check
that finds the bot down while its database shows an open trade starts the bot job once:
`[runtime] watchdog_restart_command`, by default `launchctl kickstart gui/<uid>/local.aperture.bot`
(without `-k`, so a job launchd still sees running is left alone). That is the same job the
schedule starts: preflight again (a failure alerts `bot-did-not-start`), the supervisor, the
single-instance lock, then the normal startup: recovery, reconciliation, the stop verified.
Config refuses a restart time at or before the last entry time, so the restarted bot can never
open a trade; at the flatten time its own `flatten_due` closes the position through the
normal exit path. The watchdog itself never places, cancels or replaces anything.

- Once a day: the New York date is written to `data/watchdog-restart.json` (`[runtime]
  watchdog_restart_file`) before the command runs, so there is never a second attempt that
  day, also if the watchdog job itself is started again. Later bot checks that still find the
  bot down alert `bot-down-with-open-trade` as before (the restarted bot may still be in
  preflight or waiting for the token).
- `watchdog-restart-refused`: it would restart but will not, with the cause in the text: the
  operator's stop marker exists (the watchdog never overrides a stop-bot; a stop with an open
  trade is yours to look at), the restart record is unreadable or cannot be written, or the
  clock's New York date is not the calendar day. Close the position by hand before 16:00 ET.
- `watchdog-restart-failed` (loudest; texts start with URGENT): the command failed or timed
  out (`watchdog_restart_timeout_seconds`), or on the restart day a broker check after the
  flatten time finds a position or open order, or the bot still down with an open trade. It
  replaces `watchdog-not-flat` on that day. Open the Webull app now and close by hand.
- On a quiet day nothing changes: a running bot, or a flat database, never triggers it.
- To keep the bot down on purpose with a position open (a manual trade), unload the bot job, or
  keep a stop marker left from an earlier stop made while flat. A new `stop-bot` is refused while
  any trade is open, so it cannot create the marker then. See docs/manual-trade.md.
- Owner check, once on the Mac: the exit code of `launchctl kickstart gui/$(id -u)/local.aperture.bot`
  (no `-k`) for a job launchd still sees running, and for a booted-out job. Record both here.

