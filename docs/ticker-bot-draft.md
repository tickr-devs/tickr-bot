# ticker.bot: rough notes before the spec

Status: owner's first rough ideas (2026-11-19), recorded so the spec can start from them. This is
NOT a spec and nothing here is decided beyond what is marked "owner". The spec is written later
together with the owner. Backlog items: `W1`-`W8` in `docs/backlog.md`.

## Order (owner)

1. Finish training the model (backlog R13).
2. Integrate the model into the apps (R14).
3. Then ticker.bot.

## What the owner described

- **Public site on ticker.bot (domain bought 2026-11-18).** For now a "coming soon" landing page:
  a clear description of the product, iOS / macOS / Android "coming soon" badges, made to look good.
- **A hidden full flow for the owner only.** The site is public, but the owner alone can walk the
  flow future users will walk: create an account, log in (email; maybe Google, Apple or others),
  and see what a user sees after sign-up: a free tier or trial, then a paid subscription (to be
  designed).
- **What the site does:** settings, model training, maybe statistics. **Not** the dashboard: the
  dashboard stays in the macOS and iOS apps (owner).
- **One account everywhere:** the site, the Mac app, the iOS app and the user's bot sign in with the
  same account.
- **One bot per user, in the cloud** on a rented server (AWS or similar), with that user's settings
  and a model trained on those settings. The settings offered are a limited set at first and grow
  over time.
- **No Tailscale:** the apps connect to the server directly.
- **Webull token security is critical** (owner): live accounts hold real money, and attacks get
  easier with AI. How the user's broker credentials are given, stored and used must be designed
  before anything else on the server side.

Earlier owner ideas on selling access (2026-11-18): backlog F6 (subscription covers Sandbox only,
Production and extras paid by credits charged for time, never a share of profit).

## Questions the spec has to answer

- Owner-only access to the hidden flow on a public site: allowlisted account, invite code, or a
  separate preview host.
- Sign-in providers for v1 of the site, and how the apps sign in (the same identity, tokens kept in
  the Keychain).
- Broker credentials: who holds them (the user's device, a managed secret store, a per-user
  enclave), whether the server can trade without the user present, how access is revoked, and what
  happens on a leak. Hard rule 4 applies on the server too.
- Isolation between users' bots (one process or container per user), and what the server operator
  can see.
- Which settings are user-facing first (backlog S1-S2 is the base), and how a training run is
  started, limited and paid for.
- Legal gates that still apply before strangers use it: F2 (Webull's answer on third-party use).
- How this relates to v5 (standalone Mac product): the cloud bot replaces "the bot on the user's
  Mac", so v5's scope changes once the spec is agreed.
