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
| `collar` | How far is an entry price from the last completed bar's close and the previous quote's ask? | (measured) |
| `refusals` | Per day and setup: which check refused first, which platform causes, how many entries? | (counts) |
| `calibration` | Does a model answer of 0.7 come true 70% of the time? | `measured` |
| `slippage` | How far below the planned stop did stops fill? Planned against realized loss per day? | `measured` |
| `fill-quality` | Entry, exit and stop slippage, fees and fill delay of real fills; one record set or two side by side | `measured` |
| `gate` | Descriptive R23 Step 0 report on one model version's sized trades: net, sensitivity checks, tail concentration, same-trade spread cost (never a promotion) | `simulated` or `real` |

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

## Sealed exam window: refused by every dated command

The sealed exam data (backlog R20, `docs/promotion.md` rung 0) is never read by a research
command. Its one home is the `[sealed]` table of the repository's `config/research.toml`:
`ranges` (closed `[first, last]` date ranges, today 2026-07-01 to 2026-08-14) and
`every_day_from` (2026-11-20: that day and every later one). A day is the exchange-time-zone date
(`[research] session_timezone` of the same file) of an input's time. `research/sealed.py` reads
that fixed file, never a `--settings` argument, and there is no override flag: the exam unseals
only when the owner edits the table.

Each command checks before it computes anything and refuses the whole run with
`research refused: sealed_window: <input> touches the sealed exam window on <day>` (exit 2, no
output written). A sealed day is never dropped while the rest is reported.

| Command | What is checked |
|---|---|
| `gate` | `--start..--end` (before any file is opened), then every trade's day and every `--covered-days` day |
| `outcomes`, `whatif`, `observers` | every decision's `time` and `bar_start`, every bar of `--bars` |
| `decide` | every bar of `--bars` |
| `diff`, `refusals` | every decision of each record set |
| `latency` | the receipt time of every captured quote and notice |
| `collar` | every captured quote and bar, and `--decisions` |
| `slippage` | the day of every stopped trade of `--database` |
| `fill-quality` | the day of every trade of each database, and the receipt time of every quote |
| `calibration` | nothing: stored answers carry no day, so the caller must keep sealed answers out |

The window also covers the bot's own records of live days from 2026-11-20 (captures, decision
logs, the database), so the daily reports above refuse those days until the owner moves the
window.

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

## collar: entry price distance for the price collar (T10)

```
python -m tickr.research collar --quotes data/capture/<env>/<NY date>/quotes.jsonl \
  --bars data/capture/<env>/<NY date>/bars.jsonl \
  [--decisions data/decisions/<NY date>/decisions.jsonl] \
  [--replay-settings config/replay.toml] [--config config/bot.toml] --out out/collar
```

Measures what the price collar (`[risk] entry_collar_bar_ratio` and `entry_collar_quote_ratio`,
spec section 7 check 5; shipped off at 0) would see, so the owner can pick its two numbers from
data. Run it on the Mac over as many captured days as possible (pass several files to each
flag, `.gz` archives included). Distance = |entry price - reference| / reference, the collar's
own measure, as a ratio (0.02 = 2%).

- Entry `quote`: every captured stream quote's ask (a marketable entry at that moment). Entry
  `decision` (with `--decisions`): each recorded ENTER's final limit (`entry_high` rounded to the
  tick), at the record's time. The live collar checks the limit, which is at or above the ask, so
  read both.
- Reference `bar`: the close of the symbol's last bar completed by then (start + 1 minute at or
  before the quote's provider time or the decision time; a revised bar's last version).
  Reference `quote`: the ask of the stream quote before it, as the bot's stream inbox keeps them
  (a quote older than the newest one is skipped); for a decision, the quote before the newest one
  received by then. The live collar refuses a previous quote older than the quote age limit;
  the tool measures every pair as captured.
- `collar.csv`: one row per entry kind, reference and symbol, plus `symbol = all`: `count`,
  nearest-rank `p50`, `p90`, `p99`, `p99_9`, and `max`.
- `collar_summary.json`: the `all` rows, `missing_reference` (entry prices with no completed bar
  or no earlier quote: the bot refuses those as `PRICE_COLLAR_UNKNOWN` while the collar is on)
  and `partial_last_lines`.

