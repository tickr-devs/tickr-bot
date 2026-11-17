# Backtest over past gainers (simulated)

`tickr.backtest` answers "does the strategy have an edge?" before the first trade: it rebuilds
which symbols were gainers on each past session, replays the strategy on them minute by minute
with the replay engine (`docs/replay.md`), and reports the result twice: every signal on its
own, and what the bot could have taken under its risk limits. Everything is `simulated`; it is
an evaluation tool, never an acceptance gate (decisions.md, 2026-11-14). The bot never imports it.

## Steps

```
# On the owner's Mac (read-only market data of the configured environment, its credentials):
python -m tickr.backtest fetch-all --out data/backtest --first 2026-08-17 --last 2026-11-13
# Anywhere (offline):
python -m tickr.backtest run --out data/backtest --results out/backtest \
  --strategy tickr.strategy.price_action_v1 --values strategies/price-action-v1/strategy.toml
```

`fetch-all` = `fetch-universe`, `fetch-daily`, `select`, `fetch-minutes` (each also runs alone).
Every fetch step is resumable: finished files are skipped, so rerun the same command after an
interruption. Requests take the bot's request budget; when it refuses, the step waits the time it
names. The first response of each kind and every failed one are kept in `<out>/raw/`.

1. **Universe** (`universe.jsonl`): the full instrument list (`/trading/instruments/stocks/
   profiles/list` without symbols, paged by `pagination_key`) for bot.toml
   `allowed_security_types`, filtered by `allowed_exchanges` and `allowed_currencies` exactly like
   discovery, plus `instrument_statuses` (Webull status; NT instruments are refused by the bars
   route). No symbol lists. A symbol under two instrument ids is refused
   (`universe_refused.jsonl` has every refused row and its cause).
2. **Daily bars** (`daily/*.jsonl`): the window's sessions plus the one before, 20 symbols per
   request. A bar label whose New York date is not a session of the window stops the step.
