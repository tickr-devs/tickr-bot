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

## From the old bot's tracker (reviewed 2026-11-14)
Ideas from the frozen `Trading` tracker (open and closed tasks) that Tickr does not have yet; old numbers in brackets. Old-tracker answers are not owner decisions until recorded in `docs/decisions.md`.

Strategy and research:
- **No-progress exit** as a strategy version: close when price has not moved X R in favour within N minutes; X and N were never chosen (old Q43); judge it by replay and observers first (old #083). Built as candidate `price-action-v2` with provisional X and N (docs/observers.md); not active.
- **Trailing values to start from** when the trailing stop is built: stop = best price since entry minus 1.5 ATR, at least the fill price after +1.0 R, only tightens; provisional, never confirmed (old #153-34, Q10).
- **Trigger confirmation**: two consecutive executable quotes through the trigger before ENTER, against one-quote flicker entries (old trading review of #153). Built in candidate `price-action-v2` (docs/observers.md); not active.
- **Acceleration check** (price velocity, dollar volume per minute) as a strategy rule, kept only if recorded days show it helps (old #153-33, ADR 0013). Studied on history 2026-11-15: it does not help (docs/strategy-ledger.md); recheck on recorded live days.
- **Screener source and rank vs outcome**: do the list (gainers/losers/top actives) or the rank predict the move? Study on recorded days (old Q46). History study 2026-11-15 (docs/strategy-ledger.md): the top gainer ranks fade, the list source predicts nothing; recheck on recorded live days.

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

---

## Later: path to product (after the current work)

Not started until the current work is finished: Monday Sandbox probes, the 5 clean Sandbox days of spec section 1, the open pull requests, and a strategy that shows an edge. Source: the 2026-11-15 report "Tickr: strategies, settings and the path to product" and the product assessment of the same day. Steps are in order; a later step does not start before the earlier ones.

Owner requirement (2026-11-15): every setting has exactly one source of truth. No value is written in two places, between platform and strategy or anywhere else; whoever needs a value reads it from its owner.

### A. One source of truth (remove today's duplicates)
1. **Planned loss per trade** (50 USD) is in `config/bot.toml` `[risk] planned_loss_usd` and in `[sizing] planned_loss_usd` of all four strategies. Keep it on the platform only (owner 2026-11-15); the strategy receives it with its input and keeps only its sizing formula.
2. **Position notional cap** (2,500 USD) is in `[risk] position_notional_cap_usd` and in `[sizing] max_position_notional_usd` of all four strategies. Same fix as item 1.
3. **Spread ceiling and session dollar volume**: done by pull request #403 (2026-11-15). The platform keys are gone; every liquidity threshold lives in the strategy, and discovery applies the active strategy's rules before loading bars. Left: discovery filters with the active strategy only, so an observer with looser liquidity rules never sees the symbols the active one refused; apply the union of the active strategy's and the observers' rules.
4. **History sessions** (5) is in `[market_data] history_sessions`, in `config/replay.toml` and implied by `relative_volume_sessions` / `prior_sessions` in the strategies. The strategy declares what it needs (`prior_sessions_needed` exists); the bot and the replay load the largest declared need.
5. **`price-action-v2`** repeats all 23 values of `price-action-v1` and adds 3. A variant stores only its differences from the strategy it extends.
6. **Smaller repeats**: `atr_period` in `config/research.toml`, `output_decimal_places` in `research.toml` and `replay.toml`.
7. **Test**: a key that means the same thing may not exist at two levels; `tests/test_config.py` enforces it. The Sandbox and Production tables repeat nine equal values on purpose (spec section 4: Production inherits nothing from Sandbox); that exception stays (owner 2026-11-15).

### B. Platform checks before real money
8. **Tradability and halts**: the item "Tradability check before entry" above, plus a probe of how a trading halt and the LULD band show in Webull data, no entry into a halted symbol, and the open owner question about a position held through a halt.
9. **Money in the account**: buying power, settled cash (cash account) and intraday margin (the pattern day trader rule is being replaced by an intraday margin standard: FINRA Rule 4210 amendments approved by the SEC on 2026-04-14, effective 2026-06-04 with a broker phase-in to 2027-10-20, per the WilmerHale client alert of 2026-04-23; unverified for Webull, so check what the real account enforces) as an admission check; `buying_power_usd` is read but always empty today.
10. **Price collar**: refuse an entry whose price is implausibly far from the last completed bar and the previous quote (one bad quote must not make a breakout).
11. **Weekly loss ceiling and a stop-out streak lock** (the "Loss locks" item above), before any unattended trading.
12. **Slippage measurement**: planned loss against the realized loss of every stopped trade, reported per day; replay and backtests use the measured value instead of the fixed 0.05%.
13. **Simulations obey platform rules**: observers, replay and "what if" run through the same admission rules (position limit, pause after a loss, daily ceiling) as the active strategy, from one shared code path, and the app labels each result with the rules it was computed under.
14. **Exit side**: the platform rule "a stop only moves toward the price" with the trailing stop; profit target and time exit as strategy rules.

### C. Settings as a product feature
15. **Settings schema**: one record per setting: key, owner (platform or one strategy), type, unit, minimum, maximum, step, group, description (English and Russian), when a change applies, whether it needs biometrics, plus cross-field rules. The TOML comments, the validation, the app forms and the advisor's limits all come from it.
16. **Three classes**: owner settings (about 20 platform keys plus strategy parameters) shown in the apps; engineering settings (about 210 keys) file only; closed settings (environment, Production authorization, secrets) never changeable from an app.
17. **Strategy version computed by the bot** as a fingerprint of the values; the owner only names it. Every decision and trade references the settings version.
18. **Settings API**: schema, current values, validate a draft, apply, history, rollback; every change journaled with time and device.
19. **Apply rules**: tightening protection applies at once; strategy parameters and the active strategy apply when the bot is flat, by a safe restart through the normal startup and reconciliation; loosening protection applies from the next session and needs biometrics (owner 2026-11-15: a raised risk limit never applies on the day it is raised); an open trade finishes under the version it was opened with. No generic hot reload.
20. **App screens** (design first, then macOS, then iOS): Protection, Strategy, Rules in force (every limit on a trade with its level and value), Change history; a "platform rule" label and value on every refusal; a count of entries the platform stopped, by cause.
21. **Try before apply**: replay a draft of strategy parameters on recent sessions from the Strategy screen (Lab).

### D. Strategy analysis tools
22. **Parameter sweep and sensitivity report**: result against each parameter moved by one step; a strategy must sit on a plateau, not a peak.
23. **Walk-forward run** as the standard evaluation, and a counter of how many times each data set and hold-out was used.
24. **Promotion ladder with written criteria**: history, observer for N sessions, active in Sandbox, real account at small size, full size. Answers the open question "what counts as Sandbox proved the strategy".
25. **Risk budget per strategy** (share of the daily ceiling, position limit), needed before two strategies are active at once.
26. **Fewer parameters**: new strategies aim for one hypothesis and at most about 10 parameters (price-action-v1 has 23).

### E. Standalone product, ready to distribute (still for the owner's own use)
Done when a person with a Mac and a Webull account installs and runs Tickr without a terminal, a code checkout or any developer tool. No legal or business step is needed for this group.
27. **One installable Mac app** that contains the bot, the local API and the scheduled jobs: signed and notarized, with its own bundled runtime; no Python install, no repository, no hand-written launchd files.
28. **First-run setup in the app**: broker credentials typed once and kept in the Keychain (never in a file or a log), account check, alert channel, phone pairing, preflight result shown as a checklist.
29. **Running it from the app**: start, stop, status and the reason the bot is not trading; settings only through the screens of group C; no config file to edit for an owner setting.
30. **Updates and data**: in-app update that never runs with a position open, database migration with backup, export of logs and a diagnostics bundle without secrets.
31. **Strategies without code for the user**: strategy templates with parameters and the advisor; no visual strategy builder; adding new logic stays a developer task.
32. **User documentation in the app** (English and Russian): setup, every setting, every refusal cause, what to do on each alert.
33. **Clean-machine test**: install and a full Sandbox day on a Mac that never had the developer setup.

### F. Selling it: legal and business (only if the owner still wants to after E)
34. Check the "Tickr" trademark, buy the domain, add a license file (the repository is public without one).
35. Written answer from Webull on third-party use: the Trading API is for a person's own account; a product for others needs the Connect API (OAuth, by application). Nothing else in this group starts before the answer.
36. Landing page and waitlist; continue only with real demand (about 200 emails in 2 to 3 months; under 50 means stop).
37. Company, securities lawyer (tool, not advice), direct sale of the notarized Mac app by subscription (App Store rule 3.2.1(viii) blocks trading apps from individuals), closed beta.
38. Strategy marketplace: postponed until demand is proven (owner decision 2026-11-15).
