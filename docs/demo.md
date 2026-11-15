# Demo day (`tickr demo`)

A made-up trading day, so every screen of the macOS and iOS apps can be looked at with full data
and live events (open position with its stop, wins and losses, daily room near the ceiling in
the morning, decisions and their
checks, skipped signals for "Not taken" (ZNTH's re-breakout inside its cooldown after a loss is
refused, with the cooldown's `until`), the Overview's live flow and "Watching closely", observers
with their simulated day results, account, past days for the day picker, performance in R over
the demo sessions, minute bars).

```
.venv/bin/tickr demo          # or: python -m tickr.cli demo
```

What it does:

- Rebuilds `demo/` in the repo (git-ignored): a copy of `config/` and `strategies/` whose
  `bot.toml` runs as environment `demo`, serves the API on bot.toml `[api] demo_port` (8776, with
  `demo_port = 0` in the copy, so the demo's API forwards nowhere) and runs the observers listed in
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
- Plays the past sessions in `config/demo.toml` fast, then the demo day (today if it is a session,
  otherwise the latest session before it) fast up to `[demo] clock`, then in real time with the
  local API listening, until `[demo] live_until` (it then stops its bot and exits 0) or Ctrl-C.
  About two to three minutes before the API is up.

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
still and a DEMO chip sits next to the day. Off, the app is back on the bot, on today, and any
recorded day can be picked again.

How it reaches the demo, with one click and nothing to type:

- **The demo runs on its own**: the `local.tickr.demo` launchd job (`ops/launchd`, rendered and
  installed with the other jobs, docs/operations.md) runs `tickr demo` at login. At `[demo]
  live_until` the demo exits 0 and launchd starts a fresh one (KeepAlive on a clean exit only), so
  whenever the apps look, the day is inside the same window: the live loop above, before the last
  entry and the flatten. About three minutes in every eighty the demo is rebuilding and the apps
  show "not reachable". It never talks to Webull (broker hosts point at `.invalid`, credential
  names are unset), needs no credentials file, runs at Background priority, and touches only
  `demo/`. Why `tickr demo` and not a frozen copy of `demo/` served by the API alone: the frozen
  copy's status goes stale and its "today" is not the demo day, so Overview, positions and the live
  flow would all read as a dead bot; `tickr demo` is the real bot on a simulated exchange, the same
  code path as production, with no second way of serving.
- **The apps never change their address**: the bot's own API forwards every request under `/demo/`
  to the demo's API on `[api] demo_port` (`api/demo_forward.py`, docs/api.md). The switch only puts
  `/demo` in front of the path. So the Mac uses `http://127.0.0.1:8766/demo`, and the iPhone uses
  its paired Tailscale address with `/demo`, through the same `tailscale serve` and the same pairing:
  no second port, no second pairing. The token is checked by the bot's API and never forwarded; the
  real address and token stay as they are and are used again the moment the switch is off.
- The demo's port lives in one place, `[api] demo_port` in `config/bot.toml`; the apps hold no port.

Without the job, `tickr demo` in a terminal serves the same day the same way. The API's clock is the
demo day's clock, so "today" in the apps is the demo day.

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
