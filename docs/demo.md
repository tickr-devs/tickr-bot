# Demo day (`aperture demo`)

A made-up trading day, so every screen of the macOS and iOS apps can be looked at with full data
(open position with its stop, wins and losses, daily room near the ceiling, decisions and their
checks, skipped signals for "Not taken" (ZNTH's re-breakout inside its cooldown after a loss is
refused, with the cooldown's `until`), the Overview's live flow and "Watching closely", observers
with their simulated day results, account, past days for the day picker, performance in R over
the demo sessions, minute bars).

```
.venv/bin/aperture demo          # or: python -m aperture.cli demo
```

What it does:

- Rebuilds `demo/` in the repo (git-ignored): a copy of `config/` and `strategies/` whose
  `bot.toml` runs as environment `demo`, serves the API on `[demo] api_port` (8776) and runs the
  observers listed in `config/demo.toml`. The demo's `data/` is inside that folder; the real
  `data/` is never read or written. A `demo/` folder it did not make is refused, never deleted.
- Runs the real bot (`aperture run`) on a simulated exchange (`src/aperture/demo/world.py`):
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
- Plays the past sessions in `config/demo.toml` fast, then the demo day (today if it is a session,
  otherwise the latest session before it) fast up to `[demo] clock`, then in real time with the
  local API listening, until Ctrl-C. About two to three minutes before the API is up.

Point the apps at it: macOS Settings, API address `http://127.0.0.1:8776` (or
`defaults write local.aperture.operator apiBaseURL http://127.0.0.1:8776`). The API's clock is the demo
day's clock, so "today" in the apps is the demo day.

The bot holds one position at a time in v1 (admission refuses a second), so the demo has one open
position; the day's other trades come one after another. Everything the day contains is in
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

After `[orders] max_unprotected_seconds` (30 s), the bot cancels the stop and closes the
position (`unprotected-too-long`), so the apps then show that exit and its fence. Put `at`
inside the live part of the day (after `[demo] clock`), so the 30 s happen in real time. Only
the demo's exchange changes: `broker/fake.py` and the live path are untouched.
