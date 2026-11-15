# Monday 2026-11-16: the first Sandbox day with the bot

A checklist for the owner at the Mac. Details are in [operations.md](operations.md),
[manual-trade.md](manual-trade.md) and [webull-facts.md](webull-facts.md); this page only gives
the order. Times are New York (ET) unless marked "Mac time". Nothing here touches Production.

**The order of the day (owner decision 2026-11-15): manual probes first, then the bot.** The
bot and the manual probe trades (`tickr trade`, `probe-replace-stop`) share one lock and one
database, so they cannot run at the same time. The bot job is kept from starting at 09:00
(step 7), the probes of "Probe day" in manual-trade.md run from 09:30, and once they end flat
the bot is started by hand (steps 11 and 12) and runs to the close.

## Sunday evening, or Monday before 08:30 Mac time

1. Update the code, flat and with the bot not running:
   ```sh
   cd ~/Development/Tickr && git status --short && git pull
   .venv/bin/pip install -q -e ".[webull]"
   ```
   If `git status --short` lists `config/bot.toml` (you changed a setting), the pull stops with
   "local changes would be overwritten". Then: `git stash`, `git pull`, `git stash pop`, and
   step 3 tells you whether the merged file is valid.
   If the Mac still runs the old name (jobs `local.aperture.*`, `~/.config/aperture`), the
   pull brings the rename to Tickr: stop here and run `sh ops/migrate-from-aperture.sh` in the
   same sitting (dry run first, then `--apply`). After such a pull the old launchd jobs no
   longer start and nothing alerts. The repository folder may keep its old name.
   The API job runs all the time and keeps the old code until it is started again. Restart it
   after every pull, before the bot runs: an API on old code cannot read the status of a bot
   on new code (the status now names the strategy of each trade).
   `launchctl kickstart -k gui/$(id -u)/local.tickr.api`.
2. Load the environment in the Terminal you will use: `source .venv/bin/activate`, then
   `set -a; . ~/.config/tickr/env; set +a`.
3. `tickr check-config`: the environment is `sandbox`, the gate is as you expect.
4. `python -m tickr.runtime preflight; echo $?`: every check has `"passed": true`. On Monday
   the exit code is 0. On Sunday it is 3 with `"session_today": false` and the calendar check
   saying `no session ...: WEEKEND`; that is correct, and the Webull token is then not checked
   (it is checked by the 08:30 run on Monday). Exit code 1 names its cause; a
   `database-other-environment` or `stopped-by-operator` cause: see operations.md before
   anything else. Cause `token-dir-open`: the broker token folder is readable by other
   accounts; `chmod 700 data/webull-token-sandbox` once and run preflight again.
5. If `ops/launchd/` changed in the pull, render and load the jobs again (operations.md,
   "Install the jobs"). Then send yourself a test alert:
   `launchctl kickstart gui/$(id -u)/local.tickr.test-alert`. The Mac notification and the
   iMessage must both arrive. In `~/Library/Logs/Tickr/test-alert.log` the first JSON line
   only says the alert is on its way (`"delivery": "sending"`, both channels pending); one
   line per channel follows, and both must say `"delivery": "shown"`.
6. The Mac stays on power, awake and logged in all day. Messages is signed in.
   **Demo day is off in both apps** («Демо-день выключен в обоих приложениях»): Mac Settings ›
   General and iPhone Settings, "Demo day" switch off; no DEMO DAY pill in the Mac title bar and
   no demo banner on the iPhone's Home or the menu-bar panel. While it is on, every command from
   that app (flatten, stop, emergency) goes to the demo, not the bot.
7. Keep the bot job from starting by itself at 09:00 (manual-trade.md, "Before the open",
   step 1). Leave the watchdog job alone.
   ```sh
   launchctl disable gui/$(id -u)/local.tickr.bot
   launchctl bootout gui/$(id -u)/local.tickr.bot
   launchctl print-disabled gui/$(id -u) | grep tickr.bot   # shows it disabled
   ```

