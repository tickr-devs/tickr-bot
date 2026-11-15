# Monday 2026-11-16: the first Sandbox day with the bot

A checklist for the owner at the Mac. Details are in [operations.md](operations.md),
[manual-trade.md](manual-trade.md) and [webull-facts.md](webull-facts.md); this page only gives
the order. Times are New York (ET) unless marked "Mac time". Nothing here touches Production.

**One choice first.** The bot and the manual probe trades (`aperture trade`,
`probe-replace-stop`) share one lock and one database, so they cannot run at the same time.
Either the bot runs the whole session (this page), or you follow "Probe day" in manual-trade.md
with the bot job disabled. Both on one day: do the manual probes first, finish flat, then start
the bot (step 9).

## Sunday evening, or Monday before 08:30 Mac time

1. Update the code, flat and with the bot not running:
   ```sh
   cd ~/Development/Aperture && git status --short && git pull
   .venv/bin/pip install -q -e ".[webull]"
   ```
   If `git status --short` lists `config/bot.toml` (you changed a setting), the pull stops with
   "local changes would be overwritten". Then: `git stash`, `git pull`, `git stash pop`, and
   step 3 tells you whether the merged file is valid.
   The API job runs all the time and keeps the old code until it is started again: after a
   pull, `launchctl kickstart -k gui/$(id -u)/local.aperture.api`.
   If the pull brought `ops/migrate-from-aperture.sh` (the rename to Tickr), stop here and run
   that script in the same sitting (dry run first, then `--apply`): after such a pull the old
   launchd jobs no longer start and nothing alerts. Every command below then says `tickr`
   where this page says `aperture`.
2. Load the environment in the Terminal you will use: `source .venv/bin/activate`, then
   `set -a; . ~/.config/aperture/env; set +a`.
3. `aperture check-config`: the environment is `sandbox`, the gate is as you expect.
4. `python -m aperture.runtime preflight; echo $?`: every check has `"passed": true`. On Monday
   the exit code is 0. On Sunday it is 3 with `"session_today": false` and the calendar check
   saying `no session ...: WEEKEND`; that is correct, and the Webull token is then not checked
   (it is checked by the 08:30 run on Monday). Exit code 1 names its cause; a
   `database-other-environment` or `stopped-by-operator` cause: see operations.md before
   anything else.
5. If `ops/launchd/` changed in the pull, render and load the jobs again (operations.md,
   "Install the jobs"). Then send yourself a test alert:
   `launchctl kickstart gui/$(id -u)/local.aperture.test-alert`. The Mac notification and the
   iMessage must both arrive.
6. The Mac stays on power, awake and logged in all day. Messages is signed in.

## Monday morning

7. 08:30 Mac time: the preflight job runs by itself. No alert means it passed
   (`launchctl print gui/$(id -u)/local.aperture.preflight | grep "last exit"` shows 0).
8. 09:00 Mac time: the bot job starts by itself; 09:05 the watchdog.
   `aperture status` shows `bot: pid ... recorded`. `tail -f ~/Library/Logs/Aperture/bot.log`.
9. Starting later by hand (after manual probes): `launchctl kickstart gui/$(id -u)/local.aperture.bot`.

## What to watch

