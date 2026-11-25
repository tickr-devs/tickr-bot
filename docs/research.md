# Signal research: outcomes, what-if, version diff, refusals

`tickr.research` answers these questions offline, from captured M1 bars (the bar JSONL format
of `docs/replay.md`) and recorded decisions. It never trades. The bot imports only its record
format (`records.py`), through the observers (`docs/observers.md`).

| Command | Question | Label |
|---|---|---|
| `outcomes` | What did the price do 1/5/15/30/60 minutes after each WATCH and ENTER? | `observed` |
| `whatif` | What if we had entered on an ENTER that was refused or never evaluated? | `simulated` |
| `decide` + `diff` | What would another strategy version have decided over the same day? | (decisions) |
| `observers` | How did the live observer versions do against the active one today? | `simulated` |
| `latency` | How late do captured quotes arrive, by symbol and hour? What do stream notices say? | (measured) |
| `refusals` | Per day and setup: which check refused first, which platform causes, how many entries? | (counts) |
| `slippage` | How far below the planned stop did stops fill? Planned against realized loss per day? | `measured` |

Live observers see only the symbols discovery kept, and discovery applies the active strategy's
liquidity rules, so observer results are comparable only while the active strategy (and its
`[liquidity]` values) stays the same.

Every simulated trade of `observers` also records the share of the stock's move it captured
(owner 2026-11-16: settings are judged by that share together with the dollars):
(exit - entry) / (move high - move low), the low from the regular open through the signal bar,
the high from the entry bar through the flatten time; 1 is the whole move, below 0 a loss. The
report's `average_captured_share` is over the trades that have one; a trade with missing bars or
a move without height has none, with its cause.

Outcomes are price moves, never called profit. What-if results are simulated with the replay's
conservative fill model and prove nothing about real fills.

## Decision records (input)

One JSON object per line, written by `tickr.research.records.write_records` (the bot is
expected to use the same writer when it records decisions; until then `decide` produces them).

```
{"time": "2026-11-10T15:01:02+00:00", "bar_start": "2026-11-10T15:00:00+00:00",
 "symbol": "ABC", "strategy": "price-action-v1", "version": "1", "role": "active",
 "verdict": "ENTER", "setup": "breakout", "trigger": "10.00", "invalidation": "9.80",
 "entry_low": "10.00", "entry_high": "10.10", "stop": "9.80", "quantity": "15",
 "admission": "refused", "refusal_cause": "daily loss ceiling", "checks": [
   {"rule": "relative_volume", "measured": "2.5", "operator": ">=", "threshold": "2.0",
    "passed": true, "note": ""}]}
```

- `bar_start` is the decision bar: the last completed M1 bar the strategy saw. `time` must be
  at or after its end.
- Decimals are strings or null (floats are refused).
- `admission`: `taken` / `refused` (active ENTER that reached admission; `refused` needs a
  `refusal_cause`), `not_evaluated` (ENTER that never reached admission: observer, replay,
  or the bot's own rest of a minute evaluated late for the record after protect-first, see
  `docs/operations.md`),
  `not_applicable` (every other verdict). `taken` is only valid for role `active`.
- Exactly these keys; anything else is refused with file, line and cause.
- A late remainder is evaluated with the quotes and daily loss budget saved when its minute
  was put aside. Its `evaluation-remainder` journal row (keyed by the bar) is written before
  its records, so a restart never records it twice. An `evaluation-remainder` row with no
  matching records for that `bar_start` and those symbols means those records were lost: the
  process died between writing the row and writing the records.

## outcomes

```
python -m tickr.research outcomes --bars data/capture/.../bars.jsonl \
  --decisions decisions.jsonl --settings config/research.toml [--config config/bot.toml] \
  --out out/outcomes
```

For each WATCH and ENTER record and each horizon `h` in `config/research.toml`:

- reference = close of the decision bar;
- price at horizon = close of the last bar that started at or before `bar_start + h` minutes
  (the bar ending `h` minutes after the decision bar ends); a minute without trades keeps the
  last price;
- move, max up (highest high) and max down (lowest low) after the decision bar up to the
  horizon, each minus the reference, in percent of the reference and in ATR;
- ATR = Wilder ATR(`atr_period`) over the symbol's bars up to and including the decision bar
  (history days included, no later bar).