A maximum just above the far tail (`p99_9` or `max` of the days measured) refuses only prices no
captured day produced; a lower one also refuses real moves. Snapshot quotes are not read. A
malformed line is refused with file, line and cause.

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

## calibration: "70 means 70" (R29)

```
python -m tickr.research calibration --answers answers.csv --settings config/research.toml \
  --out out/calibration
```

Input: one CSV of stored model answers on months the model never saw (the caller picks the
months; the tool cannot tell). Header row; `outcome` = 1 if the event the probabilities predict
happened, else 0; every column named `p_<name>` is a probability in 0..1 (for example `p_layer1`,
`p_layer2`); other columns are ignored. A blank probability cell means that layer gave no
answer for the row and is skipped (counted). Nothing writes this file yet: the training job or
the bot must export it. A bad value is refused with file and line, never clipped.

Per probability column, a table of `calibration_buckets` equal buckets (10 = tenths; p = 1 goes
in the last): count, mean predicted, observed rate and gap (observed - predicted). Outputs:
`calibration.csv` and `calibration_summary.json` (per column: answers, skipped, base rate,
Brier score, expected calibration error = count-weighted mean absolute gap). Report only: no
pass/fail threshold exists until the owner chooses one.

## fill-quality: real fills against the assumptions (M4)

```
python -m tickr.research fill-quality --database data/tickr.sqlite3 [--environment NAME] \
  [--quotes capture/<day>/quotes.jsonl ...] [--label sandbox] \
  [--compare-database other.sqlite3 [--compare-quotes ...] [--compare-environment NAME] \
   [--compare-label production]] [--replay-settings config/replay.toml] [--config config/bot.toml] \
  --settings config/research.toml --out out/fill-quality
```

Works on the records of any environment: the environment is only which database (and optional
`--environment` filter on its runs) you point it at; the code never names one. Read only, over
closed trades with a fill, days = New York date of the close. Positive = worse than the reference.

- **Entry slippage**: fill - entry limit, and fill - ask of the last captured quote received at
  or before the entry decision (the `order-intent` journal row) on the same day. Needs
  `--quotes`; without a quote the trade has a `cause` and is left out of that figure. The quote's
  age is in the trades CSV, never cut off.
- **Exit and stop slippage**: stops are R5's measure (`slippage`), reused as is. A market exit is
  its decision's bid - its fill (needs `--quotes`).
- **Fees**: the broker's recorded fees on all filled orders of the trade, per trade and per share
  filled. A trade with any unrecorded fee is counted in `trades_without_fees`, never as free.
  The replay and the backtest are gross of fees (no fee model in config), so the fee "assumption"
  is none and the compare file leaves it empty; fees are shown as measured.
- **Fill delay**: `order-attempted` to the first `order-observed` row with fills (entry and
  market exit; a resting stop's wait is not a delay). The bot polls, so this is an upper bound.

Percent figures are share-weighted: sum(slip x shares) / sum(reference x shares) x 100. The
`assumed` column of the compare file is the replay's `slippage_percent` (the measured one when
`measured_slippage_file` is set) for the three quote and stop slippage figures.

Outputs: `<label>/fill_quality_trades.csv`, `<label>/fill_quality_days.csv` (a row per day,
then `all`), `fill_quality_compare.csv` (metric, assumed, a column per set, and `difference` =
second minus first when two sets are given, e.g. Sandbox now against the assumptions, Production
against Sandbox later) and `fill_quality_summary.json`.

## gate: R23 Step 0 descriptive report

```
python -m tickr.research gate --trades trades.csv --covered-days days.csv \
  --start 2026-08-01 --end 2026-10-31 --all-sessions-usd 4000 \
  --model-version NAME --results simulated --settings config/research.toml --out out/gate
```

An offline, DESCRIPTIVE report on one model version's closed trades, simulated (the exam) or real
(`--results` labels which). Nothing is promoted from it and it never says a gate is passed: each
check "holds", "does not hold" or is "inconclusive". Binding promotion criteria are set with the
owner on open development evidence before any exam read (`docs/promotion.md`).