## Monday

8. 08:30 Mac time: the preflight job runs by itself. No alert means it passed
   (`launchctl print gui/$(id -u)/local.tickr.preflight | grep "last exit"` shows 0).
9. 09:00 Mac time: the bot does not start, as intended: `tickr status` shows
   `bot: not running`. The watchdog starts at 09:05; while a manual trade is open it alerts
   `bot-down-with-open-trade`, which is expected on a probe day.
10. Before 09:30: the chart C2 probe (below). From 09:30: "Probe day" in manual-trade.md from
    top to bottom (step 0 and step 1 right after 09:30, steps 2 and 3 from 10:00). Manual
    trades are accepted until 15:30; ending by midday leaves the bot a useful afternoon.
    **If Webull's answers cannot be read:** the step 2 trade must close by itself about 90 s
    after its stop appears. If it does not, goes silent, or prints `needs the operator` or
    `fence`: close the position and cancel the orders by hand in the Webull app, confirm the
    app is empty, and do not start the bot on Monday. Paste the probe files into the project
    chat.
11. **Go or no-go for the bot.** Start the bot only if the step 2 trade printed
    `closed (hold-elapsed)` by itself, with no `fence` and no `needs the operator` line, and
    nobody touched it in the Webull app. That one result proves the fill was read, the stop
    was verified, non-empty positions were read, and the stop cancel and the MARKET SELL work.
    A fence cleared by hand after closing in the app does not count: the bot would meet the
    same unreadable answer. Otherwise the bot does not start on Monday; skip step 12.
    If it is a go, end the probes flat (manual-trade.md, step 4): the last `tickr exit`
    printed no `fence ...` line, `tickr status` shows `bot: not running`, the Webull app
    shows no position and no working order.
12. Only after a go in step 11, enable and start the bot:
    ```sh
    launchctl enable gui/$(id -u)/local.tickr.bot
    launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/local.tickr.bot.plist
    launchctl kickstart gui/$(id -u)/local.tickr.bot
    ```
    `tickr status` shows `bot: pid ... recorded`. `tail -f ~/Library/Logs/Tickr/bot.log`.
    If the API job was not restarted after the pull (step 1), restart it now, together with
    the bot: `launchctl kickstart -k gui/$(id -u)/local.tickr.api`.
    The job stays enabled: from Tuesday the bot starts by itself at 09:00 Mac time.
    If the apps have a Start button for the bot, do not press it on Monday: it clears the
    operator stop and kickstarts the bot job, which must wait for the go in step 11. While the
    bot job is disabled for the probes it does nothing useful either. Start the bot as above.

## What to watch

