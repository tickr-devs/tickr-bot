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
| Literature set on an "actives" proxy (every fetched symbol-day with at least 20M USD of dollar volume and a previous close of at least 10 USD; pre-registered, fixed parameters) | L1 late-day momentum (15:25, up at least 3%); L2 noise-band momentum (Zarattini-Aziz-Barbon style); L3 afternoon HOD break after a 60-min base; L4 early volume shock (5-bar volume at least 5x normal, day +1 to +3%); L5 first pullback after a 2% opening drive; L6 lunch dip | L1 -0.32%, L3 -0.23%, L6 -0.28%, L5 -0.03%. L4 (+0.11%) is positive in every period and L2 (+0.04%) in validation and test only, but that is look-ahead: on days that became gainers they earn +0.5..+1.3%, on the other days -0.35..-0.6% | No edge for L1, L3, L5, L6. L2 and L4 cannot be judged on this data; they need an actives universe chosen without look-ahead |
| Symbol hold-out of C, A and B (pre-registered 2026-11-15: the T-Z gainer days, 415 symbols and 1634 days that no earlier test used, all dates, rules unchanged) | entry on or after the gate bar (as pre-registered) / only strictly after it | C as pre-registered: +0.62% [+0.08,+1.21], n=374, and +0.30% [-0.23,+0.89] at 0.30% per side, so it passes the written criterion. Strictly after the gate bar: +0.36% [-0.19,+0.96]. On A-S strictly after: +0.16% [-0.15,+0.49]. A / B: n=37, +0.07% / +0.97% with intervals wide across 0 | No edge shown. The pass comes from entries on the gate bar itself (55% of them): the fill happens inside the bar whose close makes the stock a 5% gainer, which is look-ahead. The pool is ranked by the day's high change, which is look-ahead as well. This needs the liquid universe to decide |
| Liquid universe without look-ahead (pre-registered 2026-11-15: each session's top 300 by the previous session's dollar volume; exploration on the first 197 symbols fetched, 5570 symbol-days) | L1-L6 unchanged; C' = the 5-bar-high buy-stop of C without the gainer gate | Every family is negative in fit, validation and test at 0.15% per side: L2 -0.35% (gross -0.05%), L4 -0.20% (gross +0.10%), C' -0.32% [-0.41,-0.23] and still -0.12% at 0.05% per side; L1, L3, L5 and L6 -0.22% to -0.44% | No edge. The L2 and L4 results on the actives proxy came from look-ahead. Nothing qualified for the hold-out run on the remaining symbols |
| Liquid universe, new families (pre-registered 2026-11-15 13:3x UTC, `preregistered-n.txt`; same exploration symbols, signal on a completed bar, entry at the next bar's open, every trade stopped, flat at 15:55; costs 0.05% per side for large caps, 0.10 / 0.15% as stress) | N1 worst 10% at 10:00 (opening reversal); N2 gap down 3% or more; N3 yesterday's loser (-4%); N4 last-30-min periodicity (Heston-Korajczyk-Sadka); N5 market first-half-hour momentum (Gao-Han-Li-Zhou); N6 VWAP stretch -2% on a calm market; N7 strong prior close; N8 failed opening-range breakdown; N9 best 10% at 10:30; N10 relative strength on red mornings; N11 worst 10% at 15:00; N13 large-cap gap-and-go; N14 first-hour compression breakout; N15 gap down filled in the first hour; N16 overnight hold 15:55 to next open; N17 LightGBM ranker at 10:00 / 11:00 / 13:00, trained on fit only; N19 day 2 after a news gap (+5% gap on 3x volume); N20 20-day leader on a red morning | At 0.05% per side: N4 -0.10% [-0.19,-0.01], N5 -0.13%, N8 -0.14% [-0.21,-0.07], N1 -0.38%, N2 -0.41%, N20 -0.51%, N9 -0.18%, N10 -0.17%, N14 -0.14%, N3 -0.25%, N6 -0.11% (test -0.33%), N7 -0.01% (fit +0.59%, test -0.76%), N11 -0.06% (fit -0.20%), N13 +0.06% (n=74, validation -0.84%), N15 -0.74% (n=16), N16 -0.12% [-0.33,+0.07] (two split nights excluded), N17 validation +0.15% and test -0.18% (prediction-label correlation 0.01). Only N19 passed exploration: n=21, +1.26%, positive in every period. Its one hold-out run (401 symbols not used before, 9312 symbol-days): n=57, +0.07% [-1.07,+1.57], -0.02% at 0.10% per side, median trade -0.07%, the mean carried by one +17% trade | No edge. N19 failed its hold-out. 18 hypotheses were tested (16 rule families, the N17 model and the N16 overnight hold; N12 and N18 were never defined): none gives a long, intraday edge on liquid stocks after realistic costs. N16 is outside v1, which flattens every day, and is reported only to close the question. The hold-out symbols were not chosen: they are every liquid symbol fetched by 13:39 UTC that is not in the exploration list (the fetch order is alphabetical, so they run DGX..RYTM). The 401 hold-out symbols are now spent for N19; the same run also printed N20 (-0.43% [-0.81,-0.06]), and no other family touched them |

**Data caveat found 2026-11-15** (`PoolEntry.previous_close_on_minute_scale` converts): `previous_close` in a pool file comes from forward-adjusted daily
bars while M1 bars are raw. On 27% of liquid rows the two scales differ by more than 0.05%
(dividends) and on 0.7% by more than 5% (splits, for example APH 2:1 showed as +100% gaps). The splits caused almost all of the gap bias: without rows whose gap exceeds 30%, the raw value already gives -0.01%. The
research scripts before `edge-research-2/` used the raw `previous_close`; `nfam.py` rescales it with
the day's adjusted close (`pc * minute close / daily close`). With the fix, the liquid universe's
mean overnight gap is -0.02%, not the +0.22% the raw value suggests. The bot's backtest already
skips split days (`SPLIT_BETWEEN_SESSIONS` in `aperture.backtest.run`); its small dividend bias on
the gate is unchanged.

Break-even cost per side, meaning the gross mean divided by two: about 0 or negative for every
gainer and loser family above; on the biased actives proxy, L2 0.17%, L4 0.21%, L5 0.13% and
L3 0.035%. Only L2 and L4 clear the assumed 0.15% per side, and only on the look-ahead sample.
At realistic wider spreads nothing does.

The backtest thread's own results for price-action-v1 are in `docs/backtest-results.md`.

L2 and L4 now run live as observers (`noise-band-v1`, `volume-shock-v1`, docs/observers.md).
Their live rules differ from the history test, so forward numbers are not directly comparable:
the stop is at least `minimum_stop_percent` below the entry window's floor and the bid; the
entry is any quote inside the window up to `signal_valid_minutes` after the signal bar,
not the next bar's open; a signal whose stop level trades before the entry is dropped; and a
session without its 09:30 bar is refused all day.

## Reproducing

The research scripts are offline and are not part of the bot. They live in the project's shared
folder `aperture/edge-research/`: `prep.py` (gainer cache), `prep2.py` (loser days and market
proxy), `prep3.py` (actives proxy), `lib.py`, `sim.py` (fixed: explicit entry price), `prep_tz.py` and `tz_check.py` (symbol hold-out), `prep_liq.py` and `lit_liq.py` (liquid universe), and one script per family. The
pre-registrations are in `preregistered.txt`. The N1-N20 round is in `aperture/edge-research-2/`
(`nfam.py`, `nfam2.py`, `n16b.py`, `n17.py`, `n19.py`, `eda.py`, and `preregistered-n.txt` with every
result appended in order).

## Not tested yet, and why

- **Days after a gainer (day 2):** not in the gainer fetch. On the liquid universe, day 2 after a
  news gap (N19) was tested and failed its hold-out.
- **More sessions:** 64 sessions give wide intervals; any small edge (under 0.1% per trade) is
  below what this data can show. The May-Aug fetch would roughly double the sample.
- **Halt-resume plays:** halts show only as missing minutes. This needs a reliable halt flag.
- **Sub-minute entries:** the bot evaluates on completed M1 bars. A buy-stop entry needs a
  platform change and a live slippage measurement first.

## Next

Rerun the fit / validation / test protocol on the full symbol list and on May to August once
the backtest thread has fetched them. May to August gives a fresh hold-out that nobody has seen.