In the apps (they read the bot's local API):

- Before 09:30 the status says entries are refused because the time is outside the regular
  session. That is the calm state, not a fault. From 09:30 it is "entries allowed" or one named
  reason.
- The last broker read is seconds old and keeps moving. If it stops, or a
  `broker-read-failed` fence appears, the bot refuses entries by itself; look at the log.
- Any other fence (`foreign-position`, `unknown-order`, `quantity-mismatch`, `unprotected`):
  open the Webull app and compare positions and working orders before doing anything.
- After 09:30: candidates and one decision a minute per watched symbol appear, with the reason
  for every entry not taken. Sandbox quotes may be rare or delayed; many "quote too old"
  refusals are a finding to write down, not a fault to fix on the day.
- When a trade opens: within `[orders] max_unprotected_seconds` (30 s) the position shows a
  verified stop, and the Webull app shows the position and one working SELL stop. An
  `unprotected-position` alert means: check the app now.
- 15:45 last entry, 15:55 the bot closes what is open. After 16:00 the account is flat and the
  watchdog has sent no alert.

In the Webull app, a few times in the day: positions and working orders match what the apps show.

## Probes (read-only unless said)

Only with a command written in the docs. If a step has no ready command, ask in the project
chat for a script instead of improvising: every extra read counts against the same Sandbox
limit of 30 requests per 60 s per route that the bot uses.

- **Quotes real time or delayed** (right after 09:30): manual-trade.md, "Step 0"
  (`aperture check-quote SYM`).
- **Quotes for all subscribed symbols** (stream, webull-facts.md M3, second half): no extra
  reads. While the bot runs, note in the apps whether the stream is healthy and whether quote
  ages stay low for the symbols being watched; a stream that reconnects or goes silent is the
  finding. After the close, count the day's captured quotes per symbol and the stream's drop
  counters (research.md, "latency"):
  ```sh
  python -m aperture.research latency \
    --quotes data/capture/sandbox/2026-11-16/quotes.jsonl \
    --notices data/capture/sandbox/2026-11-16/notices.jsonl \
    --settings config/research.toml --out data/probe-2026-11-16/latency
  ```
  If `notices.jsonl` does not exist (the stream sent no notice), leave the `--notices` line
  out: the command refuses a file that is missing.
  The bot does not record which symbols it subscribed, so a symbol missing from the report
  was either never subscribed or silent: compare with the symbols the apps showed as watched.
- **Chart C2** (is the bars limit shared between processes): with the bot stopped, at the
  latest 10 minutes before its 09:00 Mac time start, or after it has stopped:
  `python -m aperture.probes chart-limit SYM`. It takes about 90 s, starts two processes that
  send 20 bars requests each, and prints `shared`, `not-shared` or `inconclusive`. It refuses
  while a bot process is recorded in the lock, and during the regular session (09:30 to 16:00)
  even with the bot stopped. A `shared` answer means Webull answered 429
  once; the bars route then stays refused for a minute or so.
- **Chart C1** (a second Webull client next to the running bot): needs
  `[chart] webull_enabled = true` in `config/bot.toml` from before the bot started, so the
  bot has left the chart its slice of the bars limit. If you want C1 on Monday, set it on
  Sunday evening and do not touch the config after the bot starts. The API does
  not become a second Webull client by that setting: it builds no chart client until this
  probe is recorded as passed (GET /chart answers 503 and says so), and the launchd API job
  is not given the Webull keys anyway. Then, any time the bot
  runs: `python -m aperture.probes chart-client SYM`. It reads one bar every 30 s for 10
  minutes, watches that the bot keeps reading the broker and that the token folder does not
  change, and prints `passed` or `failed`. Do not open charts in the apps while it runs: they
  share the same four requests a minute. It refuses without a running bot, with the chart off,
  when the config changed after the bot started, and while the bot has a position or a working
  order (run it while the bot is flat: if the second client breaks the bot's broker access,
  nothing is then open). That check is made once, at the start, so the best time is outside
  the entry window: before 09:30 or after the last entry at 15:45.
- Both write their evidence to `data/probes/<date>/`: paste `summary.json` into the project
  chat. Until both are recorded for an environment, `[chart] webull_enabled` goes back to
  `false` there after the probe.
- **Order probes F1 to F6** (two 1-share trades, replace of a stop): manual-trade.md, "Probe
  day". Not while the bot runs.

Save outputs under `data/probe-2026-11-16/` and paste them into the project chat. Check first
that nothing in them identifies the account.

## Stopping safely

- Normal end: do nothing until the close. The bot flattens at 15:55 and records its day-end
  check; once the apps and the Webull app show no position and no working order, stop the bot
  from Terminal as below.
- Stop entries only, keep protecting: the entry-stop command in the apps.
- Close everything now: the flatten command in the apps, or `aperture exit` in Terminal when the
  bot is not running. Wait until the apps and the Webull app both show no position.
- Stop the bot: when flat, `stop-bot` in the apps (it is refused while a position or an entry is
  open). Starts stay refused until `aperture clear-stop`. From Terminal:
  `launchctl kill TERM gui/$(id -u)/local.aperture.bot`, then wait for the
  `"record": "bot-exit"` line with `"clean": true` in the log. A SIGTERM leaves an open
  position under its broker stop and nothing flattens it while the bot is down.
- Never log out, restart or close the lid while a position or a working order exists.
- If something looks wrong and you are unsure: flatten from the apps, confirm flat in the Webull
  app, then stop the bot and write what you saw in the project chat.
