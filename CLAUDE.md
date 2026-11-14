# Aperture: rules for agents

Aperture is a single-owner intraday trading bot for US common stocks on Webull OpenAPI.
Read `docs/spec-v1.md` before any change. `docs/backlog.md` is NOT in scope unless a task names an item from it.
Broker facts live in `docs/webull-facts.md`. Owner decisions live in `docs/decisions.md`.

## Hard rules (never break, never "temporarily")

1. **Broker is the source of truth.** Positions, orders and fills are what Webull's authenticated reads say. Our records are expectations. On conflict: block new entries, reconcile, report.
2. **No position without a broker-side stop.** An entry path that cannot prove protection for its session stays refused.
3. **One send per decision.** Every place/cancel/replace is recorded durably before the HTTP call, sent exactly once, never auto-retried. Unknown outcome = read the broker, never resend.
4. **Secrets are never read, printed, logged or committed.** Code handles credential variable NAMES and presence only. `.env`, tokens and account ids never enter the repo, logs or the database.
5. **Environment is configuration.** Sandbox and Production differ only in `config/bot.toml`. Source code never branches on the words "sandbox" or "production". The only Production barrier is `trading_gate()` in `src/aperture/config.py`, driven by config plus an owner authorization file.
6. **Every number lives in config with a comment.** Code contains only structural literals (0, 1, -1, indices, unit factors, protocol constants). Every config key has a comment saying what it is, its unit and who reads it. `tests/test_config.py` enforces this.
7. **Refuse, never guess.** Unknown status, missing field, limit breach, unknown trading day: a typed refusal with one recorded cause. No silent defaults or clipping.
8. **Strategies are swappable modules.** A strategy never sends orders and never loosens platform risk. It receives data and returns a decision with its reasons, tagged with its name and version.
9. **Only `src/aperture/broker/webull.py` may import the Webull SDK.**

## How we work

- One stage at a time (see `docs/spec-v1.md`, "Stages"). Work on a branch, open a pull request, keep PRs small (one reviewable idea).
- CI must be green. Tests run in a couple of minutes; if they get slow, fix that first.
- Trading-critical code (orders, stops, risk, reconciliation, storage, startup/recovery) needs an independent review before merge.
- No evidence packets, receipts, trackers or design tables. The PR description says what changed, how it was tested, and what is left.
- Never weaken a test, raise a timeout or add a retry to make CI pass.
- Code that talks to a real broker is verified by the owner on his Mac; cloud agents test against `broker/fake.py` and recorded responses in `tests/fixtures/`.
- Size is a warning sign: if v1 grows past ~15k lines of product code, stop and ask why.
- Write code, comments, commits and docs in English. The owner reads Russian in chat.
