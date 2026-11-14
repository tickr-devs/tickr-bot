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
- Extended hours (needs stop behaviour in PRE/POST proven), half days.
- Short selling (needs borrow/SSR facts).
- Risk as percent of equity.
- Loss locks (stop-out streak, cooldown) if the owner wants them.
- Limit-exit chase before market exit.
- Per-variant evidence, stop-overshoot measurement, missed-moves "trade of the day".
- Spike-entry setup block (owner wants it back as its own block; values open).
- Account page completeness (balances, buying power, all broker fields), fee evidence, gRPC trade events.
- Production readiness: authorization record form, readiness checklist, enabling day.
- Store maintenance (archive/compact closed days).

## Open owner questions (ask when they start blocking)
- Manual trades in the bot's account: forbidden by convention and fenced (recommended) or ignored?
- Position that cannot close before the close (halt): keep overnight and close at the first RTH opportunity (recommended)?
- What counts as "Sandbox proved the strategy" before Production is considered.
- Quote capture retention days and disk allocation (decide after one measured day).
- Missing relative-volume history for a new listing: refuse (recommended) or pass.
