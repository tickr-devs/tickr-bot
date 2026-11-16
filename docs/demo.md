# Demo day (`tickr demo`)

A made-up trading day, so every screen of the macOS and iOS apps can be looked at with full data
and live events (open position with its stop, wins and losses, daily room near the ceiling in
the morning, decisions and their
checks, skipped signals for "Not taken" (ZNTH's re-breakout inside its cooldown after a loss is
refused, with the cooldown's `until`), the Overview's live flow and "Watching closely", observers
with their simulated day results, account, past days for the day picker, performance in R over
the demo sessions, minute bars).

```
.venv/bin/tickr demo                   # start: serves within seconds (or: python -m tickr.cli demo)
.venv/bin/tickr demo --build-fixture   # build demo-fixture/ now (a few minutes); `tickr demo` does it on its own
.venv/bin/tickr demo --record          # record the demo day the apps carry (about ten minutes; commit the file)
```

The apps do not need any of this running: their "Demo day" switch plays a recording of this day
that they carry with them (below, "The Demo day switch in the apps"). `tickr demo` serves the same
day live, for a browser or `curl`, the layout sweep and the macOS screen tour.

What it does:

- Rebuilds `demo/` in the repo (git-ignored): a copy of `config/` and `strategies/` whose
  `bot.toml` runs as environment `demo`, serves the API on bot.toml `[api] demo_port` (8776; the
  copy's own `demo_port` is 0) and runs the observers listed in
  `config/demo.toml`. The demo's `data/` is inside that folder; the real
  `data/` is never read or written. A `demo/` folder it did not make is refused, never deleted.
- Runs the real bot (`tickr run`) on a simulated exchange (`src/tickr/demo/world.py`):
  only the broker, market data, clock, `sleep` and alerts are replaced. Webull is never
  contacted, alerts go to the terminal. So the database, status, account, capture and decision
  files have exactly the format of a real day. The per-trade metrics of `docs/api.md`
  (planned loss, stop confirmation time, MAE/MFE) and the entry send of each position and
  trade (sent once, outcome, fill time) come from those records too, so every demo trade has
  them.
- Draws each symbol's minute bars from its planned legs, then roughens them (`[market] texture_*`):
  the price wanders around the plan, run-ups get red bars and pullbacks, wicks and volume vary.
  The noise is seeded by symbol and day, so every run draws the same tape. Where the plan holds
  or drifts, a close never sits above its planned close and every high reaches it, so noise
  alone never closes above the recent highs: the breakouts, stop-outs and refusals stay where
  the legs put them.
- Gives each symbol `[market] chart_days` older days (250) of chart-only D bars before its minute
  history, a seeded random walk ending at its `base`, so the 1M, 3M and 1Y charts and RSI 14
  have data. They are weekdays (holidays are not skipped) and are served on D charts only. The
  bot never reads them: its history reads are minute bars.
- Starts from the fixture (below): copies the day up to `[demo] clock` into `demo/data`,
  runs the store's normal forward migrations on it, serves the API at once on the demo's clock
  (within `[demo] answer_within_seconds`, 10; measured about 2 s), then runs the real bot in real
  time from `clock` with the live loop until `[demo] live_until` (it then stops its bot and exits
  0) or Ctrl-C. Nothing is replayed at start. The bot comes up as on any restart mid-day: it
  reconciles with the (fresh, flat) simulated broker, and its records say every trade is closed.
  The port is bound only then; before, a client is refused at once. Log lines are written at once.

## The fixture (`demo-fixture/`)

`tickr demo --build-fixture` runs the past sessions in `config/demo.toml` fast, then the demo day
(today if it is a session, otherwise the latest session before it) fast up to `[demo] clock`, with
the real bot on the simulated exchange, in its own folder `demo-build/` (so it can run while the
demo serves from `demo/`). It stores the demo's data folder as `demo-fixture/<digest>.tar.xz`
(about 0.8 MB; the prior-session bar cache and backups are left out, the bot rebuilds the first
from the simulated feed), with its manifest inside: the digest of the inputs, the demo day, the
clock and the store schema version. The day must end flat at the clock (MVLD is flattened at
14:29:30), so the live part starts on a fresh simulated broker. The demo day is the fixture's
date, whatever today is. Only the latest build is kept. On the owner's Mac a build takes about
4 minutes.

The inputs are what the fixture's records depend on for the apps: the scenario
(`config/demo.toml`), the demo code (`src/tickr/demo/`), the API contract the apps read
(`src/tickr/api/contract.py`) and the strategy folders (`strategies/`, whose names and versions the
records carry). Engine, risk or runtime changes do not make a fixture stale: it keeps the data
format the apps read, not the decisions today's engine would make (owner 2026-11-15). A store
schema change does not either: its forward migration runs on the fixture at start, as on real
data.

The fixture is built on each Mac from those committed inputs and is never committed (`.gitignore`):
a binary in git would conflict between every pair of pull requests that touch an input and grow
the history by a new copy on each rebuild (a rebuild is not byte for byte the same). Git LFS
would only fix the growth, not the conflicts, and would need `git lfs` on every machine and in CI.
So `tickr demo` builds it when needed:

- **Fixture of the current inputs present** (the usual restart): copy it and serve within seconds.
- **Inputs changed** (after a `git pull`): serve the latest previous build within seconds all the
  same, and start `tickr demo --build-fixture` beside it as a child process (its lines go to the
  demo's log). When the new build lands, the demo stops its bot and exits 0; the next start
  serves the new fixture within seconds. If the build fails, the demo keeps
  serving the previous one and the cause is in the log; the next start tries again. A build still
  running when the demo ends (at `live_until`, Ctrl-C, or a SIGTERM) is stopped and
  started again by the next demo.
- **No usable fixture at all** (the first start on a Mac, or a build whose clock or schema no
  longer fit): build it first, in the foreground (a few minutes, once), then serve. To skip that
  wait, run `tickr demo --build-fixture` once beforehand.

A build never leaves a fixture a start would take but should not:

- **Stopped from outside** (SIGTERM from launchd or from the demo ending its background build,
  Ctrl-C): the bot stops cleanly, as at the build's own stop, only earlier. The build checks that
  every session ran to its own stop (`past_stop`, then `clock`), so a cut-short day is never
  stored; nothing is recorded, and the next start builds again. The archive is written as
  `.<digest>.tar.xz.partial` (manifest last) and gets its name only once complete and checked; a
  start never reads a partial file, and the next build deletes one a killed writer left.
  An outside stop that comes less than `[orders] shutdown_deadline_seconds` (bot.toml) of
  simulated time before the session's own stop can count as reaching it: the bot's drain keeps
  the simulated clock moving, and when it passes that stop, the day it leaves runs up to it. That is harmless: the
  stored day is the same as after the build's own stop.
- **Refused**: the day not flat at the clock, the bot failing (a non-zero exit on a past
  session or on the demo day, even before its stop), or the new archive failing the
  same checks a start runs on it (manifest, extraction, the store's forward migrations, done in a
  scratch folder). Nothing is stored, the previous build is kept, and the cause is recorded in
  `demo-fixture/<digest>.refused`. A start then serves the previous build and says so once,
  without building again (no build-and-restart loop); with no previous build it is refused (exit
  2, which launchd does not retry). An explicit `tickr demo --build-fixture` tries again and
  clears the record when it succeeds; a change of the inputs gives a new digest anyway.

CI builds the real fixture on every Python change (job `demo-fixture`), so a change that breaks the
build (the day not flat at the clock, the bot failing) fails the pull request instead of the
Mac's next build. `tests/demo` builds a short day's fixture once per test module and checks the
first start, a start from a stale fixture (served, rebuilt, swapped) and the refusals.

## The live loop (`[live]`)

The real-time part of the demo day is a loop, so the apps keep getting real bot events to animate
(docs/design-system.md rule 5: data animations move only on real events). Every `[live]
cycle_minutes` (10) from `[demo] clock` on, for every whole cycle before `live_until`, the loader
(`scenario.unroll`) adds legs to the symbols named by `[live] roles` and queues `[live] commands`;
nothing else changes, the bot is the real one. One cycle, in minutes from its start:

| Minute | Role / command | What the real bot does, what the apps get |
| --- | --- | --- |
| 0-4 | `win` breaks out | WATCH, then ENTER around minute 2: entry sent, filled, broker stop placed and verified; flow stages advance, position appears |
| 1-4 | `busy` breaks out | ENTER refused `OWN_POSITION_OPEN` (one position at a time): refused tray, "Not taken" |
| 1-5 | `cooldown` (last cycle's `loss` symbol) breaks out | refused `SYMBOL_COOLDOWN` once flat (or `OWN_POSITION_OPEN` before), with its `until` |
| 4:45 | flatten (everything) | operator command toast, exit `operator-flatten` with a win, day result and curve move |
| 5-9 | `loss` breaks out, collapses at 9-10 | entry, fill, stop verified, then `stopped-out`: a loss, daily room shrinks |
| 5-9 | `blocked` breaks out | refused `OWN_POSITION_OPEN`, then `ENTRY_STOP_SET` |
| 7:50 / 9:50 | entry-stop on / off | entries pill and entry block change, command toasts |

All the time: quotes tick, minute bars close, discovery runs every minute (screener chips move),
reconciliation every 10 s (account and reconcile ages), the observer decides on every bar. The
textured tape makes the exact minute of an entry vary by a minute or so from day to day (the
noise is seeded by symbol and day): most cycles show every row of the table; the cooldown refusal
needs its symbol to have been stopped out in the cycle before, so it shows in about every second or
third cycle. A symbol a role did not mean to be bought (it broke out while the bot was flat) is
stopped out by its own collapse or closed by the next flatten, so the loop never sticks.
Wins are bigger than the losses, so the day stays inside its loss ceiling. The AI advisor is not
part of the loop: it is an offline tool that needs the Anthropic API.

## The "Demo day" switch in the apps

Both apps have a "Demo day" switch (Mac: Settings › General; iPhone: Settings). On, the app shows
the demo day and only that day: the day picker, the day menu and the previous/next buttons stand
still and a DEMO chip sits next to the day, and the command surfaces carry the demo banner. Off,
the app is back on the bot, on today, and any recorded day can be picked again.

The apps carry the demo day with them, so it works on its own: any time (also while the bot
trades), on the phone away from the Mac, with no `tickr demo` running and no network at all.

- **The recording** (`apps/shared/Sources/TickrKit/Resources/DemoDay.jsonl.xz`, made by `tickr demo
  --record`, `src/tickr/demo/record.py`): the real bot runs the scenario on the simulated exchange
  exactly as `tickr demo` does (past sessions and the day up to `[demo] clock` fast, then the bot
  again from `clock` with the live loop), fast and in its own folder (`demo-record/`, deleted
  afterwards). Through `[recording] cycle` of the live loop (cycle 1: its cooldown refusal has the
  symbol stopped out the cycle before), every `[api] poll_seconds` of simulated time (3 s, the
  apps' status poll; 200 frames) the bot is held while the demo's own API is asked for everything
  the apps read: status, account, pending commands, jobs, journal, settings, config, advisor, Lab
  replay, the day list and the day, flow, watching, observers, skipped, performance over
  `[recording] performance_days`, and per symbol of the day its decisions and bars; once a minute
  its charts in every range and interval the apps offer; once, the past days with their trades'
  bars and decisions. Right after the cycle, every command the apps can send (each kind, flatten
  per symbol of the day) goes to the real bot through the demo's API, one every `[recording]
  command_seconds`, stop-bot last, and the API's answer and the bot's outcome are kept; after the
  bot stops, start-bot, a Lab replay and an advisor decision are asked once. The file is
  xz-compressed lines (about 0.5 MB; about 16 MB once unpacked in the app): line 1 the index (per
  route its change points), every further line a piece; a route keeps its first answer whole and
  each change as runs copied from the answer before plus new pieces (cut at commas), so a ticking
  clock or a new candle costs a few bytes. A recording takes about seven minutes on the Mac.
- **Playing it** (TickrKit `DemoSource`): with the switch on, the app's address is
  `DemoDay.address` (`tickr-demo://day`, no network host), and the container answers it from the
  recording instead of URLSession, through the same `APIClient` and the same decoders as the live
  API, so every screen and every animation works as on a real day. The clock loops: frame
  `(now - first use) / poll_seconds`, wrapping after the cycle back to its start (the cycle's
  trades disappear and come back, as at a new cycle). A GET is the frame's recorded answer; a read
  the recording does not hold is `404 not_found`. A command is never sent or queued anywhere: the
  app gets the answer the demo's API gave that command when it was recorded (under a fresh
  command id), and the bot's recorded outcome of it joins the recent commands of the following
  status answers, at the frame's time. So the confirmation (which says DEMO: nothing is sent),
  the "sent" toast and its outcome play as usual, and nothing reaches any bot. The recording
  itself does not change: the flatten does not close the recorded position.
- **Kept current**: the file's `inputs` is the fixture digest (the scenario, the demo code, the API
  contract, the strategies; "The fixture" above). `tests/demo/test_recording.py` fails while it
  does not match, so a change of those inputs comes with `tickr demo --record` and the new file in
  the same pull request. The Swift tests decode every recorded answer with the apps' live decoders
  (`DemoSourceTests`), so a contract change the recording does not follow fails there too.

The bot's API no longer forwards `/demo/` and the `local.tickr.demo` launchd job is gone: the apps
need neither. `tickr demo` stays for looking at the day served live (above).

The bot holds one position at a time in v1 (admission refuses a second), so the demo has at most
one open position; the day's trades come one after another. Everything the day contains is in
`config/demo.toml`, each key commented.

## An unprotected position, for checking the apps

`[scenario] faults` in `config/demo.toml` is empty by default. To see the open position without a
verified stop, add:

```toml
faults = [{ at = "14:31", kind = "stop-unverified", symbol = "MVLD" }]
```

From that New York time on, the simulated exchange shows that symbol's stops in a status the
adapter cannot map (`DEMO_UNVERIFIED`, so `OrderState.UNKNOWN`). The stops are not working and
never fill. The real bot reacts as it would live:

- the position's stop is no longer `verified`;
- the `unprotected` and `status-unknown` fences are set and new entries are blocked;
- the unprotected-position alert goes to the terminal.

After `[orders] max_unprotected_seconds`, the bot cancels the stop and closes the
position (`unprotected-too-long`), so the apps then show that exit and its fence. Put `at`
inside the live part of the day (after `[demo] clock`), so that wait happens in real time. Only
the demo's exchange changes: `broker/fake.py` and the live path are untouched.
