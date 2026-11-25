# Roadmap and backlog (versions after v1)

Nothing here is built unless a task names its id. v1 is `docs/spec-v1.md`; this file says what comes
after it, in which version, and when a version is done. Old-tracker answers quoted here are not owner
decisions until recorded in `docs/decisions.md`. The full original product spec lives in the old
`Trading` project at `.scratch/claude-review/product-spec/PRODUCT-SPEC.md` (read-only reference).

## Rules for this file

- Every item has a stable id (`R3`, `T7`, ...). Ids are never reused or renumbered; a moved item keeps
  its id. Prefixes: `R` strategy research, `T` trading and safety, `S` settings, `M` real money,
  `P` standalone product, `F` legal and business, `X` later trading features, `Q` open owner question,
  `V` v1 work, `A` apps, `O` owner's own setup tasks, `W` ticker.bot (site and cloud). Each item added from 2026-11-18 on carries a
  status: decided, proposed (owner has not confirmed), in progress, in review (PR), done, frozen
  (kept for the record, no work planned).
- Versions are built in order, and development never waits for data (owner, 2026-11-16). A version
  is done when its items are merged, reviewed and released; the released version keeps running and
  collecting data while the next one is built, and several versions may run side by side. Only
  broker and SDK probes are waited for: probe, record the fact, move on. Results gathered by a
  running version feed later decisions (R1, the enabling day of v4); they never block a merge.
- Every setting has exactly one source of truth (owner, 2026-11-15). No item may add a value that
  already lives somewhere else.
- The product (v5) comes before anything legal or commercial (v6), and v6 happens only if
  the owner still wants it after v5.
- ticker.bot (owner, 2026-11-19) comes only after the main bot and app work (v1 to v4), in order.
  Until then every item respects the design rules W9-W14 (owner-approved 2026-11-19), so the bot
  and the apps fit ticker.bot later without being redone.
- Done items move to "Done" at the end with the pull request that did them.
- Re-scope (owner, 2026-11-25): the bot trades the trained model (layer 1 plus layer 2), not hand-written
  strategies and observers. Items about strategies and observers are dropped or reworked; R28 removes
  the old strategies. The first model run is ACTIVE in the Webull Sandbox, with no shadow run, no
  comparison with the old strategy and no profit threshold before the Sandbox start (owner,
  2026-11-24). The apps follow mockup v126 (R14).

## Overview