3. **Pool** (`pool.csv`): per session, symbols whose daily high reached `gainer_change_percent`
   above the previous close and whose whole day passes the active strategy's session liquidity
   rules (its `[liquidity]` `min_dollar_volume_usd` on the day's typical price x volume; the
   floor lives only in the strategy's file); at most `max_pool_per_day`, best first. This is a superset of
   the minute-level rule below and uses no minute data. Ranking by the day's high is look-ahead,
   so the cap is set not to bind; `pool_cap.csv` records each session's candidates and drops,
   and the report states on how many sessions it bound.
   A symbol with any non-positive daily price in the window (forward adjustment can push old
   bars below zero) is left out of selection; `pool_refused.csv` lists it with the bar.
   `select --rule liquid` builds a pool without look-ahead instead, for research beyond
   gainers: per session the `liquid_top_count` symbols with the highest previous-session dollar
   volume whose previous close is at least `liquid_min_price_usd`. Only the previous session
   decides; a chosen symbol with no bar on the day is left out and counted as dropped in
   `pool_cap.csv`. `--daily <dir>` reads daily bars already fetched elsewhere, so the liquid pool
   gets its own data folder:
   `select --rule liquid --out data/liquid --daily data/backtest --first ... --last ...`, then
   `fetch-minutes --out data/liquid`. The `run` step's gainer gate still applies to such a pool.
   `select --rule all` keeps every symbol with a bar on the day whose previous close is at least
   `all_min_price_usd`: the whole universe, so stocks that jumped and faded are in it too (the
   gainer pool only holds symbols whose day ended up as a gainer). Symbols delisted since are
   still missing: the universe is Webull's current instrument list.
4. **Minute bars** (`minutes/<SYMBOL>.jsonl`, replay bar format; `minutes_status.jsonl`): each
   pool day plus replay.toml `history_sessions` sessions before it, merged into contiguous runs
   and paged back with `end_time` (probe M4). Symbols that page from the same end share a request.
   A run that cannot be completed (no older bars, page limit, conflict) is recorded with its cause.
5. **Run** (`trades.csv`, `signals.csv`, `summary.json`, `skipped.csv`, `report.json`,
   `report.md`): see below. `--last-sessions N` replays only the pool days of the latest N
   calendar sessions, ending at the pool's last day (the Lab's "replay the last days");
   `--end-day D` ends the window at pool day D instead (`--last-sessions 1 --end-day D` is the
   Lab's "replay one day"). The
   report then has `last_sessions`: the count asked for, the first and last day of the window,
   and how many of its sessions had no replayed day (no gainer, or incomplete data). With few
   sessions, the interval and the hold-out are wide by construction, and report.md says so.

## Nightly

`python -m tickr.backtest nightly --out data/nightly --seed data/backtest` keeps a rolling folder
of the last `[backtest] nightly_window_sessions` sessions for the Lab's replay
(`[api] lab_replay_data_dir` is its `current/`). launchd runs it after the close
(`local.tickr.nightly-fetch`, `[runtime] nightly_fetch_*`, docs/operations.md). A full fetch of
that many sessions takes many hours under Webull's limits, so a night adds only what the newest
completed sessions bring:

1. Daily bars of every universe symbol for the new sessions (and the one before, for its close),
   as their own `daily/n<last day>-<chunk>.jsonl` files. The universe is the folder's own.
2. The new sessions' pool days (same gainer rule as `select`), appended to `pool.csv` and
   `pool_cap.csv`.
3. Minute bars only for the sessions the new pool days need (each day and `history_sessions`
   before it) that the symbol's minute file holds no bar for, merged into that file.
4. Pool days older than the window are dropped, with minute files no pool day uses any more.

The night is built in `staging/` (minute and daily files hard-linked from `current/`, every file
it changes written under a new name, so `current/` is never modified), then swapped in.
`raw/` stays outside. Without `current/` it starts from `--seed` (a `fetch-all` folder).
Refused, leaving `current/` as it was and the cause in `nightly.json`: during the regular
session, with less free disk than `nightly_min_free_disk_mb`, with more missing sessions than
`nightly_max_new_sessions` (run `fetch-all` instead), and once the next session's open minus
`nightly_stop_minutes_before_open` is reached, so it never shares the market-data budget with
the bot. GET /jobs shows the last outcome. The universe is not refreshed: a new listing enters
the pool only after the next `fetch-all`.

## How a pool day is replayed

- Skipped (with cause) when the symbol lacks bars on the day or on any of the `history_sessions`
  sessions before it, or its run is incomplete: live, the bot refuses a symbol with incomplete
  history. Also skipped when the daily (forward-adjusted) to M1 (unadjusted) price scale differs
  between the previous session and the day by more than `split_tolerance_percent`: a split
  between them would make the gate's change meaningless. When the replay engine refuses a
  symbol (a bad bar, e.g. a 0.01 USD print whose synthetic bid is not positive), the symbol is
  replayed again one pool day at a time on that day's bars only, and the days still refused are
  skipped as `REPLAY_REFUSED` with the engine's message; the run never stops for one bar.
- The strategy sits behind a gate (`backtest/gate.py`): it is asked only from the first completed
  bar at which close >= previous session's last close x (1 + `gainer_change_percent` / 100) and the
  strategy's own `session_liquidity` rules (its `[liquidity]` dollar-volume floor) pass on the
  session's bars so far, as discovery applies them live; then for the rest of the day. Before
  that, and on every non-pool session, the decision is IGNORE with one check. An open simulated
  position is always passed through.
- Fills, slippage, the synthetic quote, last entry and flatten are the replay's
  (`config/replay.toml`, bot.toml `[session]`, calendar).

## Report

- **Every signal**: trades, win rate, mean and median R, profit factor, max drawdown in R, and a
  percentile bootstrap interval of the mean R that resamples whole days, since trades of one day
  are correlated (`bootstrap_samples`, `bootstrap_seed`,
  `confidence_percent`). An interval above 0 is evidence of an edge under these assumptions; an
  interval containing 0 is not. The same is reported for the last `holdout_percent` of sessions
  (hold-out) and for the sessions before it.
- **Bot limits**: trades in entry order, at most bot.toml `max_open_positions` open, each through
  the live money checks: `risk.sizing.check_size` (planned loss and notional caps at the window
  top and the stop) and `risk.daily.check_daily` (net realized loss of the day + planned loss of
  open positions + this trade's planned loss within `max_daily_loss_usd`). Refusals are counted by
  cause. Near the ceiling the quantity is sized down to the room left, as the live strategy does
  with `loss_budget_usd` (approximately: by entry_high - stop, without the strategy's spread and
  slippage reserve). USD = (exit - entry) x quantity, gross (no commissions or fees).
- Breakdowns by month, setup, exit reason, entry hour (New York) and entry price
  (`price_bucket_edges_usd`).

## Known limits

- Gainers only: discovery also reads losers and top actives (relative volume); symbols that were
  candidates only through those lists are not in the pool.
- The rank on the 200-row gainers list is not reconstructed; the change threshold is a proxy.
- The spread floor and quote-dependent strategy checks see the replay's synthetic quote, not real
  quotes; real spreads of fast gainers are often wider, so results are likely optimistic there.
- Survivorship: the instrument list is today's, so symbols delisted since the window are missing
  (this applies to the gainer pool and to the liquid pool alike).
- Each symbol is replayed independently; the bot view applies the one-position and daily-loss
  limits afterwards, first come first served.
