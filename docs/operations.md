# Operations on the Mac

How the owner runs Aperture under launchd. Nothing here is installed from the cloud: the owner
renders the job files, reads them, and loads them.

## Jobs

| Job | Label | When | Relaunch |
|---|---|---|---|
| preflight | `local.aperture.preflight` | `[runtime] preflight_start_local` on `launch_weekdays` | never |
| bot | `local.aperture.bot` | `[runtime] bot_start_local` on `launch_weekdays` | never (a crash raises an alert) |
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
5. Broker token folder `<data_dir>/<token_dir>` exists.

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
3. Data and token folders: `mkdir -p data/<token_dir>` (see `[environments.*] token_dir`).
4. Notifications: run `.venv/bin/python -m aperture.runtime alert preflight-failed "test"` once.
   macOS shows notifications from `osascript` under "Script Editor"; allow them in System Settings >
   Notifications if nothing appears. The JSON line says `"delivery": "shown"` when osascript
   succeeded.
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
# Read the three files in data/launchd, then:
cp data/launchd/local.aperture.preflight.plist ~/Library/LaunchAgents/
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/local.aperture.preflight.plist
```

Load `local.aperture.bot.plist` and `local.aperture.api.plist` the same way once their commands
exist. After changing `[runtime]` values, render again, then `launchctl bootout` and `bootstrap`
the changed jobs.

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
Keep the Mac awake on trading days (power adapter, "Prevent automatic sleeping" in Battery
settings) or schedule a wake before the preflight time, for example
`sudo pmset repeat wakeorpoweron MTWRF 08:20:00` (Mac local time).

## Alerts

`python -m aperture.runtime alert <kind> <text>` shows one alert. Kinds (spec section 11):
`unprotected-position`, `stop-rejected`, `unknown-order-status`, `foreign-exposure`,
`broker-reads-failing`, `stream-lost-with-position`, `daily-ceiling-reached`, `bot-crashed`,
`bot-did-not-start`, `bot-did-not-stop` (added in review), `preflight-failed`, `token-needs-confirmation`. The same kind and text inside
`[alerts] dedupe_window_seconds` is shown once; every occurrence is still recorded (in the bot's
journal, or in the job log for these commands).
