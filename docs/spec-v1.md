# Aperture spec, first release (v1)

Status: accepted by the owner 2026-11-14. Everything not described here is in `docs/backlog.md` and is out of scope for v1.
Broker facts: `docs/webull-facts.md`. Owner decisions: `docs/decisions.md`.

## 1. Goal

Prove in the Webull Sandbox that the bot can trade safely and record everything needed to judge the strategy.

v1 is done when the bot, started by launchd on a trading day, finds setups with the active strategy, enters long positions in the regular session, protects each one with a broker-side stop, exits, ends every day flat, and does this for 5 trading days in a row with broker records and our records agreeing.

"Sandbox shows the strategy is profitable" and "Production may be enabled" are separate later decisions of the owner. Sandbox fills prove nothing about real fill quality.

## 2. Scope

In v1:
- One Webull account, Sandbox environment. Production code path exists but is closed by the gate (section 8).
- US common stocks only, regular session (09:30-16:00 ET) only, long only, at most one open position.
- One active strategy (`price-action-v1`), loaded as a swappable module.
- Broker-side STOP_LOSS protection; exit by the resting stop, by strategy EXIT, or by session flatten.
- Recording of every decision and all market inputs for later analysis.
- A small local API with state and operator commands, and a minimal status screen on top of it.

Not in v1 (see backlog): trailing stop and stop tightening, extended hours, shorts, several positions, observers running live, replay of other strategy versions, reports, AI advisor, the macOS/iOS app, Production enablement.

## 3. Architecture

One Mac. One bot process (single writer), one SQLite database, one folder of daily market-data files.

```
Webull OpenAPI (HTTPS data + trading, MQTT L1 quotes)
        |
  broker/webull.py   <- only module importing the SDK
        |
  marketdata -> strategy (module) -> safety -> orders/protection/reconcile
        |                                           |
  data/capture/<day>/*.jsonl              store (SQLite, journal)
                                                    |
                                         api (127.0.0.1, read + commands)
                                                    |
                                         status screen (minimal)
```

Rules:
- Decisions run on one sequential loop: read event (quote, bar, broker observation, timer) -> decide -> write journal -> perform the side effect. Commit before effect.
- Plain SQLite: WAL, `synchronous=FULL`, `fullfsync=ON` on macOS. No custom storage engine, no native code.
- Bulk market data never enters the trading database.
- One OS file lock guarantees a single bot instance.

Modules (`src/aperture/`): `config`, `store`, `broker/` (interface, `fake.py`, `webull.py`), `marketdata`, `strategy/` (interface + strategies), `safety`, `orders`, `runtime`, `api`, `cli`.

## 4. Configuration

- `config/bot.toml`: platform, risk, session and per-environment settings. `strategies/<name>/strategy.toml`: strategy values.
- Every key has a comment (what, unit, who reads it). Unknown or missing key = refuse to start. Values are frozen per run and written to the run record.
- Both environments define the same keys (tested). Nothing in Production inherits a Sandbox value.
- Credentials are environment variables named in config; only presence is checked.

## 5. Strategy platform

- The platform owns data, risk, orders, protection, timers and recording. A strategy owns only its analysis: it receives completed M1 bars and L1 quotes for a symbol and returns a decision: `IGNORE`, `WATCH(trigger, invalidation)`, `ENTER(price window, stop)`, `HOLD`, `EXIT`, each with the list of checks (rule, measured value, threshold, pass/fail).
- The strategy computes the stop and the quantity from configured risk; the platform only checks limits and refuses on breach. A strategy can only be stricter than the platform.
- Strategy identity = name + `version` from its file; recorded with every decision and trade.
- The interface must allow several strategy instances on the same data, each decision tagged with role `active` or `observer`. Only `active` reaches the safety layer. v1 runs only the active one.
- `price-action-v1` values: see `strategies/price-action-v1/strategy.toml` (breakout and impulse-continuation setups on M1 bars, VWAP/EMA9/EMA20 filters, ATR 14, relative volume at least 2.0, price window up to 1.0 ATR above trigger, cost rule, minimum stop distance).
- No look-ahead: a decision at time t must not change when bars after t are altered (tested).

## 6. Market data