A horizon is measured only when it ends at or before the regular-session close of the
decision's date (from the calendar named by bot.toml, so half days count; post-market bars are
never used) and the captured bars of that date reach it; otherwise the row has a `cause` and no
numbers. A day the calendar refuses is a cause too. Missing decision bar, missing symbol,
unavailable or zero ATR are causes too (percent stays when only ATR is missing).

Outputs: `outcomes.csv` (one row per decision and horizon) and `outcomes_summary.json` (per
group `WATCH`, `ENTER:taken`, `ENTER:refused`, `ENTER:not_evaluated` and horizon: rows,
measured, up, down, mean and median move in percent and ATR).

## whatif

```
python -m tickr.research whatif --bars ... --decisions decisions.jsonl \
  [--replay-settings config/replay.toml] [--config config/bot.toml] \
  [--quantity 20] --out out/whatif
```

Every ENTER whose admission is not `taken` is replayed alone through the replay engine with a
scripted strategy that returns exactly that decision on its bar. So the rules are the replay's
(`docs/replay.md`, settings in `config/replay.toml`, each day's clock from bot.toml `[session]`
and the calendar, as live): entry at the top of the window plus
slippage if a following bar trades in the window within `entry_wait_bars` (else `missed`, never
chased); exit at the stop, at the flatten bar's open, or at the last bar's close, always with
slippage; when the fill bar also reaches the stop, the stop is taken in that bar. An ENTER
completing at or after the last entry time (or on a disabled half day) is `rejected`.

Result: R per share, and USD = (exit - entry) x quantity, **gross**: slippage is included,
commissions and fees are not (the config has no commission model), where quantity is `--quantity` or the
recorded `quantity`; without either the row is `not_simulated`. Outputs: `whatif.csv` and
`whatif_summary.json` (outcomes, admissions, trades, wins, losses, total and average R, total
USD, exits by reason).

## decide and diff: another strategy version

```
python -m tickr.research decide --bars <history days + the day> \
  --strategy tickr.strategy.price_action_v1 --values strategies/<v2>/strategy.toml \
  [--replay-settings config/replay.toml] [--config config/bot.toml] --out out/v2
python -m tickr.research diff --baseline decisions.jsonl --candidate out/v2/decisions.jsonl \
  [--baseline-strategy price-action-v1:1] --settings config/research.toml --out out/diff
```

`decide` runs the strategy through the replay engine (same history, synthetic quote and
no-look-ahead rules as a replay) and records every decision with role `observer`. While the
replay holds a simulated position the strategy is asked with that position, so HOLD/EXIT
appear as in a replay. Pass the prior sessions' bars too: strategies need warm-up history.

`diff` compares per (symbol, decision bar), only inside the (symbol, session date) pairs of the
baseline. Each side must hold one strategy version (filter with `--*-strategy NAME[:VERSION]`).
If a side has several records for one bar, the last one in file order is compared and the
count is shown. Pairs where both sides are IGNORE or absent are skipped; the rest are `same`,
`verdict_changed` (a missing side is `absent`) or `levels_changed` (setup, trigger,
invalidation, window, stop or quantity) with the changed fields and each side's first failed
check. Outputs: `diff.csv` and `diff_summary.json`.

Known gap: live decisions see real L1 quotes, replayed ones a synthetic quote around the bar
close, so quote-dependent checks (spread, cost rule) can differ for that reason alone.

Hold-out rule (`docs/advisor.md`): when the candidate version was tuned on some days, a `diff`
or `whatif` over those days says nothing about the change; run it on other days.

## latency: quote latency from capture files

```
python -m tickr.research latency --quotes data/capture/<env>/<NY date>/quotes.jsonl \
  [--notices data/capture/<env>/<NY date>/notices.jsonl] [--source stream|snapshot] \
  --settings config/research.toml --out out/latency
```

Reads the capture files as `tickr.data.capture` writes them (several days may be passed).
Latency = `receipt_time` minus `provider_time` in milliseconds, for quotes of `--source`
(default `stream`; snapshot quotes measure a REST round trip plus quote age, a different thing).

- `latency.csv`: one row per symbol (`group=symbol`) and per receipt hour in
  `session_timezone` (`group=hour`, `YYYY-MM-DD HH:00`): `count`, `median_ms` (middle two
  averaged), `p90_ms` (nearest rank: the smallest value with at least 90% of values at or
  below it), `max_ms`, `min_ms`, `clock_skew`.
- **Clock skew is shown, never hidden.** A negative latency means the quote arrived before the
  provider's own timestamp: our clock and Webull's disagree. It stays in every statistic and is
  counted in `clock_skew`; `min_ms` shows how far.
