# Planned API for backlog screens

The design canvas draws four screens that the bot cannot serve yet (boards marked BACKLOG in the
row "Бэклог": macOS and iOS each). This file says what each one needs from the local API, so the
work that builds it starts from the screen. Nothing here exists yet; `docs/api.md` stays the
contract of what does. When an item is built, its section moves to `docs/api.md` with its schema,
and this file loses it.

Wire rules are those of `docs/api.md` (decimal strings, UTC times, every key present, `api_version`).
Nothing here sends anything to the broker from the API process, and nothing here lets a client
change the trading configuration: Sandbox/Production, limits and strategies stay in
`config/bot.toml` (rules 5 and 6).

## 1. AI advisor proposal

Screens: Strategy lab › Advisor proposal (Mac), Lab › proposal (iOS). The list of runs, a
pipeline (proposed, judged on the hold-out, your decision, observer by config edit, active by
config edit), the parameter diff with the advisor's reason per change, the hold-out check (mean R
of base and candidate with their intervals around 0, total R, win rate, profit factor), the fitting
period struck through as "no evidence", the new-strategy idea as text, and Report / Reject /
Accept.

Contract: owned by the advisor-in-the-apps work (`GET /advisor` → `advisor-runs-v1`,
`POST /advisor/decision`), not defined here. The design relies on these properties of it:

- **Accept and Reject record the owner's decision only.** They never copy, start or activate a
  strategy. The screen says so; running a version as an observer or as the active strategy stays an
  edit of `config/bot.toml` on the Mac (rule 8).
- Every number is simulated and is labelled so on screen.
- Per change: section, key, current value, proposed value, reason. Direction (up/down) is derived.
- Per judge: candidate and base verdict codes (`edge`, `negative_edge`, `no_evidence`, `too_few`),
  hold-out stats including `mean_r_interval`, and the fitting stats shown as no evidence.
- On iOS, Accept asks for Face ID first (client-side only, as for other commands).

## 2. Trailing stop

Screens: the position card and its risk band (Mac Overview, iOS Positions › position). A dashed
stepped stop line on the price chart that only moves up, a dot at each replace, the band from
−1 R to +2 R with the price, the best price and the protected part, the trailing state, and the
list of stop replaces with their outcome.

Prerequisite (`docs/backlog.md`): a probe proving Webull's replace mechanics for a stop order.
Until then nothing below is built.

### Config `[trailing]` (proposed; values from the old tracker, provisional)

Shipped instead as `[trailing_stop]` in `config/bot.toml` (off, refused on until the loop
integration), computed by `tickr.risk.trailing`: `enabled`, `atr_multiple`, `breakeven_after_r`
(the backlog's "at least the fill price after +1.0 R"; the stop trails from the start, there is
no separate activation) and `min_move_usd`. The table below is the earlier proposal; the
`status` object below still has to be aligned with it when the loop PR adds it.

| Key | Unit | Meaning |
|---|---|---|
| `enabled` | bool | Trail the stop of the bot's own positions. |
| `activate_after_r` | R | The best price since entry must reach entry + this many R before the first tightening (1.0). |
| `atr_multiple` | ATR | The new stop is the best price since entry minus this many ATR (1.5). |
| `floor_at_entry` | bool | Once active, the stop is never below the entry fill price. |
| `min_step_usd` | USD per share | A replace is sent only when the new stop is at least this much above the current one. |

### `status.positions[].trailing` (new object, null when `[trailing] enabled` is false)

| Key | Type | Meaning |
|---|---|---|
| `state` | `waiting` \| `active` \| `held` | `waiting`: best price below the activation price. `active`: the stop follows. `held`: a replace is unresolved or refused; the stop stays where the broker shows it until a broker read settles it. |
| `held_cause` | string \| null | Why `held`, with `held_cause_codes` like other causes. |
| `activation_price` | decimal | Entry plus `activate_after_r` R. |
| `best_price` | decimal | Highest price since entry the bot used. |
| `best_at` | time | When it was seen. |
| `distance` | decimal | `atr_multiple` × the ATR the bot used, in USD per share. |
| `next_stop_price` | decimal \| null | The stop the rule wants now; null while `waiting`. |
| `next_replace_at_best` | decimal \| null | The best price at which the next replace would be sent. |
| `replaces` | list | Oldest first: `{client_order_id, intent_at, sent_at, from_price, to_price, outcome}`; `outcome` is `sent`, `confirmed` (a broker read shows the new stop), `refused` (with `cause`) or `unknown` (read the broker, never resent). |

The current broker stop stays in `position.stop.price`; "locked" R on screen is
(`stop.price` − `entry_price`) / 1 R, computed by the client.

After the close, each trade in `day-v1` gains `stop_path`: a list of `{at, price}` from the
initial stop and the confirmed replaces, so Trades and Chart draw the same stepped line.