- Discovery once per configured interval in the regular session: gainers, losers, top actives (top 200 each); merge by `instrument_id`; universe and liquidity checks; M1 bars for candidates; L1 subscription for WATCH symbols and the open position.
- All work stays inside the per-route request budget (Sandbox: 30 per 60 s per route). Hitting the budget produces a visible state ("evaluated 140 of 200: bars budget"), never a silent cut.
- Quotes keep provider time and receipt time. Decisions use bid/ask only, never mid or last, and only quotes not older than `max_quote_age_seconds` end to end. Delayed data means no entries.
- Stream health per symbol: fresh / quiet (transport up, no quotes) / lost (transport down). Quiet is not loss. Transport loss with a position open = market exit (regular session).
- Recording from day one: every screener row, every M1 bar used, every L1 quote of subscribed symbols, with provider and receipt time, into `data/capture/<env>/<NY-date>/*.jsonl`. This is the input for later replay.

## 7. Risk and the safety layer

Numbers in `config/bot.toml` `[risk]`: planned loss 50 USD per trade; notional cap 2,500 USD per position; daily loss ceiling 300 USD gross; one open position; quote age 5 s.

Admission (all must hold; the first failure is the single recorded cause):
1. Trading gate open for the active environment.
2. Startup reconciliation done; latest broker read fresh; no unresolved or foreign exposure; entry-stop flag clear.
3. Regular session, calendar known for today, not a half day, before last-entry time.
4. No own position or working order; open positions below the limit.
5. Executable quote fresh; setup still valid; price inside the window.
6. Quantity at least 1; notional within cap; planned loss within limit; daily rule passes (realized loss + open reservations + new planned loss <= ceiling).
7. Request budget available for the entry AND the stop.

Operator controls (local API + CLI): `entry-stop set|clear` (blocks entries only, survives restart), `cancel-own-orders` (own entry orders only, never stops), `flatten` (closes own position through the normal exit path).

## 8. Environments and the trading gate

`trading_gate(config)` is the single function deciding whether this process may send trading requests. It requires the active environment's `trading_enabled = true` and, where `requires_owner_authorization = true`, a valid owner authorization file. In v1 Production has `trading_enabled = false`. The only Production call allowed is the read-only trading calendar.

## 9. Orders, protection, exits

Order sequence:
1. Write the intent (symbol, side, type, quantity, prices, TIF, session, `client_order_id`, reservation).
2. Take request budget for the route.
3. Write the attempt.
4. Send exactly once (SDK retries off for trading calls).
5. Write the outcome: acknowledged / typed refusal / unknown.
6. Poll Detail until the order is seen; only Detail establishes state.

- `client_order_id` at most 32 characters, deterministic from (run, setup, leg, sequence), with a prefix separating bot and probe orders.
- Unknown outcome: keep the reservation, block entries, resolve by Detail + open orders + positions; never resend.
- Status map: SUBMITTED/PENDING -> accepted; PARTIAL_FILLED -> partial; FILLED -> filled; CANCELLED -> cancelled (fill may be > 0); FAILED -> rejected; EXPIRED -> like cancelled; anything else -> UNKNOWN: refuse, keep polling, block entries.
- Entry: LIMIT BUY, DAY, session CORE, at the top of the price window. Cancelled when the quote leaves the window or goes stale; never re-priced. Any partial fill becomes a protected position.
- Protection: if Webull accepts an attached entry+stop order (OTO; to be proven by probe), use it. Otherwise after each fill observation place one SELL STOP_LOSS, DAY, for exactly the broker-confirmed filled quantity. Protection counts only when Detail shows the stop working. Measure separately: fill -> fill observed, and fill observed -> stop verified. A rejected stop: retry per config, then exit and block entries, alert.
- Exit teardown: cancel stop -> confirm CANCELLED -> re-read held quantity -> send the closing order for exactly that quantity. Unknown cancel outcome: keep the stop, send nothing, alert. Stop filled during cancel: position closed, send nothing.
- Session flatten: working entries cancelled at last-entry time; position closed by the flatten time (market in regular session).
- A trade is closed only when the broker is flat, the stop is CANCELLED or FILLED, and no own open order remains.

