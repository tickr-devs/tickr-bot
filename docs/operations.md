# Operations on the Mac

How the owner runs Tickr under launchd. Nothing here is installed from the cloud: the owner
renders the job files, reads them, and loads them. Manual trades and the Sandbox probe day
(stop the bot job first) are in docs/manual-trade.md.

## Jobs

| Job | Label | When | Relaunch |
|---|---|---|---|
| preflight | `local.tickr.preflight` | `[runtime] preflight_start_local` on `launch_weekdays` | never |
| bot | `local.tickr.bot` | `[runtime] bot_start_local` on `launch_weekdays` | never (a crash raises an alert) |
| watchdog | `local.tickr.watchdog` | at load and `[runtime] watchdog_start_local` on `launch_weekdays`; then runs at every hour, nights and weekends too | launchd keeps it alive |

The bot job has one starter besides its schedule: near the flatten time the watchdog may start it once a day (see "Watchdog", `watchdog-restarted-bot`).
| test-alert | `local.tickr.test-alert` | only on `launchctl kickstart` (grants the Messages permission, see below) | never |
| nightly-fetch | `local.tickr.nightly-fetch` | `[runtime] nightly_fetch_start_local` on `launch_weekdays` (after the close); stops itself before the next open | never |
| api | `local.tickr.api` | at login, always on, loopback only; with `[chart] webull_enabled` it holds the app key and secret only (see "The API job and the Webull keys") | launchd keeps it alive |

Times in `[runtime]` are the **Mac's local time**. The calendar decides whether there is a
session: on weekends, market closures and half days (unless `half_days_enabled`) preflight reports
"no session today" and the bot job exits without starting the bot. If the Mac is not on New York
time, check the start times around daylight-saving changes (US and other countries switch on
different dates). The bot starts at the 04:00 ET pre-market open and runs through the
post-market to the 20:00 ET extended close: outside the regular session it only records the
screener lists (see "Screener lists outside the regular session") and never enters.

The bot job runs `python -m tickr.runtime run-bot`, which runs preflight again, then starts the
bot as a child process. launchd stops the job with SIGTERM; the supervisor forwards one SIGTERM to
the bot, whose handler only sets the stop-entries flag; the main loop then cancels working entries,
leaves resting stops at the broker and exits. If the bot is still running
`stop_alert_after_seconds` after the SIGTERM, the supervisor alerts `bot-did-not-stop` and keeps
waiting; launchd sends SIGKILL at `bot_exit_timeout_seconds`.

## Stopping the bot safely

1. `launchctl kill TERM gui/$(id -u)/local.tickr.bot`
2. Before logging out, shutting down or `launchctl bootout`, wait for the `"record": "bot-exit"` line in `~/Library/Logs/Tickr/bot.log`
   (`"clean": true`). A `bot-did-not-stop` alert means the bot is stuck: check orders and the
   position in the Webull app before doing anything else.
3. Do not log out, restart or shut down the Mac while the bot has a working order or an open
   position. Logout and shutdown also stop the job with SIGTERM, but macOS does not fully honor
   `ExitTimeOut` then. A resting stop stays at the broker either way; a working entry order may not be
   cancelled.

`tickr.cli run` (the bot job's command) recovers and reconciles, keeps the positions it owns
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
to close, then stop-bot). With positions open the apps ask "Close and stop / Leave open":
mode `close_and_stop` closes them in the bot and stops once flat (refused, bot running, if a
close fails), mode `leave_open` stops at once and nothing closes them afterwards (docs/api.md,
stop-bot "Modes"). A
SIGTERM does not have this check: it leaves an open position under its DAY stop, and nothing
flattens it while the bot is down. When obeyed, the bot writes `data/<[runtime] stop_marker_file>` and every later start is refused (preflight
cause `stopped-by-operator`, with an alert) until you run `tickr clear-stop`, or press Start in the app (`POST /commands/start-bot`,
which removes the marker and runs `launchctl kickstart` of the bot job). See docs/api.md,
"Commands" and "Start-bot".

## Exit codes