In the apps (they read the bot's local API):

- Before 09:30 the status says entries are refused because the time is outside the regular
  session. That is the calm state, not a fault. From 09:30 it is "entries allowed" or one named
  reason.
- The last broker read is seconds old and keeps moving. If it stops, or a
  `broker-read-failed` fence appears, the bot refuses entries by itself; look at the log.
- Any other fence (`foreign-position`, `unknown-order`, `quantity-mismatch`, `unprotected`,
  `detail-unreadable`): open the Webull app and compare positions and working orders before
  doing anything. `detail-unreadable` means an order's details stayed unreadable for 30 s;
  while it lasts the bot keeps the stop and sends no close, so an exit of that trade waits for
  you (close it in the Webull app and cancel its SELL stop there).
- After 09:30: candidates and one decision a minute per watched symbol appear, with the reason
  for every entry not taken. Sandbox quotes may be rare or delayed; many "quote too old"
  refusals are a finding to write down, not a fault to fix on the day. Symbols left out for a
  wide spread or a small dollar volume show cause `STRATEGY_LIQUIDITY` with the strategy's
  own check (`spread_ratio`, `dollar_volume_usd`): those limits are the strategy's now.
  If the strategy did not load, the bot still protects and exits but discovers nothing and
  enters nothing; the status shows entries off with the cause.
- When a trade opens: within `[orders] max_unprotected_seconds` (30 s) the position shows a
  verified stop, and the Webull app shows the position and one working SELL stop. An
  `unprotected-position` alert means: check the app now.
- 15:45 last entry, 15:55 the bot closes what is open. After 16:00 the account is flat and the
  watchdog has sent no alert.

In the Webull app, a few times in the day: positions and working orders match what the apps show.

**Never buy or sell by hand a symbol the bot holds.** If foreign shares turn up in the bot's
symbol, the bot raises `quantity-mismatch`, and at its exit or the 15:55 flatten it sells
exactly its own quantity and never touches the extra shares. The trade then stays open in the
apps until the account is flat in that symbol: close the extra shares by hand in the Webull
app and confirm no position and no working order is left.

## Probes (read-only unless said)

Only with a command written in the docs. If a step has no ready command, ask in the project
chat for a script instead of improvising: every extra read counts against the same Sandbox
limit of 30 requests per 60 s per route that the bot uses.

- **Quotes real time or delayed** (right after 09:30): manual-trade.md, "Step 0"
  (`tickr check-quote SYM`).
- **Quotes for all subscribed symbols** (stream, webull-facts.md M3, second half): no extra
  reads. While the bot runs, note in the apps whether the stream is healthy and whether quote
  ages stay low for the symbols being watched; a stream that reconnects or goes silent is the
  finding. After the close, count the day's captured quotes per symbol and the stream's drop
  counters (research.md, "latency"):
  ```sh
  python -m tickr.research latency \
    --quotes data/capture/sandbox/2026-11-16/quotes.jsonl \
    --notices data/capture/sandbox/2026-11-16/notices.jsonl \
    --settings config/research.toml --out data/probe-2026-11-16/latency
  ```
  If `notices.jsonl` does not exist (the stream sent no notice), leave the `--notices` line
  out: the command refuses a file that is missing.
  The bot journals each change of its subscribed set (a `stream-subscriptions` event with
  `added`, `removed` and the whole `subscribed` set), so a symbol missing from the report
  can be checked against what was subscribed and when.
- **Chart C2** (is the bars limit shared between processes): with the bot stopped and outside
  the regular session; on Monday the bot is off in the morning, so before 09:30:
  `python -m tickr.probes chart-limit SYM`. It takes about 90 s, starts two processes that
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
  is not given the Webull keys anyway. If that 503 names an unfinished run or a summary it
  cannot read, run the probe again; for an unreadable one, first delete that run's folder
  under `data/probes/`. Then, any time the bot
  runs: `python -m tickr.probes chart-client SYM`. It reads one bar every 30 s for 10
  minutes, watches that the bot keeps reading the broker and that the token folder does not
  change, and prints `passed` or `failed`. Do not open charts in the apps while it runs: they
  share the same four requests a minute. It refuses without a running bot, with the chart off,
  when the config changed after the bot started, and while the bot has a position or a working
  order (run it while the bot is flat: if the second client breaks the bot's broker access,
  nothing is then open). That check is made once, at the start, so the best time is outside
  the entry window. On Monday the bot starts after the manual probes, so: after the last
  entry at 15:45.
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
- Close everything now: the flatten command in the apps, or `tickr exit` in Terminal when the
  bot is not running. Wait until the apps and the Webull app both show no position.
- Stop the bot: when flat, `stop-bot` in the apps (it is refused while a position or an entry is
  open). Starts stay refused until `tickr clear-stop`. From Terminal:
  `launchctl kill TERM gui/$(id -u)/local.tickr.bot`, then wait for the
  `"record": "bot-exit"` line with `"clean": true` in the log. A SIGTERM leaves an open
  position under its broker stop and nothing flattens it while the bot is down.
- Never log out, restart or close the lid while a position or a working order exists.
- If something looks wrong and you are unsure: flatten from the apps, confirm flat in the Webull
  app, then stop the bot and write what you saw in the project chat.
