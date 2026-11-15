# Backtest results: price-action-v1

All numbers are SIMULATED (see docs/backtest.md for the method and its limits).

## Interim run, 2026-11-15

Window 2026-08-17 to 2026-11-13 (64 sessions). Minute bars were available for 600 of the
pool's symbols only, alphabetically A to C, because the read-only fetch was still running.
2613 symbol-days were replayed; 14258 had no minute file yet.

| view | trades | win rate | mean R | total R | profit factor |
|---|---|---|---|---|---|
| every signal | 148 | 29.7% | -0.293 | -43.32 | 0.50 |
| bot limits | 65 | 33.8% | -0.243 | -15.82 | 0.54 |

- Mean R, 95% interval with days resampled: -0.451 to -0.126. The interval is below 0.
- Before 2026-10-16: 89 trades, mean -0.426 R (-0.604 to -0.217). Hold-out from 2026-10-16:
  59 trades, mean -0.092 R (-0.275 to 0.120, includes 0).
- Bot view: -734 USD gross over 53 days, max drawdown 832 USD.

### Where the loss comes from

Checked on the same 148 trades with their minute bars:

- Stops fill close to plan: mean -1.03 R on stop exits. Slippage is not the problem.
- Other exits do not rescue it. Fixed target at +1, +1.5, +2, +3 R, breakeven after +1 R,
  or a 1 R trailing stop: none is positive on both periods (best: +1 R target, +0.00 R
  before, -0.14 R on the hold-out).
- Stopped trades rarely moved in favour first: median favourable excursion +0.22 R, a
  quarter never traded above the entry.
- A second entry in the same symbol on the same day: 12 trades, 1 win, mean -0.86 R.

Conclusion so far: the entry (buying a 5-bar high on a stock already up 5%) has no edge on
this sample; a new strategy version needs a different entry idea, not new exit numbers.

### Not yet covered

- Symbols D to Z (fetch running). The pool rule in this branch selects 3301 symbols; the
  running fetch was started with an earlier pool of 2815, so about 486 symbols need a second
  `fetch-minutes` pass before the full run.