| Version | Goal | Done when (built, not waited for) | Keeps running to collect |
|---|---|---|---|
| **v1** (current) | The bot trades safely in the Sandbox and records everything | the Monday probes are answered and v1 is released | Sandbox days, broker vs our records |
| **v1.1** | Tools to find an edge, and the model path | R1, R5, R15, R16, R35 and T12 merged (R5, R35, T12 core done) | replay and model results |
| **v2** | Unattended Sandbox: the bot can run a day alone, also outside the regular session | T1-T3, T6-T11, T13-T19, X3, X4 merged and released (T2, T3, T9, T16-T19 done) | unattended Sandbox days |
| **v3** | The owner sets up and runs the bot from the apps; several positions | S1-S6, X1, X2 merged in both apps (X5 only if its condition holds) | |
| **v4** | Real money at small size | M1-M5 merged; enabling Production stays an owner decision | Production days, real fills |
| **v5** | The product: ticker.bot (site, accounts, one cloud bot per user) and the apps signed in to it; standalone Mac scope settled in W1 | W1 spec agreed with the owner, W2-W8 and W15-W17 built | |
| **v6** | Selling it: legal and business | only if the owner still wants it after v5; O3 (Webull's answer) gates the rest | |

Why this order: the edge now comes from the model path (R15 to R30), not from hand-written strategies
(every family tested on history so far is negative, `docs/strategy-ledger.md`, and strategy tuning is
frozen). So the tools that judge a model come right after v1, and model research goes on in every later
version beside the build work.

---

## v1: Sandbox proof (current)

Goal, scope and stages: `docs/spec-v1.md`. What is left:
- **V1** Monday 2026-11-16 Sandbox probes (`docs/monday-sandbox.md`), including F5 "replace a working
  stop", which T1 needs.
- **V2** Release v1 and keep it running on Sandbox days. The 5 clean days of spec section 1 are
  collected while v1.1 and v2 are built; they do not block them.
- **V3** GTC protective stop at entry (decided 2026-11-18): the broker-side stop is placed GTC
  instead of DAY, so it survives the session boundary. First a Sandbox probe of GTC STOP_LOSS
  (accepted, visible, cancel and replace), recorded in `docs/webull-facts.md`; then the change.
  Trading-critical: Fable review. Status: decided, probe not run.
- **V4** Safety controls redesign (decided 2026-11-18, own thread): a Close button per position;
  "Close all" in the positions header (pauses entries, cancels working entries, closes every
  position); the "Cancel orders" and "Emergency stop" buttons are removed; the Safety card keeps
  "Pause entries" and "Stop bot"; stopping asks "Close and stop / Leave open / Cancel"; while the
  bot is stopped with positions open, a warning "bot stopped, N positions open" stays on screen.
  Both apps and the API. Status: decided, in progress.
- **V5** (W9) iOS API address per build destination and the Simulator's own token (`pair-simulator`).
  Status: in review, #673 (API, Fable) and #674 (iOS); then one `pair-simulator` run on the Mac.
- **A1** Chart: a click (Mac) or a lifted finger (iOS) holds the crosshair on its candle; pinch to
  zoom on iOS. Status: Mac done (#663); iOS in review (#664 hold, #667 pinch).
- **A2** (W9, W11) "System" card in both apps: watchdog, supervisor restarts, Webull link freshness, the 03:30
  preflight result, caffeinate, model training progress. Status: proposed, owner not confirmed.

Owner's own setup tasks (outside the repository):
- **O1** Merge-queue timeout 30 to 60 minutes in the GitHub repository settings (decided
  2026-11-18). Status: owner to change.
- **O2** Ask Massive (Polygon) in writing: is training models on their data allowed (personal plan
  now, business plan later), may we keep derived per-minute numbers after the subscription ends,
  and what Benzinga news allows (R31). Needed before R19 and before the closed beta. Status: email
  drafted (2026-11-20), owner to send.
- **O3** Ask Webull now (F2, moved from v6 because the answer decides v5): may a product for other
  people use the Trading API, or only the Connect API (OAuth, by application); plus three key-security
  questions for W7: can a key be limited to trading without withdrawals, can it be bound to a
  server address, and how is a key revoked at once. Status: decided (2026-11-19), owner to ask.
- **O4** Turn off "Use Tailscale DNS" on the Mac: after a Wi-Fi drop the bot is blind for about a
  minute while names do not resolve. Status: decided (2026-11-19), owner to change.
- **O5** Free disk space on the Mac: the old "Valentyn" account holds about 102 GB. Status: decided
  (2026-11-19), owner to clean up.

## v1.1: Tools to find an edge

Goal: the tools and rules to tell a model version with a positive net result after costs, on data it
was not fitted on, from a fitted one. Scope: offline research tools and the model path; nothing in the
order, risk or session path changes, except T12, moved here from v2 because the model's dollar exam
and backtest need it. Models and strategies never send orders (rule 8). Hand-written strategy tuning is
frozen (owner 2026-11-17: models replace it); R6 to R12 are dropped in the 2026-11-25 re-scope;
research runs never gate a merge.

- **R1** Promotion ladder of MODEL versions with written criteria: exam on sealed data (R20), then the
  Sandbox, then the real account at small size, then full size. Answers Q3 ("what counts as Sandbox
  proved the model version"). Written in `docs/promotion.md` (rungs, evidence, who decides, report
  format); numbers the owner has not set are listed there. Status: criteria written.
- **R2** Walk-forward run as the standard evaluation, with a counter of how often each data set and
  hold-out was used. Status: merged into R15 and R16 (2026-11-19).
- **R3** Parameter sweep and sensitivity report: result against each parameter moved by one step; a
  strategy must sit on a plateau, not a peak. Status: merged into R15 and R16 (2026-11-19).
- **R4** Fewer parameters for new strategies. Status: dropped (2026-11-25, model re-scope); hand-written
  strategies are not extended.
- **R6** Pre-registered R21 test (screener rank 21 and lower, `tickr/b4b5/preregistered.txt` in
  project files) on the May-Aug data. Status: dropped (2026-11-25, model re-scope); it was frozen (2026-11-19).
- **R7** Recheck on recorded live days: screener rank and source vs outcome (old Q46), acceleration check
  (old #153-33). Both studied on history 2026-11-15 (`docs/strategy-ledger.md`): rank fades, source predicts
  nothing, acceleration does not help. Status: dropped (2026-11-25, model re-scope); it was frozen
  (2026-11-19).
- **R8** Candidate `price-action-v2` (no-progress exit, old Q43; trigger confirmation, old #153)
  judged by replay and observers. Status: dropped (2026-11-25, model re-scope); it was frozen (2026-11-19).
- **R9** Per-variant evidence and missed-moves "trade of the day". Status: dropped (2026-11-25, model
  re-scope) (PR #772 closed).
- **R10** Spike-entry setup block, built as the spike setup of `price-action-v3`. Status: dropped
  (2026-11-25, model re-scope); it was frozen (2026-11-19).
- **R11** Observers see what they would trade (discovery applies the union of liquidity rules; left
  over from #403). Status: dropped (2026-11-25, model re-scope) (PR #764 closed).
- **R12** Intra-minute breakout entry on the L1 stream (owner, 2026-11-16), a strategy variant.
  Status: dropped (2026-11-25, model re-scope); it was frozen (2026-11-19); tick data returns in R21.
- **R13** Model research order (decided 2026-11-18): the remaining 6 questions of the wide model, then
  the entry model, then the exit model. Status: superseded by R19 and R20.
- **R14** The apps follow mockup v126 (https://claude.ai/artifact/FzDPRpGgQPmKWi3AJyzztN): "model
  version" everywhere, confidence against the threshold with the top reasons, "Closest to entry", the
  Model lab, candidate versions and the "New model version" flow. Training runs as a job (W13).
  Status: decided, waits for the model decision API from R24 to R26.

Model path, training stages (approved 2026-11-19; source: the training spec,
https://claude.ai/artifact/RLasCBhBcU9JiwF6QZ8BkR). Acceptance for every stage: income goes up AND
losses go down, on both validation periods.
- **R15** Stage 0: baseline, and a journal of every variant tried (includes R2's use counter).
  Status: decided.
- **R16** Stage 1: day-level comparison with its spread, walk-forward and the sensitivity check of R2
  and R3. Status: decided.
- **R17** Stage 2: the second (entry) layer trained on money, not on hit rate. Status: decided.
- **R18** Stage 3: market context as input to the second layer. Status: decided.
- **R19** Stage 4: ONE final first-layer training. The plan lives only in the first section of the
  training spec, "Итоговый план первого слоя (20 ноября 2026)" (link above); this line only points
  at it. In short: the whole history 2021-11-16 to 2026-11-19 (all rows, top-500 lists), trade and
  quote features inside the same models, 22 targets (barrier targets as probabilities, max rise,
  market-relative), settings searched on three early folds then frozen, uniqueness weights, a
  monthly walk-forward step. Before it: a full minute-data server run as rehearsal and as the
  "without ticks" control. Runs on a rented server. Status: decided in principle (owner,
  2026-11-20); start waits for the Mac checks, the measured server cost and the owner's yes on the
  sum.
- **R20** Stage 5: rebuild the second layer, then ONE exam on the sealed data (2026-07-01 to
  2026-08-14 and every day from 2026-11-20), then a production refit on the whole span as the
  average of 3-5 seeds. The first layer is judged by measures that need no trading settings;
  dollars only for the whole chain, under several setting sets, corrected for the number of trials.
  Status: decided.
- **R21** Stage 6: tick data. The 5-year tick download on Massive Advanced is running (59 numbers per
  stock-minute; ticks of sealed days go to a closed prefix) and feeds R19. Massive's terms: raw
  files are deleted 30 days after the subscription ends. Status: in progress.
- **R22** Stage 7: neural networks, only if ticks pay off in R19 against its "without ticks"
  control. Status: decided.
- **R32** Free float history from SEC filings as a feature. Status: in progress.
- **R33** Live feature parity, a hard condition before the model is switched on live (before the R23
  gate): every first-layer input is fed live from the same source as training (Massive) and computed
  by the shared feature code. Covers: top-500 lists built from the live Massive full-market stream
  (not Webull screener lists); SPY and QQQ; 20-day history loaded before the open from Massive
  grouped daily bars (today the bot keeps only `[market_data] history_sessions` = 5 for its own
  candidates, from Webull); live trades and quotes for the 59 tick numbers; Benzinga news and
  earnings if those groups stay; SEC shares and float. Webull stays for orders and positions only.
  Ongoing cost: Massive Advanced 199 USD a month, plus 99 USD a month per Benzinga add-on kept; a
  paid group stays in the final model only if its measured gain clearly exceeds its monthly price.
  Status: decided (owner, 2026-11-20).
- **R34** (part of R33) News feed lag monitor (owner, 2026-11-20): when the bot reads the Benzinga
  news feed from Massive (REST polling), it records for every article the first-seen time minus the
  published time. The System card (A2) shows the recent median and worst lag; an alert goes to
  `alerts.jsonl` when the lag exceeds the news lag assumed in training. That lag is one config key
  with a comment, read by both the feature code and the monitor (one source of truth); training uses
  a conservative value until the monitor has measured the real one. The same first-seen log for the
  earnings calendar builds a point-in-time history for later top-ups. Status: in progress; the monitor is
  built on the live-features revision store (#833, #835, in review).

Model path, plan v1.5 phases (approved 2026-11-19; source: `model-migration/plan-v1.5.md` in
project files):
- **R23** Step 0 gate (needs R33 first), applied before real money (v4), not before the Sandbox start:
  plus after costs on the sealed period, plus in 2 of 3 months, plus without the best 1% of trades,
  every feature available live, stop and decision frequency chosen. The Sandbox starts with a trained
  layer 2 and no threshold (owner, 2026-11-24). Status: decided, not passed.
- **R24** Live data for the model's features. Status: in progress; the model bundle loader and the
  Massive inputs are merged (#747, #752), the wiring PR is next.
- **R25** Shadow: the model decides live without orders. Status: dropped (2026-11-25, model re-scope)
  (owner: no shadow run, 2026-11-24); the first model run is ACTIVE in the Sandbox.
- **R26** Sandbox, regular session. Status: decided, after R24 (no shadow run first).
- **R27** Pre- and post-market. Status: decided, after R26 and X4.
- **R28** Cleanup of the old hand-written strategies, right after R26 once the models trade the
  regular session; it does not wait for R27 (owner, 2026-11-19). Status: decided, after R26.
- **R29** Calibration check "70 means 70" (owner, 2026-11-20): for every probability the first and
  second layers output, a reliability table by tenths on months the model never saw (the layer-1
  training report already prints it as its CAL line). Rerun after every retraining. A hard
  precondition for R30. Status: report merged (#817); the owner picks the pass rule.
- **R30** Size by model confidence (owner, 2026-11-20): a bot setting "size by confidence: off / on,
  from X to Y USD" that scales a position's size with the model's probability. Needs no retraining.
  Both bounds sit inside the platform's position cap (one source of truth for the cap); the setting
  never loosens platform risk, and loosening it needs Face ID like any protection setting (S5).
  Before it can be switched on: R29 passes, and a backtest on stored model answers shows earned,
  lost and net with and without it, on both money lines. Earlier evidence: the legs model
  (2026-11-17) did better sized by filter rank than flat in all three periods; liquidity of larger
  bets in thin stocks was not checked. A config setting now, in the settings schema (S1) later.
  Status: in review, #821 (needs Fable), off by default; the owner picks min and max.

Exit: R1, R5, R15, R16, R35 and T12 merged (R5 #762, R35 #761 and T12's shared admission core #767
are merged; R1, R15 and R16 are open). A model version climbs R1's ladder as its exam and Sandbox
days come in, while later versions are built.

## Design rules for ticker.bot (owner-approved 2026-11-19, apply from now on)

ticker.bot is built after v4, but the bot and the apps are built now. These rules keep that work
from being redone later. Each lists the items it applies to; a PR on those items says how it meets
the rule.

- **W9** Apps talk only to the API, over a server address plus a bearer token, never to bot files
  and never assuming Tailscale or a LAN (Tailscale is only today's transport). The token check sits
  in one place, so an account token can replace pairing tokens later. Applies to: V5, A1, A2, V4,
  S4, S6, every app screen.
- **W10** One bot instance = one config folder + one data folder. Nothing global outside them, so
  a second instance with another config runs on the same machine. Owner settings change only through
  the settings schema and API, never a hand edit. Applies to: S1-S5, T2-T7, R14.
- **W11** The trading core stays headless and portable. Mac-only services (launchd, iMessage through
  Messages.app, caffeinate, the watchdog) sit behind small interfaces outside the core, and alerts go
  through a channel interface, so a server can run the same bot with other services. Applies to:
  A2, T14, V3, P1, the alert code.
- **W12** Broker credentials are read through one provider (environment variable names today),
  never assumed to sit on the machine that runs the apps. A Keychain or a server secret store then
  replaces that provider in one place. Hard rule 4 holds everywhere. Applies to: P2, W7, M1.
- **W13** Model training is a job: settings and data in, a versioned model out, progress readable
  through the API. The same job can then run on a server for each user's settings. Applies to:
  R13, R14, W5.
- **W14** Every API change stays versioned and tested (`docs/api.md`), with the account or bot
  identity as a later field and no hidden single-user assumption in the wire format. Applies to:
  every API PR.

## v2: Unattended Sandbox

Goal: the bot runs a Sandbox day alone, protects open trades better than a fixed stop, and refuses
every entry it cannot justify. Trading-critical: every item needs an
independent review.

One source of truth (old group A):
- **T1** **Trailing stop / stop tightening** by replace, once V1 proves Webull's replace mechanics;
  the platform rule "a stop only moves toward the price". Starting values (old #153-34, Q10,
  provisional): best price since entry minus 1.5 ATR, at least the fill price after +1.0 R. Planned
  API: `docs/api-planned.md` section 2. The PR that brings stop replacement into the loop must handle
  an operator clearing `replace-unresolved` while the broker already holds the new price (Fable
  review of #742, 2026-11-25).
- **T4** History sessions declared by the strategy. Status: dropped (2026-11-25, model re-scope) (PR #774
  closed); the model loads 20 days of history from Massive by its manifest.
- **T5** A variant stores only its differences from its strategy. Status: dropped (2026-11-25, model
  re-scope) (PR #765 closed).
- **T6** Smaller repeats: `output_decimal_places` in `config/research.toml` and `config/replay.toml`.
  Status: merged (#802); `atr_period` dropped (2026-11-25, model re-scope): only the strategy-decision research tool
  reads it.
- **T7** `tests/test_config.py` refuses a key that means the same thing at two levels. The Sandbox and
  Production tables repeat equal values on purpose (spec section 4, owner 2026-11-15); that stays.
  Status: merged (#802).

Platform checks (old group B):
- **T8** Tradability and halts: instrument profile `status` and trade-instrument
  `trade_policy`/`forbid_reason` (CLOSE_ONLY, DENIED, HALTED) refuse entries with a typed cause and
  never block exits; probe first how a halt and the LULD band show in snapshot, profile and stream
  (old #158). A halt with a position open follows Q2. Status: in review, #800 (needs Fable), probe and check off; the owner runs the probe. Before Production it also gates price-action-v3's spike setup, which
  enters right after a sharp move, where LULD pauses are most likely (Fable review of #476,
  2026-11-16).
- **T10** Price collar: refuse an entry whose price is implausibly far from the last completed bar
  and the previous quote. Status: in review, #807 (needs Fable), off; the owner picks the width.
- **T11** Loss locks: weekly loss ceiling and a stop-out streak lock, beyond v1's per-symbol pause.
  Status: in review, #812 (needs Fable), off; the owner picks the values.
- T12 moved to v1.1 (2026-11-19).
- **T13** Profit target and time exit become the exit rules stored per model version (in its
  manifest), unless an exit model replaces them.
- **T14** (W11) Notification settings in the apps (`docs/api-planned.md` section 4). Status: in review, #768 (routing, needs Fable) and #770 (screens).

The owner's own bot in the cloud (owner 2026-11-19, pulled forward from W6; plan:
https://claude.ai/artifact/QhzoePqMNyeqFeYwUga59C):
- **T15** (W9-W13) The owner's bot runs on one AWS server (Sandbox only) instead of the Mac; the apps
  reach it over HTTPS with a per-device token, no Tailscale. Parts, each its own small PR: a Telegram
  alert channel that works off the Mac (unblocks `alert-channel-missing`; app push through APNs
  comes later as a second channel), several device tokens in the API,
  the Mac app paired by address and token like the iPhone, `render-systemd` and a server deploy that
  refuses unless flat, an off-server heartbeat alarm, and the infrastructure as code. Cut over on a
  weekend while flat, not before V2; the Mac stays the fallback for two weeks, then Tailscale goes.
  Trading-critical and auth parts: Fable review. Status: decided (order, Telegram alerts); domain and AWS
  account wait for the owner. In review (all need Fable): several device tokens #794, systemd and
  flat-only deploy #803, Telegram #809, APNs push #828, Live Activity #830. iMessage and Mac notification
  removal (#823, #824) lands only with the server move release (owner, 2026-11-25: no iMessage on the server).

Trading outside the regular session (owner 2026-11-16, ids kept):
- **X3** Limit-exit chase before a market exit. Old-tracker answer, reconfirm when it starts: SELL
  LIMIT one tick through the bid, re-price every 10 s at most 6 times, never worse than 0.5 ATR from
  the first reference, then MARKET in the regular session (old Q22, Q110). Needed by X4: outside the regular session
  only limit orders work, so the exit has to chase. Status: in review, #813 (needs Fable), off; the owner
  reconfirms the 4 numbers.
- **X4** Extended hours and half days (owner wants them, 2026-11-16; own thread "Enable premarket and
  after-hours trading"). No overnight holding (owner, 2026-11-16): every position is flat before the
  extended session ends. Needs the PRE/POST stop probe and the owner's decision on protection
  outside the regular session. Old-tracker answer, reconfirm: limit orders only in PRE/POST, smaller
  size, no entries on stream or broker loss (old Q118, #020-12).

Exit: T1-T3, T6-T11, T13-T19, X3 and X4 merged (T2, T3, T9, T16-T19 are merged) with their probes answered (V1 for T1, the halt probe for T8, the
PRE/POST stop probe for X4), the
duplicate-key test green, v2 released and running unattended in the Sandbox.

## v3: Settings from the apps

Goal: the owner configures and runs the bot from the Mac and iPhone apps like a user; no owner
setting needs a config file. Order: design first, then macOS, then iOS.

- **S2** Three classes: owner settings (about 20 platform keys plus model settings) in the
  apps; engineering settings (about 210 keys) file only; closed settings (environment, Production
  authorization, secrets) never changeable from an app.
- **S3** The settings version is a fingerprint of the platform settings plus the model version (the
  bundle manifest hash), computed by the bot; the owner only names it. Every decision and trade
  references the settings version. Status: PR #769 is being reworked.
- **S4** (W9, W10) Settings API: schema, current values, validate a draft, apply, history, rollback; every
  change journaled with time and device. Status: read and validate done in #773; apply, history and
  rollback in review, #804 (needs Fable).
- **S5** Apply rules: tightening protection applies at once; model settings (threshold, size, sessions) and
  the active model version apply when flat, by a safe restart through normal startup and reconciliation;
  loosening protection applies from the next session and only after Face ID (owner 2026-11-15, 2026-11-19);
  a setting that requires retraining applies only with a newly trained model; an open trade finishes under
  the version it was opened with. No generic hot reload.
  Status: live tightening in review, #815 (needs Fable).
- **S6** App screens: Protection, Model (mockup v126), Rules in force (every limit with its level and value),
  Change history; a "platform rule" label and value on every refusal; a count of entries the platform
  stopped, by cause.
- **S7** Try before apply on strategy parameters. Status: dropped (2026-11-25, model re-scope); replaced by
  the Model lab what-if on stored model answers (R30).

Several positions and risk (owner 2026-11-16, ids kept):
- **X1** Several simultaneous positions, top-up. The cheapest of the three limits; needs the owner's
  risk-sharing design first, and a risk budget per strategy (share of the daily ceiling, position
  limit) before two strategies are active at once.
- **X2** Risk as percent of equity. Status: in review, #819 (needs Fable), off; the owner picks the percents.
- **X5** Short selling, only if the short-side research finds an edge; until the owner lifts it the
  2026-10-23 ban stays. A large project; needs borrow and SSR facts. Old-tracker idea: Webull is the
  authority (no own SSR rule); record `shortable` as read and map short-sell rejections to typed
  causes (old Q107, #158).

Exit: S1-S6, X1 and X2 merged and shown in both apps; nothing the owner sets needs an edit of a file.

## v4: Real money at small size

Goal: the bot is ready to trade the real Webull account at small size. Enabling Production stays an
owner decision plus the authorization file (rule 5), never a code change.

- **M1** Production readiness view: authorization record form, readiness checklist
  (`docs/api-planned.md` section 3), enabling day. Status: in review, #776.
- **M2** Account page completeness: done 2026-11-16 (balances, account kind, per-position P&L,
  today's fills with fees); left: fee evidence on real fills against the backtest assumptions (report in review, #808).
- **M3** gRPC trade events (faster fill and order updates than polling), if the probe shows they are
  reliable. Status: probe in review, #811; the owner runs it.
- **M4** Slippage and fill quality on real fills against the Sandbox and backtest assumptions (R5
  applied to Production). Status: report in review, #808.
- **M5** Webull support questions: is there a documented Sandbox calendar endpoint; which real-time
  entitlement applies to Sandbox L1 (old #080). Status: questions doc merged (#814); the owner sends them.

Exit: M1-M5 merged. Which model version goes live, and when, is the owner's decision on the enabling
day (R1's rungs inform it); Production days then run beside v5 work.

## v5: The product (ticker.bot)

Goal: the product people use: the ticker.bot site and accounts, one bot per user in the cloud, and
the macOS and iOS apps signed in to it. Still for the owner's own use; no legal or business step.
The cloud bot (W6) replaces the standalone Mac product (2026-11-19): P1 and P7 go, P2-P6 stay in a
form that fits the cloud bot, confirmed in W1.

### ticker.bot (owner 2026-11-19)

The product's own site and cloud, after v1 to v4. Rough notes and the open questions:
`docs/ticker-bot-draft.md`. The spec is written later with the owner; nothing is built before it.

- **W1** Spec for ticker.bot, written with the owner from the draft. Includes pricing and credits
  (F6, moved here 2026-11-19) and the final scope of P2-P6. Status: not started.
- **W2** Public "coming soon" landing page on ticker.bot: clear product description, iOS / macOS /
  Android "coming soon", made to look good, with an email field for the waitlist (covers F3).
  Status: decided, waits for W1.
- **W3** Hidden full flow that only the owner can use on the public site: sign up, log in (email;
  maybe Google, Apple), what a user sees after sign-up, free tier or trial, paid subscription.
  Status: decided, details in W1.
- **W4** One account for the site, the Mac app, the iOS app and the user's bot. Status: decided.
- **W5** Site features: settings, model training, maybe statistics; the dashboard stays in the apps.
  Status: decided, scope in W1.
- **W6** One bot per user in the cloud on a rented server (AWS or similar), with a limited set of
  settings and a model trained on them; no Tailscale, the apps connect to the server. Status:
  decided, design in W1. The owner's own Sandbox bot moves earlier, in v2 (T15).
- **W7** Broker token security for live accounts, designed before any server work: who holds the
  credentials, how they are stored, used and revoked, what happens on a leak. Status: decided as a
  must, design in W1.
- **W8** Deployment: server, domain, TLS, how the owner (with Claude) deploys and updates it.
  Status: not started.
- **W15** Business market-data licence before the closed beta (Massive Business, about 2,499 USD a
  month, startup discount through sales). Status: decided (2026-11-19), before the beta.
- **W16** An early, simple build submitted to Apple to test App Store rule 3.2.1(viii) before the
  apps are finished. Status: decided (2026-11-19).
- **W17** A clear cancel and refund flow (the top complaint about competitors). Status: decided
  (2026-11-19), details in W1.

### Standalone Mac items (replaced by the cloud bot, 2026-11-19)

The bot runs in the cloud (W6), not on the user's Mac. P1 and P7 are dropped; P2-P6 stay in the form
given below, confirmed in W1.

- **P1** One installable Mac app with the bot, local API and scheduled jobs. Status: dropped
  (2026-11-19), the cloud bot replaces it.
- **P2** (W12) First-run setup after sign-up: "Connect Webull" (W7 decides how the credentials are
  held, never a file or a log), account check, alert channel (app push with the Apple Developer
  Program and APNs, Q6), preflight as a checklist.
- **P3** Running the user's cloud bot from the apps: start, stop, status and the reason the bot is
  not trading; settings only through v3 screens and the site.
- **P4** Updates and data: a bot update never runs with a position open, database migration with
  backup, export of logs and a diagnostics bundle without secrets.
- **P5** Strategies without code for the user (templates and the advisor). Status: dropped (2026-11-25,
  model re-scope); replaced by the "New model version" flow of mockup v126.
- **P6** User documentation in the apps and on the site (English and Spanish): setup, every setting, every refusal
  cause, what to do on each alert.
- **P7** Clean-machine test of the installable Mac app. Status: dropped (2026-11-19) with P1.

Exit: W1 agreed, W2-W8, W15-W17 and the kept P items built; a new user signs up, connects Webull and
runs a full Sandbox day on the cloud bot.

## v6: Selling it (only if the owner still wants to after v5)

- **F1** Check the "Tickr" trademark, buy the domain, add a license file (the repository is public
  without one).
- **F2** Written answer from Webull on third-party use. Status: moved to O3 (2026-11-19), asked
  now because it decides v5; nothing else here starts before the answer.
- **F3** Landing page and waitlist; continue only with real demand (about 200 emails in 2 to 3
  months; under 50 means stop). Status: covered by W2 and its email field (2026-11-19).
- **F4** Company, securities lawyer (tool, not advice), direct sale of the notarized Mac app by
  subscription (App Store rule 3.2.1(viii) blocks trading apps from individuals), closed beta.
- **F5** Strategy marketplace. Status: dropped (2026-11-25, model re-scope); it was postponed (owner
  2026-11-15).
- **F6** Selling access through ticker.bot (owner idea 2026-11-18, to discuss later): software, not
  advice; the user sets the main settings. Subscription covers Sandbox only (N hours of bot runtime,
  N model trainings); Production and extra Sandbox time paid by credits, charged for time, never a
  share of profit; backtests on the user's own settings sold too. Credits running out stops new
  entries early with warnings and keeps a reserve to finish open trades, never leaving a position
  without a broker stop. Status: moved into W1 (2026-11-19); O3 still gates selling it.

## Owner questions

Open: none.

Answered (recorded in `docs/decisions.md`):
- **Q1** Manual trades in the bot's account are ignored: still fenced and alerted, never touched;
  they block entries only in their own symbol (or every symbol while a foreign order's symbol is
  unknown) and do not count toward the position limit (2026-11-16, with 2026-11-15).
- **Q2** A halt, or a position that cannot close: keep the resting stop, alert, send no order into the
  halted book, close at the first chance once trading resumes, the next morning if forced
  (2026-11-16).
- **Q3** What counts as "Sandbox proved the strategy": R1's promotion ladder, written in
  `docs/promotion.md`.
- **Q4** Captured quotes, bars and decisions are kept forever (archived as gzip, never deleted);
  low disk is caught by preflight's `[runtime] min_free_disk_mb` check (2026-11-16).
- **Q5** A new listing without relative-volume history is refused with a typed cause (2026-11-16).
- **Q6** iMessage is enough before unattended days; app push waits for v5 (2026-11-16). Status: superseded (2026-11-25): the owner bought the Apple Developer Program, and app push is being built (#828).

## Done

- **R31** Benzinga News and Earnings add-ons on Massive, bought 2026-11-20.
- **S1** Settings schema (#763).
- **T2** Planned loss per trade lives only in `config/bot.toml [risk]` (#766).
- **T3** Position notional cap lives only in `config/bot.toml [risk]` (#766).
- **T9** Money in the account as an admission check (done earlier, #641).
- **R35** Backtest calendar correctness (#761).
- **A3** Operator commands are idempotent end to end (Codex review 2026-11-24): one key per
  command until its outcome is known, settled by `GET /commands/receipt`, kept no longer than
  `[api] command_max_age_seconds` (#748).
- **A4** A command is sent only on a fresh, same-server status read; late answers from a former
  server are dropped (#748).
- **A5** Phone pairing stores its token with one confirmed Keychain update (#744); a refused write
  keeps the earlier pairing (#756).
- **R5** Slippage measured from the bot's own records; replay uses the measured value (#762).
- **R36** Training stack safety on AWS: conditional lock delete, fetch failures count toward the restart
  limit, pinned installers (#760). Follow-up: claim race in tickrun fixed (#822).
- **T12** Shared admission rules from one code path (shared admission core only; the model exam calls
  it later) (#767).
- **T16** Discovery stamps candle completeness per request; no symbol starved (#750).
- **T17** Reads scale with today's work, not with history (#749).
- **T18** Daily backup retried the same day after a failure (#745).
  Retries every `[run] backup_retry_seconds` with no cap per day; the cap is an owner number.
- **T19** Operator path for a hard `order-conflict`, `tickr resolve-order` (#751).
- **A6** VoiceOver "no data" before statistics arrive; unknown stop codes in local language (#746).

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
