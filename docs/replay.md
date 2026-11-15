# Historical replay (simulated)

`aperture.replay` runs any strategy bar by bar over recorded M1 bars and reports what would
have happened under conservative fill assumptions. Every output row and the summary carry the
label `simulated`. A replay is an evaluation tool, never an acceptance gate (decisions.md,
2026-11-14), and it proves nothing about real fill quality.

Fetching history from Webull is out of scope here; the tool reads files.

## Run

```
python -m aperture.replay \
  --bars data/history/ABC-2026-11.jsonl data/history/XYZ-2026-11.jsonl \
  --strategy aperture.strategy.price_action_v1 \
  --values strategies/price-action-v1/strategy.toml \
  --settings config/replay.toml --config config/bot.toml \
  --out out/replay-2026-11
```

- `--strategy` is any importable module exposing `create(values) -> Strategy`; `--values` is
  the TOML passed to it. Comparing two strategy versions = two runs with different modules or
  values over the same bars.
- `--settings` defaults to `config/replay.toml`, `--config` to `config/bot.toml`.
- Session clock per day, exactly as live: regular open and close from the calendar named by
  `bot.toml [session] calendar_file`; last entry and flatten = close minus
  `last_entry_minutes_before_close` / `flatten_minutes_before_close`. On a half day the early
  close applies, and entries are `rejected` while `half_days_enabled = false`. Bars on a
  weekend, a holiday or a day outside the calendar's coverage refuse the whole replay.
- Exit code 2 and `replay refused: <cause>` on any bad input; nothing is guessed.

## Input: bars JSONL

One completed M1 bar per line, exactly these keys:

```
{"symbol": "ABC", "start": "2026-11-10T15:00:00+00:00", "open": "10.00", "high": "10.05", "low": "9.95", "close": "10.01", "volume": "1200"}
```

`start` is the bar's opening minute in UTC with an explicit offset (`+00:00` or `Z`). Prices
and volume are strings (floats are refused). Within a symbol, starts must strictly increase
across the files in the order given. Bars are grouped into sessions by their New York
date; earlier sessions of the same symbol are passed as `prior_sessions`.

## Settings

All simulation numbers live in `config/replay.toml`, one comment per key (tested). Missing or
unknown keys are refused. The bot never reads this file.

Slippage per fill = max(price x `slippage_percent` / 100, `slippage_min_usd`), always against
us.

**History.** The strategy gets the same shape of input as live: `bars` = the last
`history_sessions` sessions of the symbol plus today's completed bars (oldest first), and the
same sessions in `prior_sessions`. Indicators therefore warm up as they do live.

**Synthetic quote.** Recorded bars carry no bid/ask. With `synthesize_quote = true` the
strategy receives, after each completed bar, a quote around its close:
spread = max(close x `synthetic_spread_percent` / 100, `synthetic_spread_min_usd`),
bid = close - spread / 2, ask = close + spread / 2, sizes `synthetic_quote_size_shares`,
provider and receipt time = bar end. This lets quote-based strategies (price-action-v1) decide
ENTER; the fill model below stays conservative regardless. It is a model, not market data:
results remain `simulated`. With `false` the quote is `None`.

## Simulation rules

For bar `i` of a session, in this order:

1. **Open position.** A strategy EXIT decided on bar `i-1` closes at bar `i`'s open. A bar at
   or after the flatten time closes at its open. Otherwise a bar whose low reaches the stop closes
   at the stop, or at the open if the bar opened below the stop (gap).
2. **Pending entry.** A bar at or after the last-entry time cancels it (missed). A bar whose range
   touches the price window `[entry_low, entry_high]` fills it at **`entry_high` plus
   slippage**, never better. If that bar also reaches the stop, the stop is taken in the same
   bar. After `entry_wait_bars` bars without a touch the entry is missed; it is never chased.
3. **Strategy.** The strategy is evaluated with history plus bars `0..i` only (no look-ahead;
   tested by altering later bars) and the synthetic quote of bar `i`. With a position open it
   receives a `PositionView` with quantity 1.

Every exit pays slippage. A position still open on the last bar closes at that bar's close
(`end_of_data`); positions never cross sessions. Each symbol is replayed independently with a
fresh strategy instance, so the one-open-position platform limit is not applied across symbols.
ENTER decisions while a position or an entry is pending are recorded as `ignored`; ENTER
on a bar starting before the regular open, or completing at or after the last-entry time (live
admission would refuse it), or without `entry_high`/`stop`, or with the stop not below `entry_high`,
is `rejected` with the cause.

R per trade = (exit - entry) / (entry - stop), where entry already includes slippage.
Percent per trade = (exit - entry) / entry x 100.

## Outputs (in `--out`)

- `trades.csv`: one row per simulated trade: label, strategy, version, symbol, session date,
  setup, signal bar, entry time and price, stop, exit time, price and reason
  (`stop`, `strategy_exit`, `flatten`, `end_of_data`), bars held, R, percent, the strategy's
  quantity and USD = (exit - entry) x quantity (gross, empty without a quantity).
- `signals.csv`: every ENTER decision with its outcome (`filled`, `missed`, `rejected`,
  `ignored`) and cause: the input for "what if we had entered".
- `summary.json` (also printed): sessions, bars, signals by outcome, trades, wins, losses,
  win rate, total and average R, max drawdown in R, total and average percent, exits by
  reason, verdict counts.

## Not yet

- Replaying captured L1 quotes (`data/capture/...`) instead of the synthetic quote.
- A shared portfolio across symbols (one open position at a time, daily loss ceiling).