Journal rows: `stop-replace-intent` before the send, `order-attempted` around it, `order-observed`
when a read confirms it, exactly as for other orders (rule 3). An alert kind `stop-replaced`
(info) is added for the notification settings below.

## 3. Production gate

Screens: Settings › Environment (Mac), Settings › Safety (iOS). The trading gate as a shield,
Sandbox/Production as a read-only switch, a readiness checklist with a meter, the authorization
record checked field by field, and a "Copy digest" button. There is no button that enables
Production; the screen explains the config edit and the authorization file.

### `GET /production` → `production-readiness-v1` (read-only)

| Key | Type | Meaning |
|---|---|---|
| `environment` | string | The environment the API process loaded (`[environment]` name). |
| `gate` | `{open, cause, code, params}` | `trading_gate()` for that environment, as `status.trading_gate`. |
| `production` | object | The same gate evaluated for the production environment section, without switching to it: `{trading_enabled, requires_owner_authorization, keys_present, authorization}`. |
| `production.keys_present` | bool | Both credential variables of the production section are set. Names only; values are never read. |
| `production.authorization` | object | `{file, present, valid, cause, environment, approved_on, digest_matches}`; `statement` is not served. |
| `config_digest` | string | `config.digest` the authorization must carry. Not a secret: it is a hash of the committed config. |
| `checklist` | list | `{code, done, value, params}` in a fixed order (below). |

Checklist codes, each computed from data the bot already writes:

- `clean_sandbox_days`: clean days (`GET /days` verdict `clean`) against `[production]
  required_clean_days` (new config key); `value` is "N / M".
- `backtest_verdict`: the latest `judge` verdict of the active strategy (`edge` is done).
- `unattended_safeguard`: the watchdog launchd job is configured and ran today.
- `production_keys`: `production.keys_present`.
- `production_data_probes`: the chart-client probes C1 and C2 recorded for production
  (`docs/webull-facts.md` section C).
- `trading_enabled`: `[environments.production] trading_enabled`.
- `authorization`: `production.authorization.valid`.

The readiness list only informs. The single barrier stays `trading_gate()` (rule 5); the API never
writes the config or the authorization file.

## 4. Notification settings

Screens: Settings › Notifications (Mac), Settings › Safety (iOS). Channels (Mac notification,
iMessage, iPhone push), a matrix of alert kinds by channel with switches, critical kinds locked on,
the latest alerts with how each was delivered, and a test button.

Push needs the Apple Developer Program and APNs (backlog); its column stays drawn as disabled until
then.

Built so far (bot and API side): `GET /alerts` without `recent`, and `PUT /alerts/routing`
(docs/api.md, "Notification settings"). `recent`, `POST /alerts/test` and the app screens follow.

### `GET /alerts` → `alerts-v1`

| Key | Type | Meaning |
|---|---|---|
| `channels` | list | `{kind: mac \| imessage \| push, configured, cause}`; `configured` comes from preflight's checks (Tickr.app present, the iMessage variable set: name only). |
| `kinds` | list | Every `AlertKind` (`runtime/alerts.py`) plus `fill`, `stop-replaced`, `advisor-proposal`: `{kind, group: critical \| trading \| day \| lab, locked, channels: {mac, imessage, push}}`. |
| `recent` | list | The latest `[api] alerts_recent_rows` (new config key) alerts from the journal: `{at, kind, text, code, params, deliveries: [{channel, outcome: shown \| sent \| failed \| suppressed}]}`. |

`locked` is true for kinds that need the owner now (`unprotected-position`, `stop-rejected`,
`exit-refused`, `foreign-exposure`, `broker-reads-failing`, `watchdog-*`, `bot-down-with-open-trade`,
`preflight-failed`, `bot-did-not-start`); their switches cannot be turned off on any configured
channel.

### `PUT /alerts/routing`

Body `{"kind": "<kind>", "channel": "mac" | "imessage" | "push", "on": true | false}`. Changes one
cell, writes `<data_dir>/<[alerts] routing_file>` atomically and answers `200 alerts-v1`.
`409 conflict` for a locked kind turned off, `400` for an unknown kind or channel. This file is an
owner preference about where to be told, never a trading setting, so it lives in the data
directory and not in `config/bot.toml`; the bot reads it on each alert, and a missing or unreadable
file means every kind on every configured channel (an alert is never dropped because of it). On the
remote port it requires the paired token like everything else.

### `POST /alerts/test`

Body `{}`. Queues a `test` alert through the normal alert path; it appears in `recent` with its
deliveries. `202`. Rate-limited by the existing `[alerts] dedupe_window_seconds`.

## Order of building

Trailing stop first (backlog T1, trading-critical, needs the probe and an independent
review). Then notifications (helps unattended running), then the Production readiness view (before
the enabling day). The advisor endpoints are already in progress in their own work.
