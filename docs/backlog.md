# Backlog (after v1)

Out of scope for v1. Nothing here is built unless a task names it. The full original product spec lives in the old `Trading` project at `.scratch/claude-review/product-spec/PRODUCT-SPEC.md` (read-only reference).

## Owner's standing wishes (decided, scheduled after v1)
- **Trailing stop / stop tightening** by replace, once a probe proves Webull's replace mechanics. First item after v1.
- **Native macOS app (SwiftUI)** over the local API; then **iOS** with push notifications for entries, stops, failures. Needs Xcode on a Mac (owner's machine or a macOS CI runner); cloud agents cannot build or verify it.
- **"What if we had entered" report**: for every recorded signal not taken, price path after 1/5/15/30/60 minutes, whether the stop would have been hit, result in percent and R, labelled as a calculation.
- **"What if another strategy" replay**: run recorded days (screener rows, M1 bars, L1 quotes) through another strategy version and compare; replay of the live version must reproduce live decisions exactly.
- **Live observers**: several strategy versions compute decisions live next to the active one, never trade; decisions tagged `observer`.
- **AI advisor**: reads exported signals, versions and results (no credentials, no account id), proposes a new strategy version as an experiment; first checked by replay and observers; the owner applies it. Claude API.

## Later product features
- Several simultaneous positions (risk sharing design needs the owner first), top-up.
- Extended hours (needs stop behaviour in PRE/POST proven), half days. Old-tracker answer, reconfirm with the owner when this starts: limit orders only in PRE/POST, smaller size, hard flatten before the session ends, no entries on stream or broker loss (old Q118, #020-12).
- Short selling (needs borrow/SSR facts). Old-tracker idea: Webull is the authority (no own SSR rule); record `shortable` as read and map short-sell rejection texts to typed causes (old Q107, #158).
- Risk as percent of equity.
- Loss locks beyond the v1 per-symbol pause after a loss (for example a stop-out streak lock) if the owner wants them.
- Limit-exit chase before market exit. Old-tracker answer, reconfirm with the owner when this starts: SELL LIMIT one tick through the bid, re-price every 10 s at most 6 times, never worse than 0.5 ATR from the first reference, then MARKET in the regular session (old Q22, Q110).
- Per-variant evidence, stop-overshoot measurement, missed-moves "trade of the day". For overshoot: read trade prints (`ticks/list`) after the session for symbols with own fills, since Webull triggers stops on the last trade (old #087).
- Spike-entry setup block (owner wants it back as its own block; values open).
- Account page completeness (balances, buying power, all broker fields), fee evidence, gRPC trade events.
- Production readiness: authorization record form, readiness checklist, enabling day.
- Store maintenance (archive/compact closed days).

## From the old bot's tracker (reviewed 2026-11-14)
Ideas from the frozen `Trading` tracker (open and closed tasks) that Aperture does not have yet; old numbers in brackets. Old-tracker answers are not owner decisions until recorded in `docs/decisions.md`.

Strategy and research:
- **No-progress exit** as a strategy version: close when price has not moved X R in favour within N minutes; X and N were never chosen (old Q43); judge it by replay and observers first (old #083). Built as candidate `price-action-v2` with provisional X and N (docs/observers.md); not active.
- **Trailing values to start from** when the trailing stop is built: stop = best price since entry minus 1.5 ATR, at least the fill price after +1.0 R, only tightens; provisional, never confirmed (old #153-34, Q10).
- **Trigger confirmation**: two consecutive executable quotes through the trigger before ENTER, against one-quote flicker entries (old trading review of #153). Built in candidate `price-action-v2` (docs/observers.md); not active.
- **Acceleration check** (price velocity, dollar volume per minute) as a strategy rule, kept only if recorded days show it helps (old #153-33, ADR 0013).
- **Screener source and rank vs outcome**: do the list (gainers/losers/top actives) or the rank predict the move? Study on recorded days (old Q46).

Broker and safety features:
- **Tradability check before entry**: instrument profile `status` (seen OC/CO/NT, ignored today) and trade-instrument `trade_policy`/`forbid_reason` (CLOSE_ONLY, DENIED, HALTED) refuse entries with a typed cause and never block exits; first probe how a halt shows in snapshot, profile and stream (old #158).
- **Webull support questions** (owner): is there a documented Sandbox calendar endpoint; which real-time entitlement applies to Sandbox L1 (old #080).

Operator and app:
- **`explain SYMBOL`** command and API read: the latest decision for a symbol with every check (measured vs threshold) and the admission cause (old #153-18).
- **App screens from the old dashboard design**: watching-closely list with the decisive check and quote age; size with its binding constraint ("Risk" or "Position cap"); risk room left today; performance in R (expectancy, win rate, profit factor, drawdown); signals not taken (old #154, #216, #140-26).

## Open owner questions (ask when they start blocking)
- Manual trades in the bot's account: forbidden by convention and fenced (recommended) or ignored?
- Position that cannot close before the close, or a halt with a position open: keep the resting stop and report, send no flatten, cancel or replace into the halted book, keep overnight if needed and close at the first RTH opportunity (recommended; matches old-tracker answer Q108)?
- What counts as "Sandbox proved the strategy" before Production is considered.
- Quote capture retention days and disk allocation (decide after one measured day).
- Missing relative-volume history for a new listing: refuse (recommended) or pass.
- Phone alert before the iOS app: is a simple push channel needed before unattended trading days, or are macOS notifications enough until then (old #147, product-spec Q7)?
