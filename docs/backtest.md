# Backtest over past gainers (simulated)

`aperture.backtest` answers "does the strategy have an edge?" before the first trade: it rebuilds
which symbols were gainers on each past session, replays the strategy on them minute by minute
with the replay engine (`docs/replay.md`), and reports the result twice: every signal on its
own, and what the bot could have taken under its risk limits. Everything is `simulated`; it is
an evaluation tool, never an acceptance gate (decisions.md, 2026-11-14). The bot never imports it.

## Steps

```
# On the owner's Mac (read-only market data of the configured environment, its credentials):
python -m aperture.backtest fetch-all --out data/backtest --first 2026-08-17 --last 2026-11-13
# Anywhere (offline):
python -m aperture.backtest run --out data/backtest --results out/backtest \
  --strategy aperture.strategy.price_action_v1 --values strategies/price-action-v1/strategy.toml
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
   above the previous close and whose day dollar volume reached bot.toml
   `min_session_dollar_volume_usd`; at most `max_pool_per_day`, best first. This is a superset of
   the minute-level rule below and uses no minute data. Ranking by the day's high is look-ahead,
   so the cap is set not to bind; `pool_cap.csv` records each session's candidates and drops,
   and the report states on how many sessions it bound.
   `select --rule liquid` builds a pool without look-ahead instead, for research beyond
   gainers: per session the `liquid_top_count` symbols with the highest previous-session dollar
   volume whose previous close is at least `liquid_min_price_usd`. Only the previous session
   decides; a chosen symbol with no bar on the day is left out and counted as dropped in
   `pool_cap.csv`. `--daily <dir>` reads daily bars already fetched elsewhere, so the liquid pool
   gets its own data folder:
   `select --rule liquid --out data/liquid --daily data/backtest --first ... --last ...`, then
   `fetch-minutes --out data/liquid`. The `run` step's gainer gate still applies to such a pool.
4. **Minute bars** (`minutes/<SYMBOL>.jsonl`, replay bar format; `minutes_status.jsonl`): each
   pool day plus replay.toml `history_sessions` sessions before it, merged into contiguous runs
   and paged back with `end_time` (probe M4). Symbols that page from the same end share a request.
   A run that cannot be completed (no older bars, page limit, conflict) is recorded with its cause.
5. **Run** (`trades.csv`, `signals.csv`, `summary.json`, `skipped.csv`, `report.json`,
   `report.md`): see below.

## How a pool day is replayed

- Skipped (with cause) when the symbol lacks bars on the day or on any of the `history_sessions`
  sessions before it, or its run is incomplete: live, the bot refuses a symbol with incomplete
  history. Also skipped when the daily (forward-adjusted) to M1 (unadjusted) price scale differs
  between the previous session and the day by more than `split_tolerance_percent`: a split
  between them would make the gate's change meaningless.
- The strategy sits behind a gate (`backtest/gate.py`): it is asked only from the first completed
  bar at which close >= previous session's last close x (1 + `gainer_change_percent` / 100) and the
  session's dollar volume so far reached the discovery floor; then for the rest of the day. Before
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
- Survivorship: the instrument list is today's, so symbols delisted since the window are missing.
- Each symbol is replayed independently; the bot view applies the one-position and daily-loss
  limits afterwards, first come first served.
