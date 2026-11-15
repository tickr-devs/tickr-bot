# Strategy ledger: what was tested on history

Every number here is SIMULATED. Read this before proposing a new strategy idea, so that nobody
retests a family that already failed. Add a row whenever you test something new. The method
comes first, then the results.

## Method (2026-11-15 run)

- **Data:** the `aperture.backtest` fetch (`docs/backtest.md`) of regular-session M1 bars,
  2026-08-17 to 2026-11-13. Symbols A to S only (2400 of the pool's symbols), because the fetch
  was still running.
  - Gainer days: 9521 symbol-days from `pool.csv`, meaning the day high was at least 5% above
    the previous close and the day's dollar volume was at least 5M USD.
  - Loser days: 6379 symbol-days from the history sessions in the same files, meaning the low
    was at least 5% below the previous close, with at least 5M USD of dollar volume. This
    sample is **biased**: every symbol in it became a gainer within the next 5 sessions. Its
    dates start 2026-08-11, before the window, because the history sessions do.
  - Market proxy: the equal-weight return against the previous close of every non-gainer
    symbol-day priced at least 5 USD with at least 10M USD of dollar volume. SPY and QQQ are
    not in the data, because ETFs are excluded.
- **Gate:** the same rule as `backtest/gate.py`. The first completed bar whose close is 5%
  above the previous close (5% below for losers) and whose session dollar volume reached
  5M USD. A signal is decided on a completed bar at or after the gate, and the entry is the
  next bar's open. Last entry is 15:45 and flatten is 15:55.
- **Costs:** per side, the larger of 0.15% of the price and 0.015 USD (half spread plus
  slippage). The median round trip is 0.30%. Shorts carry an extra 0.2 to 0.3% for borrow.
- **Periods:** fit runs before 2026-10-01, validation from 2026-10-01 to 2026-10-15, and test
  from 2026-10-16. The test period is the backtest's hold-out and had already been seen for
  price-action-v1. A candidate counts only if fit and validation agree. Test was meant to be
  run once on candidates written down beforehand. It was seen for the pre-registered candidates
  A, B and C, and again in the review rerun after the simulator fix. Several scripts also print
  it.
- **Interval:** 95% percentile bootstrap of the mean, resampling whole days.

## Results

All are long unless marked short. Values are mean net return per trade.

| Family | Variants | Result | Verdict |
|---|---|---|---|
| Buy gainers at the gate, hold to close | price, change, gap, rel. volume, VWAP, distance from high, time of day (quintiles) | Gross about 0 overall; strongly negative for price under 5-10 USD, change over 15%, gap over 10%, extreme rel. volume, below VWAP, more than 5% off the high | No edge. Gainers drift down after the gate; use these buckets as filters |
| ML scan (LightGBM, 16 features, every 15 min after the gate) | net-cost target, 30 min / 60 min / close horizons, 5-fold walk-forward | Top 5% long picks: -0.45% (60 min), -0.74% (close) | No long edge |
| Short fade of extended gainers (info only: shorts are banned, decisions 2026-10-23) | change >= 15/30/50% at 10:00-11:00, stop at HOD+2% / +10% / +25%; VWAP-loss confirmation | With a stop, fit and validation flip sign. Gaps through the stop (halts) still cost up to -67% to -94% on one trade. Unstopped shorts held to the close (the ML scan's short picks) average positive, but one squeeze lost 392% | No stable edge |
| 5-min opening-range breakout, close-confirmed ("stocks in play", rel. volume >= 1.5-3, price >= 10 / 20) | stop at OR low / half OR; signal on a bar closing above the OR high, entry at the next open | Fit about +1.0 to +1.4%, validation about -1.0%, test about 0 | No edge |
| 5-min opening-range breakout by buy-stop (pre-registered A and B: rel. volume >= 2, price >= 20) | stop at half OR (A) / OR low (B); 15-min OR also tried | Corrected for the fill bug below: test +0.32% (B) and -0.12% (A), both intervals include 0 | No edge |
| Breakout by a resting buy-stop (pre-registered C: 5-bar high, 09:35-11:00, price >= 20) | skip if the bar opens more than 0.5% above the trigger; stop at the 5-bar low, at least 0.5% below entry; costs 0.15 / 0.30% | First reported +0.5 R. A review found the simulator bought at the bar open instead of the trigger and allowed signals on the gate bar itself. Corrected: fit +0.19%, validation -0.43% [-0.96, -0.04], test +0.33% [-0.28, +1.00]. With fill-at-trigger alone, before the gate fix, it was +0.14% at 0.2% slippage; with both fixes it is below 0 | Refuted. Only a live fill measurement could revive it |
| Losers: buy at the gate, VWAP reclaim, VWAP reclaim after -10%, 15-bar high after a -10% flush | price >= 5 / 20; hold 60 / 90 min or EOD with a stop at the day low | All -0.3% to -1.0%, even on the biased sample | No edge. Losers keep falling intraday |
| Regime filter (market proxy level and 30-min trend at entry) | gainers at the gate and 5-bar breakout, losers at the gate; hold 30 / 60 / 90 min / EOD | No regime is positive in both fit and validation | No edge |
| Holding period 30 / 60 / 90 min instead of stop / target | same families | Same sign as the EOD result | No edge |

The backtest thread's own results for price-action-v1 are in `docs/backtest-results.md`.

## Reproducing

The research scripts are offline and are not part of the bot. They live in the project's shared
folder `aperture/edge-research/`: `prep.py` (gainer cache), `prep2.py` (loser days and market
proxy), `lib.py`, `sim.py` (fixed: explicit entry price), and one script per family. The
pre-registrations are in `preregistered.txt`.

## Not tested yet, and why

- **Top-actives continuation:** there is no screener history. It needs a universe fetched by
  dollar volume rather than by change.
- **Days after a gainer (day 2):** not in the fetched sessions.
- **Halt-resume plays:** halts show only as missing minutes. This needs a reliable halt flag.
- **Sub-minute entries:** the bot evaluates on completed M1 bars. A buy-stop entry needs a
  platform change and a live slippage measurement first.

## Next

Rerun the fit / validation / test protocol on the full symbol list and on May to August once
the backtest thread has fetched them. May to August gives a fresh hold-out that nobody has seen.