- `notices.csv`: stream notice 1001 counters per receipt hour: `notices`, `drop_sum`,
  `drop_max`, `sent_sum`, `sent_max`, `rtt_median`, `rtt_max`. The bot captures them to
  `notices.jsonl` (`type`, integer `drop`, `rtt`, `sent`, `provider`, `receipt_time`), about one a
  minute. Their meaning is unproven (`docs/webull-facts.md` M3): whether `drop`/`sent` count per
  interval or since connect, and whether `rtt` is milliseconds, is not known yet, so both sum
  and max are shown and nothing is converted. A malformed notice is not captured: the stream
  journals it as a `stream-error` (`where` = `notice`) and carries on.
- Which symbols the bot was subscribed to, and when, is in the bot's journal: one
  `stream-subscriptions` event per change, with `added`, `removed`, the whole `subscribed` set
  and its `optional` part (candidates and observer symbols). A symbol with few quotes in
  `latency.csv` can be read against how long it was subscribed.
- `latency_summary.json`: quote, symbol and hour counts, `clock_skew_quotes`, `min_latency_ms`,
  notice count and `partial_last_lines`.

A malformed line is refused with file, line and cause. A last line without its newline (a file
the bot is still appending to) is left unread and counted in `partial_last_lines`.

## refusals: per-day, per-setup refusal table

```
python -m tickr.research refusals --decisions decisions.jsonl \
  [--strategy price-action-v1:1] --settings config/research.toml --out out/refusals
```

Counts recorded decisions per session date (in `session_timezone`), strategy, version, role
and setup; nothing is replayed. Each row has a `kind`, a `name` and a `count`:

- `first_failed_check`: IGNORE and WATCH decisions whose first failed check is `name` (the
  same count as the advisor evidence; decisions where every check passed are not counted);
- `platform_refusal`: ENTERs refused at admission, `name` = `refusal_cause`;
- `enter`: ENTERs by admission, `name` = `taken`, `refused` or `not_evaluated`.

`setup` is empty when the decision named none (for price-action-v1: no setup armed). Rows are
sorted by day, strategy, version, role, setup, kind, then count (largest first). HOLD and EXIT
are not counted. Outputs: `refusals.csv` (one row per count, long format, easy to pivot in a
spreadsheet) and `refusals_summary.json` (days, rows, totals by kind).

## slippage: stop slippage from the bot's records (R5)

```
python -m tickr.research slippage --database data/tickr.sqlite3 [--environment sandbox] \
  --settings config/research.toml --out out/slippage
```

Reads the bot's database read-only (never migrates or writes it). A stopped trade is a closed
trade with at least one filled stop order; `--environment` keeps only trades opened by runs of
that environment. Per trade: the planned stop price and the stop fill price (both
share-weighted over its filled stop orders; the fill is the exact price from cash activity when
recorded, else the broker's order row), slippage = stop - fill per share and in percent of the
stop (positive = filled worse than planned), the recorded planned loss and the realized loss =
(entry fill - average sell fill) x shares sold over every sell, gross of fees. A trade with a
missing price has a `cause` and is left out of every sum. Days are New York dates of the close
(`session_timezone`).

Outputs: `slippage_trades.csv`, `slippage_days.csv` (per day: stopped and measured trades,
planned and realized loss, stop slippage in USD and percent) and `slippage_summary.json`, whose
`stop_slippage_percent` = sum((stop - fill) x shares) / sum(stop x shares) x 100 over every
measured stop fill.

**Replay and backtests.** `measured_slippage_file` in `config/replay.toml` (relative to that
file's folder; `""` = none) selects a `slippage_summary.json`: its `stop_slippage_percent`
then replaces `slippage_percent` for every simulated fill (replay, backtest, what-if,
observers). The fixed value stays the default. A missing or malformed file, a negative measured
value, or fewer than `measured_slippage_min_trades` measured trades is refused, never replaced
by the fixed value. Not measured yet: how far the price traded through the stop (trade prints,
`ticks/list`), and entry slippage.

## Settings

`config/research.toml` `[research]` (one comment per key, tested; missing or unknown keys are
refused): `horizons_minutes`, `atr_period`, `session_timezone`, `output_decimal_places`
(`latency` reads only the last two).
What-if and `decide` use `config/replay.toml`, so they fill and replay exactly like the replay.

Exit code 2 and `research refused: <cause>` on any bad input; nothing is guessed.