`python -m tickr.runtime preflight` and `run-bot`:

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
1b. The running code version is known (cause `code-version-unknown`): on the Mac the git commit
   of the checkout the code runs from (`-dirty` when a tracked file under `src/` or `strategies/` differs from it; config edits are tagged by the run's config digest instead), in the Docker image the
   `TICKR_CODE_VERSION` it was built with (`docs/deploy-server.md`). `tickr run`, `trade` and the
   stop-replace probe refuse to start without it too: every run and every trade records it.
   `resume`, `exit-all` and `clear-fence` never refuse for it (closing a position must always
   work): they record the run without a version and print the cause.
2. Data folder exists; free disk on it at least `[runtime] min_free_disk_mb`.
3. Calendar knows today (New York date).
4. Credential variables named in the active `[environments.*]` table are set. Only the names are
   checked and reported, never the values.
5. There is an alert channel (cause `alert-channel-missing`): Telegram is set up, that is
   `[alerts] bot_token_variable` or `chat_id_variable` is set. The same on every operating
   system; without it the start is refused, because nobody could be told. Presence only, never
   the value.
6. When Telegram is set up, both of its variables are set (cause `alert-telegram-missing`).
7. Broker token folder `<data_dir>/<token_dir>` exists (cause `token-dir-missing`) and is closed to
   other accounts on the machine, no group or other permission bits (cause `token-dir-open`;
   `chmod 700` it). The SDK writes its token file there with default permissions. Only POSIX mode
   bits are checked, not macOS ACLs, which is enough on a single-owner Mac.
7b. No operator stop marker (cause `stopped-by-operator`): a stop-bot command from the apps
   left `<data_dir>/<[runtime] stop_marker_file>`; `tickr clear-stop` removes it.
7c. The database, when it exists, holds runs of this environment only (cause
   `database-other-environment`; `database-unreadable` when it cannot be opened). After
   changing `[run] environment`, move `data/tickr.sqlite3` aside and keep it: the apps then
   no longer show the other environment's days.
8. The active strategy builds and `[market_data] history_sessions` covers its history need. A
   failure is reported, but the bot still starts with entries off so it can protect positions.
9. Broker token usable (only when no earlier check blocks the start and today has a session):
   the Webull SDK creates or checks the token. If Webull says it is PENDING, preflight alerts `token-needs-confirmation`
   and waits up to `[environments.*] token_check_duration_seconds` for you to approve it in the
   Webull app. INVALID, EXPIRED, not approved in time, or any other token error fails the check
   with cause `token-unusable`. Where Webull has token checking off, this is one config read.
   `tickr run` alerts the same way (journaled as an `alert` record) if its token waits.

All checks are reported; the first failure is the recorded cause.

## One-time setup

1. Clone the repo and create the venv (the developer checkout; launchd runs releases, see
   "Releases"):
   ```sh
   cd ~/Development/Tickr
   python3.12 -m venv .venv && .venv/bin/pip install -e ".[webull]"
   ```
2. Credentials file outside the repo, readable only by you. One `NAME=value` per line, using the
   variable names from `config/bot.toml` (for the active environment):
   ```sh
   mkdir -p ~/.config/tickr && touch ~/.config/tickr/env && chmod 600 ~/.config/tickr/env
   open -e ~/.config/tickr/env
   ```
   The jobs load it with `sh` (`set -a; . file`), so Tickr code never reads the values. Quote a
   value with single quotes if it contains spaces or `$`.
   To test by hand in Terminal: `set -a; . ~/.config/tickr/env; set +a` first.
   Telegram is the alert channel and its two variables are required: create a bot with
   @BotFather, put its token and your chat id in this file under the names in
   `[alerts] bot_token_variable` and `chat_id_variable` (`TICKR_TELEGRAM_BOT_TOKEN`,
   `TICKR_TELEGRAM_CHAT_ID`). They are secrets and personal data: never put them in the repo or
   in a plist. **Deadline:** they must be in this file before the first preflight run (03:30
   on Monday with the default `[runtime] preflight_start_local`). Without them preflight fails
   with `alert-channel-missing` (neither set) or `alert-telegram-missing` (one of the two) and
   the bot job does not start the bot. Critical alerts always go to Telegram; the rest follow the
   routing in the app (channel `telegram`).
   Optional, push to the Tickr apps (APNs). In Apple Developer > Certificates, Identifiers &
   Profiles > Keys, create a key with Apple Push Notifications service and download its
   `AuthKey_<key id>.p8` (Apple lets you download it once). Keep it outside the repo, the file
   mode 0600 in a folder of mode 0700:
   ```sh
   install -d -m 700 ~/.config/tickr/apns
   mv ~/Downloads/AuthKey_<key id>.p8 ~/.config/tickr/apns/ && chmod 600 ~/.config/tickr/apns/*.p8
   ```
   Then add three lines to the env file (placeholders here; never put the real ids in the repo),
   under the names in `[alerts] apns_key_id_variable`, `apns_team_id_variable` and
   `apns_key_path_variable`:
   ```sh
   TICKR_APNS_KEY_ID=<key id>
   TICKR_APNS_TEAM_ID=<team id>
   TICKR_APNS_KEY_PATH=<path to AuthKey_<key id>.p8>
   ```
   None set = push is off. Preflight then fails with `alert-push-missing` if one of the three is
   not set, `alert-push-key-unreadable` if the path is not a readable file and
   `alert-push-key-invalid` if it is not a P-256 `.p8` key; the key is read only to check and
   sign it, never logged. Push is an extra channel only: it never counts as the alert channel
   preflight requires (that is Telegram), because it reaches nobody until a phone has
   registered. Until then each process journals `apns-no-device` once, not per alert. Each app
   registers its device at `POST /push-token`; critical alerts always go to every registered
   device as time-sensitive, the rest follow the routing in the app (channel `push`). The API
   job never gets these variables (it does not push). On a server (docs/deploy-server.md) the
   three names go into the env file the same way, and the key must be inside the container:
   put it in the data folder (`/app/data/apns/`, folder 0700, file 0600, owned by the service
   account) and set `TICKR_APNS_KEY_PATH` to that path. The bot, preflight and watchdog read
   the variables at their next start.
3. Data and token folders: `mkdir -p data && mkdir -m 700 data/webull-token-sandbox`
   (`[environments.sandbox] token_dir`; `data/webull-token-production` before Production). A folder
   that already exists keeps its mode: `chmod 700 data/webull-token-sandbox` instead.
4. Alerts: after loading the env file as above, run
   `.venv/bin/python -m tickr.runtime alert preflight-failed "test"` once. You should get a
   Telegram message. The send runs on a background thread, so the bot's loop does not wait for
   the network (at most `[alerts] notification_timeout_seconds`). The first JSON line says the
   alert is on its way (`"delivery": "sending"`, `"pending": ["telegram", "feed"]`); one line
   per channel follows (`"channel": "telegram"`) with its result (`"delivery": "shown"` or
   `"failed"`). A failed send is never retried; the next alert tries again.
5. Run preflight by hand: `.venv/bin/python -m tickr.runtime preflight; echo $?`.

## Install the jobs

The jobs run from one folder, given as `--release`: the link `~/Development/Tickr-releases/live`
that `release` switches (see "Releases" below; make the first release before installing). Each
job resolves the link once when it starts (`cd -P`) and runs that folder's `.venv/bin/python`
and its `config/bot.toml`, so a switch never changes the code under a running job, and a
developer `git pull` in `~/Development/Tickr` never reaches a job at all.

```sh
cd ~/Development/Tickr
mkdir -p ~/Library/Logs/Tickr
.venv/bin/python -m tickr.runtime render-launchd \
    --release ~/Development/Tickr-releases/live \
    --env-file ~/.config/tickr/env \
    --log-dir ~/Library/Logs/Tickr \
    --out data/launchd
# Read the six files in data/launchd, then:
for job in preflight bot watchdog api test-alert; do
  cp data/launchd/local.tickr.$job.plist ~/Library/LaunchAgents/
  launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/local.tickr.$job.plist
done
```

`render-launchd` refuses (exit 2) when the env file is missing or has any group or other
permission bit: `chmod 600` it first.

### The API job and the Webull keys

The API is the one job that listens on the network (loopback, and the remote listener when
configured), so it gets as little as possible. While `[chart] webull_enabled` is false it gets no
credential: its plist never names the env file. While it is true, the chart client in the API
(GET /chart) needs the active environment's Webull app key and app secret, and the API job gets
exactly those two variables, named by `[environments.<env>] app_key_variable` and
`app_secret_variable` of `[run] environment`, and nothing else: never `account_variable` (the
chart client reads no account id), never another environment's keys, never the alert recipient,
the advisor key or any other line of the env file. Its shell reads each of the two in a
subshell that sources the env file and prints only that one value (the file's own output is
discarded), then exports it; the API process itself never reads the env file. No process the
API starts gets any credential variable: the Lab replay child, the start-bot command
(`launchctl kickstart`; the bot job sources the env file itself, launchd never hands it the
caller's environment) and the alert's send.

At its start the API checks the credential variables it holds (names only). Any beyond those
above (any at all with the chart off; the account id, another environment's keys or any other
credential with it on) raise the `api-credentials-unexpected` alert and a line in api.log
naming them; the API still serves. Its plist is then stale or edited: render and reinstall it as
below. The API writes this alert to the alerts feed file only: it has no Telegram variables
(they are credential variables it must not hold).

The variable names are fixed when the plist is rendered: after changing `[chart]
webull_enabled` or `[run] environment`, render again and reinstall the API job only:

```sh
cd ~/Development/Tickr
.venv/bin/python -m tickr.runtime render-launchd \
    --release ~/Development/Tickr-releases/live \
    --env-file ~/.config/tickr/env \
    --log-dir ~/Library/Logs/Tickr \
    --out data/launchd
# Read data/launchd/local.tickr.api.plist (it names the two variables, never a value), then:
launchctl bootout gui/$(id -u)/local.tickr.api
cp data/launchd/local.tickr.api.plist ~/Library/LaunchAgents/
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/local.tickr.api.plist
```

`bootstrap` starts the API at once (`RunAtLoad`); the bot and the other jobs are not touched.

The nightly fetch is loaded on its own, and only when no other history fetch is running on the
Mac: both draw on the same Webull request budget. Its first run seeds `nightly/current/` from a
finished `data/backtest` folder; until then the Lab replay refuses (its folder does not exist).
Run the seed once by hand, outside market hours, then load the job:

```sh
set -a; . ~/.config/tickr/env; set +a
.venv/bin/python -m tickr.backtest --config config/bot.toml nightly \
    --out data/nightly --seed data/backtest
cp data/launchd/local.tickr.nightly-fetch.plist ~/Library/LaunchAgents/
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/local.tickr.nightly-fetch.plist
```

After a release that changed `[runtime]` times or `ops/launchd/` (the release prints the
`git diff --stat` of both), render again, then `launchctl bootout gui/$(id -u)/local.tickr.<job>`
and `bootstrap` the changed jobs.

## Releases

What launchd runs is a **pinned release**: a detached git worktree of one exact commit of
`origin/main`, with its own environment, never the developer checkout.

```
~/Development/Tickr-releases/<sha>/      worktree at <sha>; .venv from `uv sync --frozen --extra webull`
                                         (tickr is imported from <sha>/src); data -> ~/Development/Tickr/data
~/Development/Tickr-releases/live        -> <sha>   what every job runs
~/Development/Tickr-releases/previous    -> <sha>   the release before (rollback)
```

- **One data folder.** Every release's `data` is a link to `~/Development/Tickr/data` (the folder
  the `--config` given to `release` resolves): the same database, capture, decisions, history,
  token folder, remote hosts, pairing, status and account files. It is the Docker layout (the
  data volume mounted at `<root>/data`) done with a link; the code still addresses data only as
  `<root>/[run] data_dir`. `release` refuses a data folder without `tickr.sqlite3`, so a release
  never starts on a new, empty one, and it never copies data.
- **Config.** A release reads its own committed `config/bot.toml`: a config change is a commit
  on `origin/main`, tagged by its digest on every run. Changes made only in the checkout never
  reach the bot; `release` refuses while the checkout it runs from has uncommitted changes under
  `config/`. It prints the running and the new config (path and SHA-256) and the `git diff --stat`
  of `config/` and `ops/launchd/` between the two commits; preflight's JSON line names
  `config_path`, `config_digest` and `data_dir` (resolved).
- **Code version.** `src/tickr/version.py` reads the commit of the folder above `src/`, the
  release worktree (its `.git` is a file): the exact `<sha>`, never `-dirty` (the data link is
  untracked and does not count).

Make a release, after the close or on a non-trading day, flat (it refuses otherwise):

```sh
cd ~/Development/Tickr
.venv/bin/python -m tickr.runtime --config config/bot.toml release \
    --releases ~/Development/Tickr-releases --env-file ~/.config/tickr/env   # [--commit <sha>]
```

Each step is printed. In order, and a refusal at any step before the switch leaves the running
release, the links and the jobs exactly as they were (exit 1, `refused: ...`):

1. Refuses when `live` exists and its `data` resolves to another folder than the data folder of
   the `--config` given (a command started from another checkout would check flatness in, and
   switch the bot onto, another database). Then refuses early on an open trade in the database,
   or when the bot's account file is at most `[orders] broker_read_max_age_seconds` old and
   shows a position. The account file never proves flat (it is refreshed only by a running bot).
2. Refuses on uncommitted config changes in the checkout; fetches `origin`; refuses unless the
   commit (default `origin/main`) is on `origin/main`.
3. Prepares `<sha>/` (worktree, data link, `uv sync`) and runs its own preflight exactly as the
   preflight job does (the env file sourced by `sh`); refuses unless it passes or says no session
   today, with code version `<sha>`, the release's config and the shared data folder. On a
   trading day this preflight checks the Webull token like the scheduled preflight run. On failure only the
   exit code of that step is printed, never the shell's error text (it could quote the
   credentials file).
   **Stop marker:** while the bot is stopped by the operator (`stopped-by-operator`), preflight
   fails and so does `release`; it is not passed over on purpose, because a release cannot
   prove the code it would start. To deploy then: after the close, `tickr clear-stop`, release,
   and, if the bot must stay stopped tomorrow, send stop-bot again from the apps once it runs
   (or `launchctl kill TERM` it and keep the scheduled start in mind).
