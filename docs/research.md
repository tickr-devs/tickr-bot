# Signal research: outcomes, what-if, version diff

`aperture.research` answers these questions offline, from captured M1 bars (the bar JSONL format
of `docs/replay.md`) and recorded decisions. It never trades. The bot imports only its record
format (`records.py`), through the observers (`docs/observers.md`).

| Command | Question | Label |
|---|---|---|
| `outcomes` | What did the price do 1/5/15/30/60 minutes after each WATCH and ENTER? | `observed` |
| `whatif` | What if we had entered on an ENTER that was refused or never evaluated? | `simulated` |
| `decide` + `diff` | What would another strategy version have decided over the same day? | (decisions) |
| `observers` | How did the live observer versions do against the active one today? | `simulated` |

Outcomes are price moves, never called profit. What-if results are simulated with the replay's
conservative fill model and prove nothing about real fills.

## Decision records (input)

One JSON object per line, written by `aperture.research.records.write_records` (the bot is
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
python -m aperture.research outcomes --bars data/capture/.../bars.jsonl \
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
python -m aperture.research whatif --bars ... --decisions decisions.jsonl \
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
python -m aperture.research decide --bars <history days + the day> \
  --strategy aperture.strategy.price_action_v1 --values strategies/<v2>/strategy.toml \
  [--replay-settings config/replay.toml] [--config config/bot.toml] --out out/v2
python -m aperture.research diff --baseline decisions.jsonl --candidate out/v2/decisions.jsonl \
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

## Settings

`config/research.toml` `[research]` (one comment per key, tested; missing or unknown keys are
refused): `horizons_minutes`, `atr_period`, `session_timezone`, `output_decimal_places`.
What-if and `decide` use `config/replay.toml`, so they fill and replay exactly like the replay.

Exit code 2 and `research refused: <cause>` on any bad input; nothing is guessed.
