# Operations on the Mac

How the owner runs Aperture under launchd. Nothing here is installed from the cloud: the owner
renders the job files, reads them, and loads them.

## Jobs

| Job | Label | When | Relaunch |
|---|---|---|---|
| preflight | `local.aperture.preflight` | `[runtime] preflight_start_local` on `launch_weekdays` | never |
| bot | `local.aperture.bot` | `[runtime] bot_start_local` on `launch_weekdays` | never (a crash raises an alert) |
| watchdog | `local.aperture.watchdog` | `[runtime] watchdog_start_local` on `launch_weekdays`; runs until its last check | never |
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
trade to exit at the session's flatten time (at once when there is no session or no calendar),
and writes `data/status.json` on every loop pass. On SIGTERM it cancels working entries, leaves
stops resting and exits 0, or 5 if `shutdown_deadline_seconds` passed with work pending. Once per
completed minute in the regular session it runs the active strategy on the open position's
symbol, the strategy's WATCH symbols and the latest discovery candidates; ENTER decisions go
through the platform risk checks before any order is sent, and every decision is written to
`data/decisions/<environment>/<date>/decisions.jsonl`. Without a trading calendar it never opens
a trade and status shows `entries_not_wired`.

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
5. The iMessage recipient variable named by `[alerts] imessage_recipient_variable` is set
   (cause `alert-recipient-missing`; blocks the start, because the phone is the alert channel
   that reaches you away from the Mac). Presence only, never the value.
6. Broker token folder `<data_dir>/<token_dir>` exists.
7. The active strategy builds and `[market_data] history_sessions` covers its history need. A
   failure is reported, but the bot still starts with entries off so it can protect positions.
8. Broker token usable (only when no earlier check blocks the start and today has a session):
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
3. Data and token folders: `mkdir -p data/<token_dir>` (see `[environments.*] token_dir`).
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
cp data/launchd/local.aperture.preflight.plist ~/Library/LaunchAgents/
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/local.aperture.preflight.plist
```

Load `local.aperture.bot.plist`, `local.aperture.watchdog.plist`, `local.aperture.api.plist` and
`local.aperture.test-alert.plist` the same way. After changing `[runtime]` values, render again, then `launchctl bootout` and `bootstrap`
the changed jobs.

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
`exit-refused`, `observers-stopped`, `not-flat-after-flatten`, `exit-not-filled`, and from
the watchdog `watchdog-not-flat`, `watchdog-cannot-check`, `bot-down-with-open-trade`. Every alert goes to
the Mac notification and to iMessage (`[alerts] imessage_recipient_variable`, see setup step 4).
The iMessage is sent on a background thread, so the bot's loop never waits for Messages.app; its
result is journaled as its own `alert` record (`"channel": "imessage"`) once known. An alert
counts as shown, for repeat suppression, when either channel delivered it.
The same kind and text inside `[alerts] dedupe_window_seconds` is shown once; every occurrence is
still recorded (in the bot's journal, the watchdog's journal, or the job log for these commands).

## Fill to stop

While a trade has an entry working, filled shares not yet covered by a stop that Detail shows
working, or an exit in progress, the bot does no discovery and runs no strategy: each pass is
only the Detail and positions reads and the protection step. One `evaluation-skipped` row marks
the start of each such stretch. A pass whose market upkeep, discovery and strategy took longer
than `[runtime] evaluation_budget_seconds` journals `evaluation-slow`.

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

## Watchdog

The bot's own day-end check dies with the bot. The protective stop is a DAY order and a crashed
bot is not restarted, so after a crash nothing would flatten at 15:55 ET and the position would
go overnight without a broker-side stop. The watchdog job (`python -m aperture.runtime watchdog`)
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
- Every check and alert is one JSON line in `data/watchdog.jsonl` (`[runtime]
  watchdog_journal_file`) and in `~/Library/Logs/Aperture/watchdog.log`.
- Exit 0: all checks clear; 1: something alerted; 2: config invalid (alert sent); 3: no session.
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
   reconciles first, but you decide.