4. Proves flat with a fresh read: `python -m tickr.runtime broker-flat` of the new release, run
   like a job (env file sourced by `sh`), reads positions and open orders once through the
   release's Webull adapter, read-only (never an order). Refuses when the broker shows any
   position or open order, when the database has an open trade, or when the read fails (the
   cause is printed). Then takes a verified backup of the database,
   `<data>/<[run] backups_dir>/pre-release-<UTC time>-<sha12>.sqlite3` (SQLite online backup
   from a read-only connection, schema-agnostic, `integrity_check` ok; a release removes older
   `pre-release-*`, a rollback removes none, so the backup from before the release it rolls
   back survives; the daily `tickr-<date>` backup is left alone) and refuses if the backup or
   a removal fails. Then points `previous` at the running release and switches `live` (an
   atomic rename of a new link).
5. Restarts the jobs that run: stops the watchdog first (so it never sees the stopping bot
   missing), then the bot, then the API, each with SIGTERM (`launchctl kill TERM`) and waited
   for (at most `[runtime] release_stop_wait_seconds`); then starts the bot, the API and the
   watchdog (`launchctl kickstart`). A job that is not running is left to its schedule, which
   starts the new release; one that did not stop is not started and is reported (exit 1). If
   the link rename itself fails, nothing is restarted and the message names where `live` and
   `previous` point.
6. Removes every release folder other than `live` and `previous` (keep only the latest backup),
   except one a running process still runs from (removed by the next release): its data link is
   unlinked first, then `git worktree remove` without force. The data folder is never touched.

`rollback` (same arguments, no `--commit`) switches back to `previous` the same way, flat only,
and needs no GitHub: it does not fetch (it still reads the broker in step 4), it verifies the folder (HEAD at its commit, no changes
to tracked files, its `data` link naming the data folder, its `.venv` present) and runs its
preflight. `live` and `previous` swap, so a rollback can itself be rolled back.