**Frozen before evaluation.** `--start` and `--end` (inclusive) and the repository's session
calendar (`[session] calendar_file` of `--config`) are fixed before anything is read; the summary
records both dates, the calendar's coverage and the sha256 of each calendar file. A trade or a
covered day outside `--start..--end` is refused (`outside_period`); a day the calendar does not
cover is refused with the calendar's cause. The caller picks the rows: the tool cannot tell which
months the model saw.

**Coverage.** `--covered-days` is a CSV with a `day` column: the calendar trading days (half days
count as one) on which the producer had data and ran the model. A calendar trading day missing from
it is missing coverage; a covered day without a trade is an observed day with zero trades. A
non-trading or repeated day is refused, and so is a trade on a day not in the file
(`not_covered`). There is no coverage threshold, the rule is structural: a window month is
`observed` only when EVERY calendar trading day of that month inside `--start..--end` is in
`--covered-days`; otherwise it is `insufficient_coverage`; a window month that ends before
`--start` is `outside_period`. The whole period follows the same rule: one in-period trading day
without data makes every check inconclusive. A partial month stays visible: each window month
reports its trading days, its in-period days and `period_fraction_pct` (in-period trading days /
the whole month's trading days, e.g. a month cut by `--end`), its covered days and `coverage_pct`
(covered / in-period trading days). An observed month with zero trades has net 0 (not in plus); a
month that is not observed makes the months check inconclusive.

**Trades CSV**, one row per actual sized trade; header required, other columns ignored:

- `trade_id` (unique), `symbol`, `session` (`regular` or `extended`);
- `entry_time`, `exit_time`: ISO 8601 with a UTC offset; the trade's day is the New York date of
  its entry. Both entry and exit must be inside `--start..--end` (`outside_period`, e.g. an exit a
  year after a report's end). Same-day flatten: the exit must be on the entry's New York day, by
  that day's extended close in the calendar (`not_same_day`); an exit before the entry is refused;
- `session` must match where the entry falls in that day's calendar sessions: `regular` in
  regular_open..regular_close, `extended` in premarket_open..regular_open or
  regular_close..extended_close (half days use their own closes); otherwise `session_mismatch`;
- `notional_usd`: the trade's actual sized notional, above 0 and at most
  `[risk] position_notional_cap_usd` (above it: `limit_breach`);
- `return_units`: `pct_of_notional` (percent of `notional_usd`) or `usd`; `result` and the three
  cost columns are in these units;
- `result`: the trade's result AFTER the costs listed in this row (signed);
- `fees`, `slippage`: the fees and slippage already taken off `result`;
- `spread`: the bid/ask spread cost of the round trip (entry and exit);
- `spread_in_result`: `true` when `result` already includes the spread (fills at bid/ask, every
  real fill): the report charges nothing more. `false` (a result priced at the midpoint): the report
  takes `spread` off once. So the spread is never charged twice;
- `cost_provenance`: `broker_fills` (real fills, broker fees; must have `spread_in_result` true) or
  `simulated` (a cost model); `cost_version`: the cost model's or export's version (required).

Cost sign: every cost is positive when it reduced the result; `fees` and `spread` are never below 0,
`slippage` below 0 is a price improvement. An EMPTY cost cell is unknown, `0` is a valid zero. An
unknown fee or slippage (or an unknown spread on a `spread_in_result` false row) makes the trade's
net unknown: it is left out of the sums, counted in `unknown_cost_trades`, and its line is
inconclusive. The report never fills in a cost. The repository has no fee model (the replay and the
backtest are gross); for real trades the costs are the broker's recorded fees (`fill-quality`).
Nothing writes this file yet: the exam job or a later export must.

**Money lines** (dollars only DESCRIPTIVE: summed per-trade results, no replay of cash, slots,
repeated symbols, protection, session limits or actual exits, so they cannot count for promotion):

- `main_session`, label `descriptive`: `regular` rows at their actual `notional_usd`;
- `all_sessions`, label `hypothetical`: every row rescaled to `--all-sessions-usd` (the hypothetical
  4,000 USD line), shown "regular only" while the file has no extended trade.

Each line shows earned on winners, lost on losers, net, fees, slippage, the spread already in the
results, the spread the report charged, days in plus, and the same-trade spread cost.

**Same-trade spread cost** (a cost DIAGNOSTIC, not a baseline): the spread paid on the model's own
trades at an unchanged midpoint. `same_trade_matched` counts the trades with a known net AND a
known spread; `same_trade_spread_cost` is the spread in dollars over those trades only, so both
sides of any comparison use the same rows; `net_before_spread` (net plus that cost) is shown only
when every trade on the line is matched and is empty otherwise (an incomplete delta is never
printed: with two trades, one of unknown cost, a 10 USD net is not compared with the spread of
both).

**Paired baseline: unavailable.** A real paired baseline (for example tightest spread) must select
independently from the same pre-outcome candidates each minute with the same execution, dates,
costs, quota and constraints. The trade rows cannot reconstruct those opportunities, so `gate.csv`
and the summary report `paired_baseline` as `unavailable`; a version-bound matched-baseline input
is future work.

**Checks**, per line (`gate.csv`, kind in brackets):

1. `net_after_costs_above_0` (descriptive);
2. `sensitivity_k_of_m_months` (sensitivity): net above 0 in at least `gate_positive_months` (K) of
   the last `gate_window_months` (M) calendar months, anchored to the month of `--end`, months after
   the last trade included;
3. `sensitivity_without_best` (sensitivity): net still above 0 after removing the best
   `gate_drop_best_percent` (X) percent of the line's trades by after-cost result (count rounded up,
   so 1% of 150 trades removes 2).

A check is `inconclusive` when an in-period trading day has no data, the line has zero assessable
trades, or a cost is unknown (and, for the months check, when a window month is not observed); an
empty trades file is inconclusive, not refused. **Tail concentration** (`gate_tails.csv`): for each
of `gate_tail_percents` (1, 5, 10), the top that percent of trades by after-cost result (count
rounded up), their net in dollars and their share of the line's net (empty when the net is not
above 0), because the model targets rare large gainers. The other R23 items (every feature
available live, stop chosen, decision frequency chosen) are not numbers: they are listed as
"checked by hand".

