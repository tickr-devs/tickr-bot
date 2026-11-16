# Roadmap and backlog (versions after v1)

Nothing here is built unless a task names its id. v1 is `docs/spec-v1.md`; this file says what comes
after it, in which version, and when a version is done. Old-tracker answers quoted here are not owner
decisions until recorded in `docs/decisions.md`. The full original product spec lives in the old
`Trading` project at `.scratch/claude-review/product-spec/PRODUCT-SPEC.md` (read-only reference).

## Rules for this file

- Every item has a stable id (`R3`, `T7`, ...). Ids are never reused or renumbered; a moved item keeps
  its id. Prefixes: `R` strategy research, `T` trading and safety, `S` settings, `M` real money,
  `P` standalone product, `F` legal and business, `X` later trading features, `Q` open owner question.
- Versions are built in order, and development never waits for data (owner, 2026-11-16). A version
  is done when its items are merged, reviewed and released; the released version keeps running and
  collecting data while the next one is built, and several versions may run side by side. Only
  broker and SDK probes are waited for: probe, record the fact, move on. Results gathered by a
  running version feed later decisions (R1, the enabling day of v4); they never block a merge.
- Every setting has exactly one source of truth (owner, 2026-11-15). No item may add a value that
  already lives somewhere else.
- The standalone product (v5) comes before anything legal or commercial (v6), and v6 happens only if
  the owner still wants it after v5.
- Done items move to "Done" at the end with the pull request that did them.

## Overview