### Database migrations are one way

A release whose code adds a database migration (`MIGRATIONS` in `src/tickr/store.py`) upgrades
the shared database the first time its bot opens it, after a verified backup
`tickr.pre-v<N>.sqlite3` next to it. There is no downgrade:

- **Rollback past a migration is refused.** An older release's preflight refuses a database
  newer than its code (`... has schema 5, this code knows 4: update the code`), so `rollback` and the
  emergency switch-back fail. Going back means restoring the `pre-v<N>` backup by hand, which
  loses everything the newer bot recorded since; ask the owner first.
- **Install it together with the bot restart.** Until the new bot has migrated the database,
  the API of the new release reads the old schema and refuses history (`database schema v4,
  this API reads v5`); an old API on a migrated database refuses the same way. `release`
  restarts bot and API together; a hand restart must do both.
- Migration v5 (2026-11-16) adds `orders.exact_fill_price` (exact fill prices from the
  account's cash activities, read by the realized loss): one way like every migration.
- Migration v6 (2026-11-16) adds `orders.fees_usd` (the broker's fees per finished order, counted
  by the daily loss rule): one way as well.
- Migration v7 (2026-11-16) adds `orders.net_cash_usd` (an order's settled cash amount from the
  account's cash activities): one way as well.
- Migration v8 (2026-11-16) adds `trades.entry_rest_until` (until when a resting limit entry may
  work): one way as well.
- Migration v9 (2026-11-16) adds the trade's scale-out columns (`scale_out_quantity`,
  `reduce_quantity`, `reduce_cause`, `reduce_state`): one way as well.
- Migration v11 (2026-11-25) adds `orders.operator_resolved_at` (an order the operator resolved
  with `tickr resolve-order`): one way as well.

### Emergency: the owner switches back by hand

Only for the owner, only when `rollback` cannot run: it refuses while not flat, and flatness is
read from files that only a running bot refreshes, so a new release that opened a trade and
then cannot start blocks it even after the position is closed by hand in the Webull app.

1. In the Webull app: close the position and cancel every open order; check both are gone.
2. Stop the jobs, the watchdog first, and wait for each to end (`launchctl list <label>` shows
   no `"PID"`):
   ```sh
   for job in watchdog bot api; do launchctl kill TERM gui/$(id -u)/local.tickr.$job; done
   ```
3. Repoint `live` at the previous release by hand (`R=~/Development/Tickr-releases`):
   ```sh
   cd $R && old=$(readlink live) && back=$(readlink previous)
   ln -sfh "$back" live && ln -sfh "$old" previous   # -h: replace the link, never write into its folder
   readlink live   # the previous commit
   ```
4. Run its preflight (`$R/live/.venv/bin/python -m tickr.runtime --config
   $R/live/config/bot.toml preflight` with the env file sourced), then start the jobs:
   `launchctl kickstart gui/$(id -u)/local.tickr.api`, then the bot (only if wanted today), then
   the watchdog. The bot's start reconciles with the broker and rewrites its account file.

### One-time switch-over (the coordinator, after the close, flat)

Today the jobs run from `~/Development/Tickr` and its pip venv. Once, after the market close
with the bot flat and stopped:

1. `cd ~/Development/Tickr && git status --short config/ && git pull --ff-only`: no config
   changes left in the checkout (commit them first).
2. Make the first release (the command above). It creates `live`, but the installed plists
   still name the checkout, so every job (and the API it restarts) still runs the checkout until
   step 4.
3. Check it: `readlink ~/Development/Tickr-releases/live` is the commit;
   `~/Development/Tickr-releases/live/.venv/bin/python -m tickr.runtime --config
   ~/Development/Tickr-releases/live/config/bot.toml preflight` prints `"config_path"` under
   `Tickr-releases/<sha>/`, `"data_dir": ".../Tickr/data"` and code version `<sha>`.
4. Render from the release itself, so its templates and `[runtime]` times are used:
   ```sh
   L=~/Development/Tickr-releases/live
   $L/.venv/bin/python -m tickr.runtime --config $L/config/bot.toml render-launchd \
       --release $L --env-file ~/.config/tickr/env --log-dir ~/Library/Logs/Tickr \
       --out ~/Development/Tickr/data/launchd
   ```
   Read the files, then for each installed job `launchctl bootout gui/$(id -u)/local.tickr.<job>`, copy
   the new plist to `~/Library/LaunchAgents/` and `launchctl bootstrap` it (bot last, and only
   when it is not running).
5. `launchctl kickstart gui/$(id -u)/local.tickr.test-alert`: the Telegram alert arrives.
6. From now on deploy only with `release`; a `git pull` in the checkout changes nothing that
   runs. To go back to the old setup: render with `--release ~/Development/Tickr` and reinstall.

### Alerts from launchd

`launchctl kickstart gui/$(id -u)/local.tickr.test-alert` sends a test alert through the same
program chain as the bot and watchdog jobs (`/bin/sh`, then the venv python). Check
`~/Library/Logs/Tickr/test-alert.log`: the `"channel": "telegram"` line must say
`"delivery": "shown"`. The job has no schedule, so it can stay installed; kickstart it again
after recreating the venv. No operating-system permission is needed: Telegram is a plain HTTPS
call.

## Everyday commands

```sh
launchctl print gui/$(id -u)/local.tickr.preflight      # state and last exit code
launchctl kickstart gui/$(id -u)/local.tickr.preflight  # run preflight now
launchctl kill TERM gui/$(id -u)/local.tickr.bot        # stop the bot cleanly
launchctl bootout gui/$(id -u)/local.tickr.bot          # remove a job
tail -f ~/Library/Logs/Tickr/bot.log
```

To look at the apps with a full made-up day, turn on "Demo day" in their Settings: the apps carry
it with them and need neither this Mac nor the bot (docs/demo.md). To serve the same day from a
real bot on a simulated exchange (never Webull, its own folder): `tickr demo`, same document.

News delay probe (Codex items 9-12, backlog R34; read only, needs only the Massive key): in a
Terminal, `set -a; . ~/.config/tickr/env; set +a; tickr probe-news-delay`, left running all day
(Ctrl-C stops it). Every `[massive] news_probe_poll_seconds` it lists Massive's general news, Benzinga news and
earnings and appends each new or changed record, with its published / last_updated time and our
receipt time, to `<data_dir>/massive/news-probe/<New York date>.jsonl`. Delay of a `new` record =
`received_utc - published` (at most; it arrived after `previous_poll_sent_utc`). Lines of the
first poll are `baseline` and time nothing.

Revision poller (Codex 108 point 7; off): with `[massive] revision_poll_enabled = true`, `tickr
poll-revisions` (same environment as the probe) keeps every version of each Benzinga earnings report
and news article it receives under `<data_dir>/<revision_store_dir>`, with the poll coverage in
`coverage/`. A failed poll or a restart leaves a typed gap there; `revisions.write_l1bz` refuses a
range that touches one.

The demo day had a launchd job of its own (`local.tickr.demo`) until the apps carried the day
themselves. If it is still installed on this Mac, remove it once:

```sh
launchctl bootout gui/$(id -u)/local.tickr.demo 2>/dev/null
rm -f ~/Library/LaunchAgents/local.tickr.demo.plist
```

## Feed-arrival measurement (one trading day)

Read-only: no orders, no model. It records when Massive's minute bars (every symbol, every
correction) and Webull's three lists (request sent, answer received, members) reach this Mac,
plus every stream drop (Codex 102, point 1). Start it before 04:00 ET in its own terminal; it
ends by itself at 20:05 ET (`[massive] arrival_tail_seconds` after the close), or on Ctrl-C.
It reads the lists at the bot's own cadence and budget (3 screener requests a minute); it may run
next to the bot (a second market-data client, C1 passed in Sandbox), but only one process should
hold a Massive stream, so nothing else may stream Massive meanwhile.