Reconciliation: positions and open orders every `reconcile_interval_seconds`; startup always reconciles first. Foreign exposure blocks entries and is never touched. Quantity mismatch: broker wins, entries blocked until explained, the stop is made to cover the broker quantity.

## 10. Persistence

- One database per environment. Table `journal` (append-only: seq, time, run, kind, key, body JSON) plus state tables updated in the same transaction (runs, orders, attempts, positions, protection, reservations, risk_day, fences, decisions).
- Startup: lock -> load config -> open DB (migrate after backup) -> gate -> calendar -> broker account read -> full reconciliation -> resume a protected position if any -> start data.
- Migrations: numbered, forward only, tested. Before the first real order the DB may simply be recreated; after it, data is never discarded.
- One verified backup per trading day (`VACUUM INTO` + `integrity_check`), keep the latest.

## 11. Runtime and alerts

- launchd: `preflight` (calendar known, credentials present, token usable, disk, config valid) before the session; `bot` started before 09:30 ET, no automatic relaunch after a crash; `api` always on, loopback only.
- SIGTERM: stop entries, cancel working entries, leave stops resting, close cleanly. Restart over an open position is refused unless the resting stop is verified first.
- Alerts (macOS notification now; phone push in the app stage): position without verified protection, stop rejected, unknown order status, foreign exposure, broker reads failing, stream lost with a position, daily ceiling reached, bot crashed or did not start, preflight failed, token needs confirmation in the Webull app.

## 12. Local API and status screen

`127.0.0.1` only, Host/Origin allowlist. Read endpoints: `/status` (bot state, environment, strategy version, fences and the single entry-blocking reason, position with stop state, today's trades and result gross, data freshness). Command endpoints: entry-stop, cancel-own-orders, flatten; each recorded in the journal. The API contract is versioned and tested so the macOS app can be built against it later. The v1 status screen is one simple page over `/status`.

## 13. Testing

1. Recorded-shape tests: every reader is tested against real Webull responses in `tests/fixtures/webull/` (anonymised).
2. Fake broker at the adapter boundary scripted with: acknowledgements, timeouts with and without order creation, partial fills, cancel-vs-fill races, rejected stops, 429s, unknown statuses, foreign positions, stream loss. Crash-and-restart at every step between "intent written" and "terminal observed".
3. Pure-function tests for indicators, setups, sizing, daily risk, tick rounding, status map, session clock (DST, half days), no-look-ahead.
4. Invariant tests: no request to a host outside the allowlist; no secret value in logs or DB (planted canary); gate closed without authorization; no code path sends an entry without proven protection.
5. Sandbox probes with the owner present (stage 0 and stage 3).

## 14. Stages

Each stage is one or more PRs; the next starts after the previous merges.

| # | Stage | Done when |
|---|---|---|
| 0 | Probe broker facts (owner present, market hours) | Answered: are Sandbox quotes real time; does a 1-share LIMIT BUY fill; is OTO entry+stop accepted; is STOP_LOSS accepted and visible; cancel stop + MARKET close; non-empty positions/open-orders shapes; instrument profile shape (security type of a stock, ETF, warrant, ADR) and a raw M1 bars body. Responses saved as fixtures. |
| 1 | Skeleton | config loader + comment rule, SQLite journal, single-instance lock, fake broker, CLI `status`, CI. |
| 2 | Webull adapter | read account/positions/orders/Detail, place/cancel, host allowlist, request budget; recorded-shape tests green. |
| 3 | Protected trade on command | `enter 1 SYMBOL` does intent -> order -> fill -> stop -> exit -> reconcile in Sandbox; kill -9 mid-trade and restart resumes correctly. |
| 4 | Risk and session | admission checklist, daily risk, entry-stop, cancel-own-orders, flatten, 15:45/15:55 timers. |
| 5 | Data and strategy | discovery, bars, L1 stream, capture files, strategy interface + price-action-v1, decision recording; historical M1 replay of the strategy as an evaluation tool. |
| 6 | Automatic trading | strategy ENTER wired to orders, launchd jobs, alerts, local API + status screen; 5 clean trading days. |
| 7 | Later (backlog) | trailing stop, macOS app, signal-outcome reports, replay of other versions, observers, AI advisor, more positions, extended hours, Production. |