| Version | Goal | Done when (built, not waited for) | Keeps running to collect |
|---|---|---|---|
| **v1** (current) | The bot trades safely in the Sandbox and records everything | the Monday probes are answered and v1 is released | Sandbox days, broker vs our records |
| **v1.1** | Tools to find a strategy with an edge | R1-R5 merged | observer and replay results per candidate |
| **v2** | Unattended Sandbox: the bot can run a day alone, also outside the regular session | T1-T14, X3, X4 merged and released | unattended Sandbox days |
| **v3** | The owner sets up and runs the bot from the apps; several positions | S1-S7, X1, X2 merged in both apps (X5 only if its condition holds) | |
| **v4** | Real money at small size | M1-M5 merged; enabling Production stays an owner decision | Production days, real fills |
| **v5** | Standalone Mac product (still for the owner's own use) | P7 passes on a clean Mac | |
| **v6** | Selling it: legal and business | only if the owner still wants it after v5; F2 gates the rest | |

Why this order: the biggest risk is that no strategy has an edge after costs (every family tested on
history so far is negative, `docs/strategy-ledger.md`). So the research tools come right after v1,
and strategy research goes on in every later version beside the build work.

---

## v1: Sandbox proof (current)

Goal, scope and stages: `docs/spec-v1.md`. What is left:
- **V1** Monday 2026-11-16 Sandbox probes (`docs/monday-sandbox.md`), including F5 "replace a working
  stop", which T1 needs.
- **V2** Release v1 and keep it running on Sandbox days. The 5 clean days of spec section 1 are
  collected while v1.1 and v2 are built; they do not block them.

## v1.1: Tools to find an edge

Goal: the tools and rules to tell a strategy with a positive net result after costs, on data it was
not fitted on, from a fitted one. Scope: offline research tools and strategy versions only; nothing
in the order, risk or session path changes. Strategies still never send orders (rule 8). R6-R12 are
research runs and candidates: they go on in every later version and never gate a merge.

- **R1** Promotion ladder with written criteria: history, observer for N sessions, active in Sandbox,
  real account at small size, full size. Answers Q3 ("what counts as Sandbox proved the strategy").
- **R2** Walk-forward run as the standard evaluation, with a counter of how often each data set and
  hold-out was used.
- **R3** Parameter sweep and sensitivity report: result against each parameter moved by one step; a
  strategy must sit on a plateau, not a peak.
- **R4** Fewer parameters: new strategies test one hypothesis with at most about 10 parameters
  (price-action-v1 has 23).
- **R5** Slippage measurement: planned loss against the realized loss of every stopped trade, per
  day; replay and backtests use the measured value instead of the fixed 0.05%. Stop overshoot: read
  trade prints (`ticks/list`) after the session for symbols with own fills, since Webull triggers
  stops on the last trade (old #087).
- **R6** Pre-registered R21 test (screener rank 21 and lower, `tickr/b4b5/preregistered.txt` in
  project files) on the May-Aug data, run once exactly as registered.
- **R7** Recheck on recorded live days: screener rank and source vs outcome (old Q46), acceleration
  check (old #153-33). Both studied on history 2026-11-15 (`docs/strategy-ledger.md`): rank fades,
  source predicts nothing, acceleration does not help.
- **R8** Candidate `price-action-v2` (no-progress exit, old Q43; trigger confirmation, old #153)
  judged by replay and observers; provisional X and N (`docs/observers.md`).
- **R9** Per-variant evidence and missed-moves "trade of the day".
- **R10** Spike-entry setup block: built as the spike setup of `price-action-v3`, active since
  2026-11-16 (docs/decisions.md); judged like any other setup.
- **R11** Observers see what they would trade: discovery applies the union of the active strategy's
  and the observers' liquidity rules, so an observer with looser rules is not starved (left over
  from #403).
- **R12** Intra-minute breakout entry (owner, 2026-11-16): a strategy variant that enters on the L1
  stream when the breakout happens, instead of waiting for the M1 bar to close above it. Webull
  history has only minute bars, so it cannot be backtested. First step: record every L1 quote of
  WATCH symbols from the stream the bot already holds (no extra bars quota), check the capture
  (`quotes.jsonl`) misses none, then judge the variant by quote-level replay and as an observer.

Exit: R1-R5 merged. A strategy climbs R1's ladder as its observer and Sandbox days come in,
while later versions are built.

## v2: Unattended Sandbox

Goal: the bot runs a Sandbox day alone, protects open trades better than a fixed stop, and refuses
every entry it cannot justify. Trading-critical: every item needs an
independent review.

One source of truth (old group A):
- **T1** **Trailing stop / stop tightening** by replace, once V1 proves Webull's replace mechanics;
  the platform rule "a stop only moves toward the price". Starting values (old #153-34, Q10,
  provisional): best price since entry minus 1.5 ATR, at least the fill price after +1.0 R. Planned
  API: `docs/api-planned.md` section 2.
- **T2** Planned loss per trade (50 USD) lives only in `config/bot.toml [risk]`; remove
  `[sizing] planned_loss_usd` from the strategies, which receive it with their input (owner
  2026-11-15).
- **T3** Position notional cap (2,500 USD) likewise: remove `[sizing] max_position_notional_usd`.
- **T4** History sessions: the strategy declares its need (`prior_sessions_needed`); the bot and the
  replay load the largest declared need; remove `history_sessions` from `bot.toml` and `replay.toml`.
- **T5** A variant stores only its differences from the strategy it extends (`price-action-v2`
  repeats all 23 values of v1).
- **T6** Smaller repeats: `atr_period` in `config/research.toml`, `output_decimal_places` in
  `research.toml` and `replay.toml`.
- **T7** `tests/test_config.py` refuses a key that means the same thing at two levels. The Sandbox and
  Production tables repeat equal values on purpose (spec section 4, owner 2026-11-15); that stays.

Platform checks (old group B):
- **T8** Tradability and halts: instrument profile `status` and trade-instrument
  `trade_policy`/`forbid_reason` (CLOSE_ONLY, DENIED, HALTED) refuse entries with a typed cause and
  never block exits; probe first how a halt and the LULD band show in snapshot, profile and stream
  (old #158). A halt with a position open follows Q2. Before Production it also gates price-action-v3's spike setup, which
  enters right after a sharp move, where LULD pauses are most likely (Fable review of #476,
  2026-11-16).
- **T9** Money in the account as an admission check: buying power, settled cash, intraday margin
  (the account file shows the broker's buying power since 2026-11-16, display only). The pattern day trader rule is being replaced
  by an intraday margin standard (FINRA 4210 amendments effective 2026-06-04, broker phase-in to
  2027-10-20); check what the account enforces.
- **T10** Price collar: refuse an entry whose price is implausibly far from the last completed bar
  and the previous quote.
- **T11** Loss locks: weekly loss ceiling and a stop-out streak lock, beyond v1's per-symbol pause.
- **T12** Simulations obey platform rules: observers, replay and "what if" run through the same
  admission rules (position limit, pause after a loss, daily ceiling) from one shared code path, and
  the apps label each result with the rules it was computed under.
- **T13** Profit target and time exit as strategy rules.
- **T14** Notification settings in the apps (`docs/api-planned.md` section 4).

Trading outside the regular session (owner 2026-11-16, ids kept):
- **X3** Limit-exit chase before a market exit. Old-tracker answer, reconfirm when it starts: SELL
  LIMIT one tick through the bid, re-price every 10 s at most 6 times, never worse than 0.5 ATR from
  the first reference, then MARKET in the regular session (old Q22, Q110). Needed by X4: outside the regular session
  only limit orders work, so the exit has to chase.
- **X4** Extended hours and half days (owner wants them, 2026-11-16; own thread "Enable premarket and
  after-hours trading"). No overnight holding (owner, 2026-11-16): every position is flat before the
  extended session ends. Needs the PRE/POST stop probe and the owner's decision on protection
  outside the regular session. Old-tracker answer, reconfirm: limit orders only in PRE/POST, smaller
  size, no entries on stream or broker loss (old Q118, #020-12).

Exit: T1-T14, X3 and X4 merged with their probes answered (V1 for T1, the halt probe for T8, the
PRE/POST stop probe for X4), the
duplicate-key test green, v2 released and running unattended in the Sandbox.

## v3: Settings from the apps

Goal: the owner configures and runs the bot from the Mac and iPhone apps like a user; no owner
setting needs a config file. Order: design first, then macOS, then iOS.

- **S1** Settings schema: one record per setting (key, owner: platform or one strategy, type, unit,
  min, max, step, group, description in English and Russian, when a change applies, whether it needs
  biometrics) plus cross-field rules. TOML comments, validation, app forms and the advisor's limits
  all come from it.
- **S2** Three classes: owner settings (about 20 platform keys plus strategy parameters) in the
  apps; engineering settings (about 210 keys) file only; closed settings (environment, Production
  authorization, secrets) never changeable from an app.
- **S3** Strategy version computed by the bot as a fingerprint of the values; the owner only names
  it. Every decision and trade references the settings version.
- **S4** Settings API: schema, current values, validate a draft, apply, history, rollback; every
  change journaled with time and device.
- **S5** Apply rules: tightening protection applies at once; strategy parameters and the active
  strategy apply when flat, by a safe restart through normal startup and reconciliation; loosening
  protection applies from the next session and needs biometrics (owner 2026-11-15); an open trade
  finishes under the version it was opened with. No generic hot reload.
- **S6** App screens: Protection, Strategy, Rules in force (every limit with its level and value),
  Change history; a "platform rule" label and value on every refusal; a count of entries the platform
  stopped, by cause.
- **S7** Try before apply: replay a draft of strategy parameters on recent sessions from the
  Strategy screen (Lab).

Several positions and risk (owner 2026-11-16, ids kept):
- **X1** Several simultaneous positions, top-up. The cheapest of the three limits; needs the owner's
  risk-sharing design first, and a risk budget per strategy (share of the daily ceiling, position
  limit) before two strategies are active at once.
- **X2** Risk as percent of equity.
- **X5** Short selling, only if the short-side research finds an edge; until the owner lifts it the
  2026-10-23 ban stays. A large project; needs borrow and SSR facts. Old-tracker idea: Webull is the
  authority (no own SSR rule); record `shortable` as read and map short-sell rejections to typed
  causes (old Q107, #158).

Exit: S1-S7, X1 and X2 merged and shown in both apps; nothing the owner sets needs an edit of a file.

## v4: Real money at small size

Goal: the bot is ready to trade the real Webull account at small size. Enabling Production stays an
owner decision plus the authorization file (rule 5), never a code change.

- **M1** Production readiness view: authorization record form, readiness checklist
  (`docs/api-planned.md` section 3), enabling day.
- **M2** Account page completeness: done 2026-11-16 (balances, account kind, per-position P&L,
  today's fills with fees); left: fee evidence on real fills against the backtest assumptions.
- **M3** gRPC trade events (faster fill and order updates than polling), if the probe shows they are
  reliable.
- **M4** Slippage and fill quality on real fills against the Sandbox and backtest assumptions (R5
  applied to Production).
- **M5** Webull support questions: is there a documented Sandbox calendar endpoint; which real-time
  entitlement applies to Sandbox L1 (old #080).

Exit: M1-M5 merged. Which strategy goes live, and when, is the owner's decision on the enabling
day (R1's rungs inform it); Production days then run beside v5 work.

## v5: Standalone Mac product

Goal: a person with a Mac and a Webull account installs and runs Tickr without a terminal, a code
checkout or any developer tool. Still for the owner's own use; no legal or business step.

- **P1** One installable Mac app with the bot, local API and scheduled jobs: signed and notarized,
  bundled runtime; no Python install, no repository, no hand-written launchd files.
- **P2** First-run setup in the app: broker credentials typed once into the Keychain (never a file or
  a log), account check, alert channel (app push with the Apple Developer Program and APNs, Q6), phone pairing, preflight as a checklist.
- **P3** Running it from the app: start, stop, status and the reason the bot is not trading; settings
  only through v3 screens.
- **P4** Updates and data: in-app update that never runs with a position open, database migration
  with backup, export of logs and a diagnostics bundle without secrets.
- **P5** Strategies without code for the user: templates with parameters and the advisor; no visual
  strategy builder; new logic stays a developer task.
- **P6** User documentation in the app (English and Russian): setup, every setting, every refusal
  cause, what to do on each alert.
- **P7** Clean-machine test: install and a full Sandbox day on a Mac that never had the developer
  setup.

Exit: P7 passes.

## v6: Selling it (only if the owner still wants to after v5)

- **F1** Check the "Tickr" trademark, buy the domain, add a license file (the repository is public
  without one).
- **F2** Written answer from Webull on third-party use: the Trading API is for a person's own
  account; a product for others needs the Connect API (OAuth, by application). Nothing else here
  starts before the answer.
- **F3** Landing page and waitlist; continue only with real demand (about 200 emails in 2 to 3
  months; under 50 means stop).
- **F4** Company, securities lawyer (tool, not advice), direct sale of the notarized Mac app by
  subscription (App Store rule 3.2.1(viii) blocks trading apps from individuals), closed beta.
- **F5** Strategy marketplace: postponed until demand is proven (owner 2026-11-15).

## Owner questions

Open: none.

Answered (recorded in `docs/decisions.md`):
- **Q1** Manual trades in the bot's account are ignored: no fence, the bot keeps trading and never
  touches shares it does not own (2026-11-16, with 2026-11-15).
- **Q2** A halt, or a position that cannot close: keep the resting stop, alert, send no order into the
  halted book, close at the first chance once trading resumes, the next morning if forced
  (2026-11-16).
- **Q3** What counts as "Sandbox proved the strategy": R1's promotion ladder.
- **Q4** Captured quotes, bars and decisions are kept forever (archived as gzip, never deleted);
  low disk is caught by preflight's `[runtime] min_free_disk_mb` check (2026-11-16).
- **Q5** A new listing without relative-volume history is refused with a typed cause (2026-11-16).
- **Q6** iMessage is enough before unattended days; app push waits for v5 (2026-11-16).

## Done

Items from earlier versions of this file that are built:

- Native macOS app and iOS app over the local API (owner wish; stage 7).
- "What if we had entered" report: `tickr.research whatif` and `outcomes` (`docs/research.md`).
- "What if another strategy" replay: `tickr.replay`, `tickr.research decide` + `diff`
  (`docs/replay.md`), and the historical backtest (`docs/backtest.md`).
- Live observers (`docs/observers.md`).
- AI advisor that proposes a strategy version, never applies it (`docs/advisor.md`).
- `explain SYMBOL` command and API read (`explain-v1`).
- App screens from the old dashboard design: watching closely, size with its binding constraint,
  risk room left today, performance in R, signals not taken.
- Liquidity thresholds only in each strategy's `[liquidity]` (#403); R11 is the leftover.
- Trigger confirmation and no-progress exit built in candidate `price-action-v2`, not active (R8
  judges them).
- Screener rank and source, acceleration check: studied on history 2026-11-15 (R7 rechecks live).