Refused with a typed cause: `missing_column`, `not_finite`, `bad_value` (not a number, a bad
time, session, unit, provenance or flag, a negative fee or spread, a notional not above 0,
`--start` after `--end`, a trade on a non-trading day), `duplicate`, `outside_period`,
`not_covered`, `not_same_day`, `session_mismatch`, `limit_breach`, and the calendar's causes. Outputs: `gate.csv` (line, check, kind, outcome, measured, wanted, why
inconclusive; then the unavailable paired baseline and the items checked by hand), `gate_lines.csv`, `gate_months.csv` (status,
trading, in-period and covered days, period fraction and coverage percent, trades and net per
window month and line),
`gate_tails.csv`, `gate_trades.csv` (the per-trade cost audit: identity, times, notional, units,
every cost as given, the spread charged, net and spread in dollars) and `gate_summary.json` (the
frozen inputs, sensitivity parameters, coverage, cost provenance and versions with trade counts,
`conclusive`, and each line).

## Settings

`config/research.toml` `[research]` (one comment per key, tested; missing or unknown keys are
refused): `horizons_minutes`, `atr_period`, `session_timezone`
(`latency` reads only the last), `calibration_buckets` (`calibration` reads it),
`gate_positive_months`, `gate_window_months` and `gate_drop_best_percent` (`gate` reads them as
SENSITIVITY parameters K, M and X, never pass or fail; the backlog R23 numbers: 2 of 3 months,
best 1%) and `gate_tail_percents` (`gate`).
`[sealed]` holds the sealed exam window (see "Sealed exam window" above); the `--settings`
file may leave it out, because only the repository's own file is read for it.
What-if and `decide` use `config/replay.toml`, so they fill and replay exactly like the replay. Every
report also rounds to its `output_decimal_places` (`--replay-settings` with `--config`, default
`config/replay.toml`): that number has one home.

Exit code 2 and `research refused: <cause>` on any bad input; nothing is guessed.