```sh
cd ~/Development/Tickr && source .venv/bin/activate && set -a && . ~/.config/tickr/env && set +a && tickr probe-feed-arrival
```

The capture is `data/massive/arrival/<date>/arrival-<HHMMSS>.jsonl.gz` (a restart adds a file;
the report reads them all). The next morning, once Massive has published the day's flat file:

```sh
tickr report-feed-arrival 2026-11-27 > arrival-2026-11-27.txt
```

It prints per session the bar delay after the minute's end, what share of the file's
symbol-minutes had arrived at each `[massive] arrival_report_points_seconds` (present, late,
lost in a gap, never streamed; minutes without a trade are not in the file and are never counted
as missing), corrections, list latency and, for each list member, whether its bar for the
decision minute had arrived. `--flat-file PATH` uses a file already on disk. The report points
are report columns only; no guard reads them. Keep the Mac's clock synchronised (Date & Time:
set automatically): delays are measured on its wall clock.

When the run reaches the extended close it also keeps the stream-built day (Codex 102, point 3):
`data/massive/stream_days/<date>.csv.gz` (the flat file's bar columns as the model's `DayBars`
holds them at the end, plus each row's first and last receipt and its correction count) and
`<date>.gaps.json` (drops, and the hours the recorder was not running). Once the flat file is
published, compare the two (report only):

```sh
tickr compare-stream-day 2026-11-27 > stream-day-2026-11-27.txt
```

Per session it counts symbol-minutes equal in every column, different (which column, by how
much), only in the stream and only in the file, with the minutes a gap may have touched counted
apart.

## Disk upkeep: `tickr maintain`

What grows under `data/`, per environment and New York date: `capture/<env>/<date>/`
(`quotes.jsonl` is by far the largest: every stream quote; then `bars.jsonl`, `screener.jsonl`,
`notices.jsonl`) and `decisions/<env>/<date>/decisions.jsonl`. The database (`tickr.sqlite3`)
grows slowly and is never pruned: the journal is the audit trail. `history/` is a cache of
prior-session bars, the backup folder keeps one file, `watchdog.jsonl` gets a few lines a day;
launchd's logs in `~/Library/Logs/Tickr` are not ours to rotate.

Never run it during the trading session, not even as a dry run: it takes the bot's
single-instance lock and writes its own pid into the lock file, so a bot job starting meanwhile
is refused and the watchdog and `status` see a pid that is not the bot. Run it with the bot
stopped, after the close or on a non-trading day:

```sh
.venv/bin/python -m tickr.cli maintain          # dry run: prints what it would archive and why it keeps the rest
.venv/bin/python -m tickr.cli maintain --apply  # does it
```

- Each `*.jsonl` of a finished day folder is gzipped in place to `*.jsonl.gz` (written, read back
  and compared, then renamed; the plain file goes last). Research, the API's `/bars` and `/days`,
  and `export_replay_bars` read both forms; give research tools the plain name or the `.gz` name.
- A day folder is kept (one printed cause) when it is within the last `[run]
  maintain_keep_sessions` sessions before today (today included), on or after the day of a trade
  still open in the database, without its `day-end` journal row, or when a plain file sits beside
  a `.gz` that is unreadable or holds other bytes (look at both, remove the wrong one).
- Chart cache (`chart/<env>/<SYMBOL>/<date>.json`, written by the API): day files older than
  `[chart] cache_max_age_days` are deleted (the dry run counts them); symbol folders stay.
- History cache (`history/<env>/<date>/`, the bot's prior-session bars): day folders older than
  `[market_data] history_cache_max_age_days` are deleted whole; the bot only reads today's
  folder. A folder holding anything but cache files (`<SYMBOL>-<n>.jsonl`, `.partial`), a
  symlink or a subfolder is kept with a printed cause.
- Old archives: only when `[run] maintain_archive_max_age_days` is above 0 (shipped 0: never).
  A capture or decision day folder older than that many days and outside the last
  `maintain_keep_sessions` sessions, whose files are all `*.jsonl.gz`, is deleted, under the same guards as archiving (open trade, missing `day-end`). Captured days
  feed replay, research and the advisor: deleting them loses that evidence for good.
- Both delete steps are planned before anything is archived, so the dry run lists exactly what
  `--apply` deletes, and today is never touched.
- `--apply` also runs a SQLite WAL checkpoint; it deletes no row and never vacuums.
- Refused (exit 3, nothing touched) while the bot holds its lock, when the calendar cannot count
  the sessions, or when the database is missing or has another schema. Exit 1 means one file
  failed to archive or delete: the run stopped there and what is left stays for the next run.

## Diagnostics bundle: `tickr diagnostics`

Read-only and safe while the bot runs. It writes one zip you can send on without reading it
first:

```sh
tickr diagnostics --out ~/tickr-diagnostics.zip [--days N]     # --out must not exist yet
```

`--days` defaults to `[run] diagnostics_days` (7). The zip holds, from an explicit allow-list
(`src/tickr/diagnostics.py`): `version.txt` (the code version), `config/<name>.toml` (the config in
use and the repo's known config files next to it; no other file of that folder), `logs/*.log` and rotated `*.log.N` files from
`<data_dir>/<[run] diagnostics_logs_dir>` changed in the last N days, `alerts.jsonl` (the feed
lines of the last N days), `database.json` (schema version and the row count of each table, never
a row), `status.txt` (what `tickr status` prints) and `manifest.json` (every file with its size).
It never opens the environment, env files, the secrets file, token folders, authorization files,
APNs keys, the account file, capture or decision files. On the Mac the launchd logs live in
`~/Library/Logs/Tickr`, outside `data_dir`: copy `bot.log` into `data/logs/` first if the bundle
should carry it, or send it separately. On the server see docs/deploy-server.md, "Diagnostics
bundle". Preflight is not part of the bundle (it reads the environment); run `tickr status` or
the preflight job as usual.

## The Mac must be awake

launchd runs a calendar job missed during sleep when the Mac wakes, which may be after the open.
Schedule a wake before the preflight time (`[runtime] preflight_start_local`, 03:30), once:
`sudo pmset repeat wakeorpoweron MTWRF 03:20:00` (Mac local time). It replaces the earlier
08:20 wake: `pmset -g sched` shows the one schedule in force. Without it the bot starts only when
the Mac wakes, and the pre-market screener lists before that are not recorded.

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
- The watchdog job runs at every hour and starts `caffeinate` beside itself like the bot job, so
  on the power adapter the Mac stays awake around the clock, weekends too: it must be awake to
  notice a crash or a full disk (check with `pmset -g assertions` after installing).

## Alerts

`python -m tickr.runtime alert <kind> <text>` shows one alert. Kinds (spec section 11):
`unprotected-position`, `stop-rejected`, `unknown-order-status`, `foreign-exposure`,
`broker-reads-failing`, `stream-lost-with-position`, `daily-ceiling-reached`, `bot-crashed`,
`bot-did-not-start`, `bot-did-not-stop` (added in review), `preflight-failed`, `token-needs-confirmation`,
`exit-refused`, `observers-stopped`, `strategy-not-loaded` (no entries this run: the active
strategy or its settings version was refused), `not-flat-after-flatten`, `exit-not-filled`,
`flatten-after-close`, from the API `api-credentials-unexpected`, `risk-command-used` and `device-session` (feed only, see
"The API job and the Webull keys") and `remote-auth-blocked` (an address used up its
failed-authentication budget on the remote port, docs/api.md; feed only), and from
the watchdog `watchdog-not-flat`, `watchdog-cannot-check`, `bot-down-with-open-trade`,
`watchdog-restarted-bot`, `watchdog-restart-refused`, `watchdog-restart-failed`, `bot-down`,
`disk-low`. Every alert goes to
Telegram (`[alerts] bot_token_variable` and `chat_id_variable`, see setup step 2) and to
the alerts feed below, unless the owner's routing turned its kind off on a channel (PUT
/alerts/routing, file `[alerts] routing_file` under `data_dir`; docs/api.md, "Notification
settings"). Critical kinds cannot be turned off, the feed always gets every alert, and deleting
the routing file turns everything back on.
The Telegram message is sent on a background thread, so the bot's loop never waits for the
network; its result is journaled as its own `alert` record (`"channel": "telegram"`) once known.
An alert counts as shown, for repeat suppression, when either channel delivered it. Every alert
is also journaled (the bot's, the watchdog's or the job log), whatever the channels did.
The same kind and text inside `[alerts] dedupe_window_seconds` is shown once; every occurrence is
still recorded (in the bot's journal, the watchdog's journal, or the job log for these commands).

### The alerts feed: Claude learns of an alert too

Every process that alerts (bot, supervisor, preflight, watchdog, API) also appends one JSON line
per alert, `{"at", "title", "text"}`, to `data/alerts.jsonl` (`[alerts] feed_file` under
`[run] data_dir`; the title holds the kind, as in the Telegram message). A Claude session
(the live-bot monitor) waits on that file in the background and wakes at the first new line, so
the project chat hears of a crash within seconds, not only the owner's phone:

```sh
f=~/Development/Tickr-releases/live/data/alerts.jsonl
n=$(wc -l < "$f" 2>/dev/null || echo 0)
until [ "$(wc -l < "$f" 2>/dev/null || echo 0)" -gt "$n" ]; do sleep 5; done
tail -n +"$((n + 1))" "$f"
```

It reads only that file (no credentials, no environment). After each wake it reports the new
lines and starts waiting again. The feed works only while that session runs; Telegram
does not depend on it. A feed line that cannot be written (a full disk) is recorded as
a failed `feed` channel like any other.

### `risk-command-used`

The API raises one of these for every accepted command that raises risk: `entry-stop` with
`off` (entries allowed again) and `start-bot`. The alert names the paired device (the name
`python -m tickr.api devices` lists, or `local` for the local port), the port (local or
remote) and the time; never a token. If you did not do this, the device's token is stolen or
the phone is not yours: revoke that device now (`python -m tickr.api revoke --name <device>`),
then pair again. Refused commands raise nothing. It goes to the alerts feed.

### `device-session`

The API raises one of these whenever a device signs in with tickr.bot (`POST /session`: the
device name, the tickr.bot account and app), signs out (`DELETE /session`), or is signed out by
the bot because a refresh token that was already used came back or its sign-in expired
(docs/api.md, `POST /session/refresh`). It names the device and the time, never a token or grant.
A sign-in you did not make means someone holds your tickr.bot sign-in or tickr.bot's signing key:
revoke the device now (`python -m tickr.api revoke --name <device>`) first, change your
tickr.bot password and passkeys, and set `remote_signin_accounts = []` until it is understood.
Emptying the accounts only refuses new sign-ins: a device already signed in keeps its tokens
until it is revoked, so revoking comes first. A device
signed out for a reused refresh token is either a copy of that token in use, or the app lost the
answer to its own refresh (a dropped connection); sign in again on the device, and if it happens
again without a network fault, treat it as a stolen token. It cannot be switched off and goes to
the alerts feed; the watchdog relays each one to Telegram, marked `RELAYED from the API:` with the
API's time (`runtime/watchdog.py`, `Sentinel.relay`).

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

### `quantity-mismatch`

When the account holds more of the bot's symbol than its orders explain (shares bought by hand),
an exit or the flatten sells only the bot's own shares; the trade then stays `exiting` until the
foreign shares are gone, so entries stay blocked, `stop-bot` is refused and the protection busy
cause says "exit in progress": sell or explain the extra shares in the Webull app.

### An `order-conflict`, `order-missing` or `status-unknown` that never ends

Some broker answers can never be settled by another read: a row that cannot be the bot's order
(`order-conflict:<order>` saying for example "broker shows 20 filled of 2"), an acknowledged
order that Detail says does not exist (`order-missing:<order>`), or a status the bot does not
know (`status-unknown:<order>`). The bot records nothing from such a row, its trade cannot
close, and entries stay blocked across restarts. To end it:

1. Look at the symbol in the Webull app. Close any shares in it and cancel every open order in
   it, so the account is flat in that symbol and the order is gone from the app's open orders.
2. Stop the bot (`launchctl kill TERM gui/$(id -u)/local.tickr.bot`).
3. `tickr resolve-order <order>`, with the order id the fence names (without `/listed`).
4. Start the bot again (`launchctl kickstart gui/$(id -u)/local.tickr.bot`).

The command does not reconcile or step first, so it sends no order. It reads the broker again
and refuses (exit 1, the cause printed, nothing changed) unless Detail still cannot settle the
order, every other live order of the trade is unsettled the same way or shown ended (recorded as
shown), and the account holds no shares and no open order in the symbol. Then it marks those orders resolved (the evidence is in
the journal as `order-resolved-by-operator`), clears the fences only a read could clear, and
closes the trade as "resolved by the operator". What the trade made or lost is unknown, so the
daily loss rule refuses new entries for the rest of that New York day; run it after the day's
trading when you can. If the broker later shows the order working again, the bot takes it back
as its own and fences `resurrected`.

### `refused: the database holds runs of environment ...`

One database belongs to one environment (spec section 10). After changing `[run] environment`,
move `data/tickr.sqlite3` aside (keep it) or point `[run] data_dir` at another folder; the
bot and the manual commands refuse to start on another environment's records.

## Fill to stop

While a trade has an entry working, filled shares not yet covered by a stop that Detail shows
working, or an exit in progress, the bot starts no discovery pass and runs no strategy: each
pass is only the Detail and positions reads and the protection step (a discovery pass already
running on its own thread finishes; it never touches orders). One `evaluation-skipped` row marks
the start of each such stretch. A loop pass whose market upkeep and strategy took longer than
`[runtime] evaluation_budget_seconds` journals `evaluation-slow`; discovery is not part of it.

Discovery runs on its own thread, one pass at a time, so a slow pass never delays Detail
polling, stops or commands. Each `discovery` row has the pass's duration in `seconds`; a pass
that takes longer than a minute skips the next minute's start. The candidates of a minute are
evaluated once its pass is collected (or at once when none started that minute); when the
minute ends first, an `evaluation-stopped` row with stage `candidates-late` and the minute's
`bar_start` says its candidates were not evaluated. On stop, a pass in flight ends before its
next request and is waited for at most `[runtime] discovery_stop_seconds`.

Before the open (from `[market_data] history_preload_minutes_before_open`, 9:00 ET) the bot
loads the prior-session history of the screener recorder's symbols (below), one batch of up to
20 symbols per loop pass. Each batch that settled symbols writes a `history-preload` row
(`symbols`, `loaded`, `refused`, `requests`, `queued`); a failed history read writes one with
`error` and the preload pauses `history_preload_retry_seconds` (a budget refusal pauses
silently). History refusals of the strategy's symbols are `history-refused` rows, once a
symbol a day.

The evaluation keeps at most `[market_data] history_memory_symbols` prior-session histories in
memory; one pushed out is read again from its cache file. A minute that read any back writes one
`history-reloaded` row (`bar_start`, `reloads`) once the next minute is evaluated. Such rows every
minute mean the limit is below what a minute asks: raise it. A cache file deleted during the day
is fetched again (request budget) the next time its history is not in memory.

### Screener lists outside the regular session

From the pre-market open to the regular open, and from the regular close to the extended close
(the calendar's hours), the bot reads the gainers, losers and top actives every `[market_data]
screener_record_seconds`, on the first loop pass of a minute, and appends them as they came to
`<capture>/<date>/screener-recorded.jsonl` (discovery's own reads in the regular session stay in
`screener.jsonl`). There the gainers and losers rank by the pre-market's or the post-market's
change (`[market_data] premarket_gainers_losers_rank_type`,
`postmarket_gainers_losers_rank_type`), and each record says which in `session`. A list not
read, because the request budget was spent or the read failed, is written to `events.jsonl` in
the same folder as one `screener-skipped` row per list: `kind`, `cause` (`budget` or the error's
type) and `source` (`recorder` or `discovery`). The same file has a `bot-start` and a `bot-stop`
row for each run (`run_id`, `code_version`, the stop's `cause`), so a gap in the lists can be
told apart from a bot that was not running.

### Instrument profiles of the listed stocks

With each round of lists (a discovery pass, or a recorder round), the bot reads the instrument
profiles of the listed instruments and appends them to `profiles.jsonl` in the same folder:
`shortable`, `easy_to_borrow`, `marginable` and the short margin requirements, as Webull sent
them (Webull states no borrow quantity). Each instrument is read once a day when first listed,
and again every `[market_data] profile_refresh_seconds` while it is a loser or a gainer up at
least `profile_refresh_gainer_move_percent`. At most `profile_max_requests_per_round` requests
are sent a round, leaving `profile_reserve_requests` of the route free for discovery. A refused
or failed request ends its round with a `profiles-skipped` row in `events.jsonl` (that request's
`symbols`, its `cause`, and how many more were `waiting`); the round cap alone writes one with
cause `round-cap`. What was not read is asked again in the next round. The reads run on a thread
of their own, after a discovery pass is collected: neither the pass, its candidates nor the loop
waits for them. A capture that cannot be written is a `capture-failed` journal row (`what`:
`screener-recorder` or `profile-recorder`); the bot goes on.

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

`[session] flatten_grace_seconds` after the flatten time (15:45 ET on a normal day), the bot
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
3. When you have looked, `tickr clear-fence exit-not-filled:<trade>`. If the trade is still
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
command record says "no closing order is sent now"); `tickr exit` and `tickr resume`
stop with exit 4 and print the fence.

1. Open the Webull app: the position and its stop. The stop is a DAY order and expires at the
   close; after that the position has no broker-side stop.
2. Close it in the Webull app (extended hours, or at the next open), or place your own stop
   for the next session.
3. The fence does not clear itself, so nothing is sold at the next open on its own. If you
   want the bot to close the trade instead, `tickr clear-fence market-closed:<trade>`
   during the regular session: the bot then sends one closing order. Cleared outside the
   session, the fence is set again at once with the same alert.

## Watchdog

The bot's own day-end check dies with the bot. The protective stop is a DAY order and launchd
never relaunches a crashed bot, so after a crash nothing would flatten at 15:45 ET and the
position would go overnight without a broker-side stop. The watchdog job (`python -m tickr.runtime watchdog`)
is a separate process that covers this:

- It runs at every hour: launchd starts it at load (login, install), at
  `[runtime] watchdog_start_local` (at or before the bot's start) and again whenever it ends
  (`KeepAlive`). Each New York day it takes the day's checks from the calendar; weekends and
  market closures have no broker checks and no alert for that. Half days: it checks (a position
  after the flatten time is wrong on any day).
- At every hour, also nights and weekends, every `watchdog_bot_check_seconds`: `bot-down` and
  `disk-low` (below).
- It never takes the bot's lock, opens the bot's database read-only, and only ever reads
  positions and open orders: it cannot place, cancel or replace an order.
- Its times come from the calendar, not the clock: `watchdog_after_flatten_seconds` after the
  flatten time (15:54 ET on a normal day) and `watchdog_before_close_seconds` before the
  regular close (15:59 ET). Preflight refuses values that do not fall after the bot's own
  day-end check and before the close. Started late (the Mac woke after these times), it checks
  once at once.
- One restart a day (below): from `[runtime] watchdog_restart_before_flatten_seconds` before
  the flatten time (15:40 ET on a normal day), if the bot is down while its database shows an
  open trade, the watchdog starts the bot job once. It still sends nothing itself.
- Every check and alert is one JSON line in `data/watchdog.jsonl` (`[runtime]
  watchdog_journal_file`) and in `~/Library/Logs/Tickr/watchdog.log`.
- It does not exit on its own; when it ends (a crash, a release stopping it) launchd starts it
  again.
- With no alert channel (Telegram not set up) it writes a `watchdog-alert-channel-refused`
  line (`alert-channel-missing`), still runs and journals, and never exits 0, so the scheduler
  shows a failed job.

### `bot-down`

At any hour the bot is not running (no live pid in its lock file, or a status file older than
`[runtime] watchdog_status_stale_seconds`: a hung bot counts) while its last status is not
`stopped`: it crashed, was killed, the Mac rebooted, or it hangs. A clean stop (a release, the
operator's stop, launchd's SIGTERM) writes `stopped` and stays quiet, and so does `stopping` until
`[runtime] bot_exit_timeout_seconds` (the supervisor alerts `bot-did-not-stop` meanwhile). One alert
per crash (per last status); a bot that starts and crashes again alerts again. A finding must be
seen by two checks in a row (a stale status right after the Mac wakes is not a crash), so the
alert comes 1 to 3 minutes after the crash. The supervisor's `bot-crashed` usually comes first;
this one also covers a kill, a reboot and a hang, which leave no supervisor to tell. A crash
before the run's first status write (the status still says `stopped`) is not seen here: only
`bot-crashed` covers it. During the session a down bot with an open trade also gets
`bot-down-with-open-trade`: two alerts of different kinds.

A live bot process (a pid in its lock file) whose status is `starting`, or older than the
process (token wait, startup reconcile), counts as starting for
`[runtime] watchdog_start_grace_seconds` after it took its lock; after that, or with a stale
`running` status, the alert says the bot is hung: stop the job
(`launchctl kill TERM gui/$(id -u)/local.tickr.bot`) before starting it again, since a kickstart
does nothing while it runs.

What was alerted is kept in `data/watchdog-state.json` (`[runtime] watchdog_state_file`), so a
relaunched watchdog does not alert the same crash or disk level again; launchd relaunches it at
most once per `[runtime] watchdog_relaunch_seconds` (`ThrottleInterval`). With an invalid config
it alerts `watchdog-cannot-check` and then waits instead of ending, repeating the alert every 24
hours from the first one (the hour the config was deployed, never midnight) until the config is
fixed and the job restarted.

What to do: read `~/Library/Logs/Tickr/bot.log` for the cause, fix it (a full disk: free space),
then start the bot when it is safe: `launchctl kickstart gui/$(id -u)/local.tickr.bot`.

### `disk-low`

Free space on the data disk fell below `[runtime] disk_warn_free_mb` (10 GB), checked at any hour.
It alerts once, and once more below `[runtime] min_free_disk_mb` (2 GB), where preflight refuses
to start the bot; a disk that runs full stops the bot (2026-11-19 it crashed writing its status
file at 0 bytes free). Back above the warning level re-arms it. Below `min_free_disk_mb` the running
bot also sets the `disk-low` fence (checked every loop pass): no new entry, while protection, exits
and the flatten go on; it clears itself at `[runtime] disk_clear_free_mb` (3 GB), above the floor so
it does not flap (an unreadable free space keeps it set). Below the floor while the bot runs you
get two `disk-low` alerts: the watchdog's, and the bot's once the fence has been set for
`fence_alert_after_seconds`. Free space now: research data, model files and caches belong on the
external disk.

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
file" or "status file unreadable". Nothing will flatten that trade at 15:45 ET.
A mid-session restart of the bot can raise this alert once while the new bot starts and
recovers (no live pid, or a status file not yet rewritten); if the next check is quiet and
`data/status.json` is fresh again, the bot is back.

1. Open the Webull app: is the position still there, and is its stop working?
2. Close the position by hand, or keep it with its stop and close it before 16:00 ET yourself.
3. Do not restart the bot over an open position without reading the bot log; startup
   reconciles first, but you decide. If you do nothing, the watchdog starts the bot job once
   at 15:40 ET (below).

### One restart a day: `watchdog-restarted-bot`

From `[runtime] watchdog_restart_before_flatten_seconds` before the flatten time (300 s:
15:40 ET on a normal day, 12:40 ET on a half day) until its last check, a watchdog bot check
that finds the bot down while its database shows an open trade starts the bot job once:
`[runtime] watchdog_restart_command`, on a Mac `launchctl kickstart gui/<uid>/local.tickr.bot`
(without `-k`, so a job launchd still sees running is left alone; shipped since the server
cutover: the server's restart request, docs/deploy-server.md "Bot restart on the server"). That is the same job the
schedule starts: preflight again (a failure alerts `bot-did-not-start`), the supervisor, the
single-instance lock, then the normal startup: recovery, reconciliation, the stop verified.
Config refuses a restart time at or before the last entry time, so the restarted bot can never
open a trade (the earlier restart for a resting entry, below, starts a run fenced by
`watchdog-started` instead); at the flatten time its own `flatten_due` closes the position through the
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
- Earlier, at any bot check in the regular session: a bot that is down while the database shows
  an open trade whose resting entry (`trades.entry_rest_until`) may still work at Webull is
  started at once, once a day, recorded in `data/watchdog-entry-restart.json` (`[runtime]
  watchdog_entry_restart_file`), so the flatten restart stays available. The started bot cancels
  every working entry and protects any fill, but it sets the `watchdog-started` fence: no new
  entry that day until you look and clear it (`tickr clear-fence watchdog-started`). It is set
  once per record, so a later start the same day after you cleared it is not fenced again.
- To keep the bot down on purpose with a position open (a manual trade), unload the bot job, or
  keep a stop marker left from an earlier stop made while flat, or send `stop-bot` with mode
  `leave_open` (without a mode it is refused while any trade is open). See docs/manual-trade.md.
- Owner check, once on the Mac: the exit code of `launchctl kickstart gui/$(id -u)/local.tickr.bot`
  (no `-k`) for a job launchd still sees running, and for a booted-out job. Record both here.

