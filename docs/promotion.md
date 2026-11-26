# Promotion ladder for model versions (backlog R1, answers Q3)

A model version is a trained bundle (layer 1 and layer 2, one version name). It never sends orders
and never loosens platform risk (hard rule 8). This page says how a version climbs from an exam to
full size, what evidence moves it, who decides and what moves it back down.

**The owner decides every move, up or down.** Evidence is reported to him in the format below; no
rung opens by itself, and no number on this page is invented. Where the owner has not set a number
it says "owner decides" and is listed under "Open owner decisions".

## The rungs

| Rung | Where the version runs | Money |
|---|---|---|
| 0. Exam | Sealed data (R20): 2026-07-01 to 2026-08-14 and every day from 2026-11-20 | none, simulated |
| 1. Sandbox | Webull Sandbox, regular session, ACTIVE (orders sent; no shadow run first, owner 2026-11-24) | none, paper |
| 2. Small real | The real account (Production), small size | real, small |
| 3. Full size | The real account, full size | real, full |

Rung 1 may also run for a version that has passed rung 0 only in part; see the first-run rule below.

### Rung 0: exam on sealed data (R20)

- Evidence to enter: the second layer is rebuilt, then ONE exam on the sealed data. The first layer is
  judged by measures that need no trading settings. Dollars are reported only for the whole chain,
  under several setting sets, corrected for the number of trials (R20). The calibration table
  "70 means 70" (R29) is printed for both layers on months the model never saw.
- Sealed data is spent by use: the exam is run once per version, and a rerun is recorded as a rerun.
  A version that is changed after seeing the exam is a new version and needs new sealed days.
- Moves up to rung 1: the owner reads the exam report and says go. The Sandbox start does not need a
  profit threshold (owner, 2026-11-24).
- Moves down: not applicable (a version that fails is not built further, or becomes a new version).

### Rung 1: Sandbox

- First run: it starts with a trained layer 2 and NO profit threshold and NO minimum number of days
  (owner, 2026-11-24). Nothing on this page may be read as adding one.
- Evidence to leave this rung: the Sandbox report (below) for the version, over the days the owner
  chooses to look at, together with fill and slippage facts from the same days and no open
  reconciliation or protection incidents attributed to the version. Whether a minimum number of days
  or a minimum result is needed before small real money: **owner decides**.
- Moves up to rung 2: only after the R23 Step 0 gate has been passed for that version (see the gate
  below), the R33 live feature parity condition holds, and the owner decides. Production also needs
  the owner authorization file (rule 5).
- Moves down: a Sandbox bug or a reconciliation conflict stops entries by platform rule, not by this
  ladder. The owner may stop the version at any time. Stop conditions that return a version to
  rung 0 for retraining: **owner decides**.

### Gate before real money (R23 Step 0)

Applied before rung 2 (v4), not before the Sandbox start. It is the backlog R23 text, not new
criteria: plus after costs on the sealed period, plus in 2 of 3 months, plus without the best 1% of
trades, every feature available live, stop and decision frequency chosen. Status today: decided, not
passed. R33 (live feature parity) must hold first. `python -m tickr.research gate`
(`docs/research.md`) reports the numbers as an offline DESCRIPTIVE report on both money lines: net
after costs, K of M months and without the best X% as named sensitivity checks, tail
concentration and a same-trade spread cost diagnostic (a paired baseline is reported as
unavailable), over a frozen start, end and session calendar.
Nothing is promoted from it automatically: binding promotion criteria are set with the owner on
open development evidence before any exam read, and portfolio dollars count only after a replay of
cash, slots, repeated symbols, protection, session limits and actual exits (not built yet). The
other items stay checked by hand.

### Rung 2: real account at small size

- Production is an owner decision plus the authorization file; the only barrier is `trading_gate()`
  in `src/tickr/config.py` (rule 5). Enabling is configuration and an owner record, never a code
  change.
- Evidence to enter: rung 1 evidence as above, R23 Step 0 passed, M1's readiness checklist complete.
- What "small size" means (position size, daily loss ceiling, number of days): **owner decides**.
- Evidence to leave this rung: the same report on real fills, plus M4 (slippage and fill quality on
  real fills against the Sandbox and backtest assumptions) and the fee evidence left open in M2.
  Required days and result: **owner decides**.
- Moves down: the owner returns the version to rung 1 or switches it off. Whether a rule such as a
  loss limit triggers this automatically: **owner decides** (platform risk limits apply regardless).

### Rung 3: full size

- Moves up from rung 2 only on the owner's decision after reading the real-money report.
- What "full size" is: the platform limits in `config/bot.toml` `[risk]` (the cap and the daily
  ceiling). Raising a limit is a protection change, not a ladder step (R30 and S5 describe how).
- Moves down: the owner returns the version to rung 2 or 1 at any time.

## Reporting format

Every report, for every rung, uses the same three numbers and the same two money lines. Reports on
Sandbox and real days use real recorded fills and fees; the exam report uses the exam's simulated
fills, labelled as such.

For each money line:

| Field | Meaning |
|---|---|
| Earned on winners | Sum of net results (after fees) of trades that closed in plus |
| Lost on losers | Sum of net results (after fees) of trades that closed in minus, shown as a loss |
| Net | Earned minus lost |
| Days in plus | Number of days with a net result above zero, out of the days counted |

The two money lines:

1. **Main session at 1,000 USD**: the regular session, each position at 1,000 USD (the per-position
   cap in force, `[risk] position_notional_cap_usd`).
2. **All sessions at 4,000 USD**: every session (regular, pre-market, post-market), each position at
   4,000 USD. Extended sessions are not trading yet (R27, after R26 and X4); until then this line
   shows the regular session only, labelled so, and never an estimate.

A report also states the period (first and last day), the model version name, the number of trades,
and which days were excluded and why (for example a day the bot was down). Both lines are shown side
by side; neither is dropped when it looks worse.

## Where the checklist shows

M1 (#776) adds the Production readiness view with `GET /readiness`: the authorization record form,
the readiness checklist (`docs/api-planned.md` section 3) and the enabling day. The ladder's checklist
for a version (rung reached, exam report present, R23 Step 0 status, R33 parity, owner authorization
file present) shows there. The view only informs; the barrier stays `trading_gate()`.

## Open owner decisions

- Minimum days in Sandbox, and any minimum result, before small real money (none now, by the
  2026-11-24 decision for the first run only).
- Conditions that send a Sandbox version back to retraining (rung 0).
- What "small size" is on the real account: position size, daily loss ceiling, number of days.
- Days and result needed on rung 2 before full size.
- Whether any loss or incident rule moves a real-money version down automatically, or only the owner
  does.
- How the 4,000 USD all-sessions line is read until extended sessions trade (R27).
