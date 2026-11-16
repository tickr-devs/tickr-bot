# Local API, version 1

The bot is a headless service. Clients (the status page now, the macOS app later) talk only to this
local API. The API process is separate from the bot and never talks to the broker's trading or
account routes: it serves the status and account files the bot writes and drops operator commands
into an inbox the bot reads. Its one market-data read, `GET /chart`, is off by default (below).

Planned additions for the backlog screens (advisor, trailing stop, Production gate, notifications)
are in `docs/api-planned.md`.

Run it: `python -m tickr.api --config config/bot.toml`. Settings are in `config/bot.toml` `[api]`.

## Transport and safety

- HTTP on a loopback address only (`[api] host`; a non-loopback address refuses to start).
- Every request must carry `Host: <name>:<port>` with `<name>` in `[api] allowed_hosts`, and if it
  sends `Origin`, it must be `http://<name>:<port>`. Otherwise `403`. No CORS headers are ever sent.
- Commands require `Content-Type: application/json` and a `Content-Length` within
  `[api] max_request_body_bytes`.
- Both ports close a connection that sends nothing for `[api] request_timeout_seconds` (request
  line, headers or body), so an idle or slow client cannot hold a server thread.

## Remote access (the phone)

The API still binds loopback only. The iPhone reaches it through an HTTPS proxy on the same Mac,
[Tailscale](https://tailscale.com) `serve`, which is reachable only from devices signed in to the
owner's tailnet (home Wi-Fi or cellular) and terminates TLS with a certificate for the Mac's
tailnet name. The proxy points at a second loopback port, `[api] remote_port`, opened only when
`[api] remote_hosts_file` names a host. Every request on that port must carry the paired token, whatever its
`Host` header says, except `POST /pair`, which redeems a one-time pairing code for that token; the
local port never accepts the remote name. Whether a token is needed is
decided by the port a request arrives on, never by a header the caller chooses.

1. Install Tailscale on the Mac and the iPhone, sign in to the same account, enable MagicDNS and
   HTTPS certificates in the Tailscale admin console.
2. On the Mac: `tailscale serve --bg --https=443 http://127.0.0.1:8767` (the `[api] remote_port`,
   never the `[api] port`). `tailscale serve status` shows the address, e.g.
   `https://mac.tailnet-name.ts.net`.
3. Write that name, one per line, into this Mac's `[api] remote_hosts_file` under `data_dir`
   (`echo mac.tailnet-name.ts.net > data/remote-hosts.txt`) and restart the API. The name belongs
   to this machine, so it lives with its data, never in the shared `config/bot.toml`.
4. `python -m tickr.api --config config/bot.toml pair` revokes the current phone token at once and
   leaves a pairing link with a one-time code in a file readable by you only
   (`[api] remote_pairing_file` under `data_dir`); nothing secret is printed. With the API running,
   AirDrop that file to the iPhone and open it, or paste the link in Settings › Pair with Mac,
   within `[api] remote_pairing_code_ttl_seconds`. The phone redeems the code once at `POST /pair`
   for its token; the API then deletes the link file. A copy of the link left anywhere (Files,
   iCloud Drive, a clipboard) is worthless after that or after expiry. The Mac keeps only SHA-256
   digests: of the waiting code (`[api] remote_pairing_code_file`, with its expiry) and of the
   token (`[api] remote_token_hash_file`).

Rules on the remote port: `Host` must be one of the names in `remote_hosts_file` (no port when 443, else
`name:port`), else `403 forbidden_host`; `Authorization: Bearer <token>` is required, else
`401 unauthorized` with `WWW-Authenticate: Bearer`; an `Origin`, if sent, must be `https://<host>`.
An unreadable token hash file answers `503 unavailable`. The local port (the Mac app, the status
page) needs no token, as before: any process on this Mac can use it.

`POST /pair` (remote port only; the local port answers `404`): the same `Host` and `Origin` rules,
no token, body exactly `{"code": "<code from the link>"}`. `200` with `paired-v1`
(`{"token": "...", "api_version": 1}`) once: the code is burnt, a new token's SHA-256 replaces the
stored one, and the link file is deleted. `401 unauthorized` when no pairing is waiting, the code
is wrong (nothing changes) or it has expired (the code and the link file are deleted); `400` for
any other body; `503` when the code file is unreadable. Neither the code nor the token appears in
an error or the log. A missing or empty `remote_hosts_file` (the default) turns remote access off; an unreadable
one, or a name in it that is not a lowercase DNS name, stops the API at start.

The phone keeps the token in its Keychain (this device only) together with the address it was
paired with, and sends it only over HTTPS to exactly that address; changing the address by hand
drops the token.

## Wire rules

- Money, prices and quantities are decimal strings (`"12.34"`), never JSON numbers.
- Times are UTC with milliseconds: `"2026-11-14T18:44:39.123Z"`. Dates are New York trading dates:
  `"2026-11-14"`.
- Every object has exactly the documented keys. Nullable keys are present with `null`.
- Every response carries `api_version`; documents written to disk carry `schema_version`.
- Version 1 never removes or renames a key or changes a type. Such a change is version 2 and
  gets new paths and schema files; version 1 keeps working until clients move.

JSON Schemas (draft 2020-12) are committed in `src/tickr/api/schema/` and served at
`GET /schema`. They are generated from the dataclasses in `src/tickr/api/contract.py`
(`python -m tickr.api.contract`); a test fails if code and committed schemas differ.
Example documents for every schema are in `tests/api/fixtures/` (generated by
`tests/api/test_fixtures.py`). The shared Swift package of the apps (`apps/shared/`) decodes and re-encodes each of them
in its Swift tests, so a contract change it does not model fails its CI job.

## Endpoints

| Method | Path | Answer |
|---|---|---|
| GET | `/` | The status page (one HTML file, read-only, refreshes every `poll_seconds`). |
| GET | `/status` | `200` `status-response-v1`, always this shape. |
| GET | `/commands/pending` | `200` `pending-commands-v1`: commands written but not yet consumed. |
| GET | `/schema` | `200` all schemas by name. |
| GET | `/days` | `200` `days-v1`: every New York date with records (closed trades, a bot run, captured data), newest first, each with its clean-day verdict (`clean`). |
| GET | `/days/<YYYY-MM-DD>` | `200` `day-v1`: trades closed that New York date (with prices after the exit and overshoot) and the symbols with captured M1 bars. A date without records answers with empty lists. |
| GET | `/bars?symbol=<ticker>&date=<YYYY-MM-DD>` | `200` `bars-v1`: captured M1 bars of one symbol on one date, oldest first. |
| GET | `/chart?symbol=<ticker>&interval=<1m\|5m\|30m\|1h\|1d\|1w\|1mo>&range=<1d\|5d\|1m\|3m\|1y>` | `200` `chart-v1`: candles of any US stock from market data; `400`, `429` `rate_limited`, `503` `unavailable`. |
| GET | `/performance?from=<YYYY-MM-DD>&to=<YYYY-MM-DD>` | `200` `performance-v1`: gross results in R and USD of the trades closed from `from` to `to` (New York dates, both included): counts, win rate, expectancy, averages, profit factor, max drawdown, a per-day series. |
| GET | `/account` | `200` `account-response-v1`: positions and open orders from the bot's latest successful broker read, whether they matched its records, and the latest balances. `503 unavailable` when the file is missing, invalid or dated in the future. |
| GET | `/settings` | `200` `settings-v1`: risk limits, session times, active strategy and observers from `config/bot.toml`, as the API process loaded them at its start (read-only; change the file and restart). `broker_read_max_age_seconds` is the age of `status.freshness.last_broker_read_at` past which entries are blocked; `broker_read_warn_seconds` (always below it) is where clients start showing a warning. |
| GET | `/config` | `200` `config-v1`: the configuration file the API was started with, as it is on disk now, read-only. Every `[section]` in file order, each key with its value as written (TOML text) and the comment above it. `valid`/`error`: whether the file loads now, as the next start would. `changed_since_start`: the file differs from the one this API loaded (running processes keep the old values until restarted). The file holds credential variable names only, never values. `503 unavailable` when the API was started without a file path (the demo) or the file is unreadable. |
| GET | `/jobs` | `200` `jobs-v1`: the bot's scheduled work for the Settings jobs strip (read-only; see GET /jobs below). |
| GET | `/journal` | `200` `journal-v1`: the latest `[api] journal_rows` rows of the bot's journal with their numbers (read-only; see GET /journal below). |
| GET | `/advisor` | `200` `advisor-runs-v1`: the AI advisor's newest `[api] advisor_runs` runs with their proposals, ideas, hold-out verdicts and the owner's decision (read-only; see GET /advisor below). |
| POST | `/advisor/decision` | Body `{"run": "<run id>", "verdict": "accepted"\|"rejected"}`. Records the owner's review of a proposal or idea (a note, nothing is deployed): `200` `advisor-runs-v1`; `404 not_found` without that run; `409 conflict` for a refused, no-change or unreadable run. Not a bot command. |
| GET | `/lab/replay` | `200` `lab-replay-v1`: the Lab's latest SIMULATED replay of a strategy version (and the one compared with it) over the last sessions of fetched data or one day, plus the versions and days a run may ask for (see Lab replay below). |
| POST | `/lab/replay` | Body `{}` or `{"version", "compare", "day"}` (any of them). Starts that replay: `202` `lab-replay-v1` (state `running`), or `409 conflict` with the cause it cannot start now. Not a bot command: nothing goes to the inbox. |
| GET | `/trades/whatif?date=<YYYY-MM-DD>&symbol=<ticker>&version=<folder>` | `200` `trade-whatif-v1`: what strategy folder `version` would have done on that symbol and day, SIMULATED from the captured bars (see Another version on one trade below). `409 conflict` during the regular session. |
| GET | `/explain?symbol=<ticker>` | `200` `explain-v1`: the latest decision of the active strategy recorded today for one symbol, with every check. `404 not_found` when none is recorded today. |
| GET | `/observers?date=<YYYY-MM-DD>` | `200` `observers-v1`: every configured observer with its recorded decisions of that date (default today), oldest first, its simulated day result and simulated trades. |
| GET | `/decisions?symbol=<ticker>&date=<YYYY-MM-DD>` | `200` `decisions-v1`: every decision recorded for one symbol on one date (active strategy and observers, marked), oldest first, with checks. |
| GET | `/skipped?date=<YYYY-MM-DD>` | `200` `skipped-v1`: every ENTER of the active strategy that date (default today) that did not become a position (admission `refused` or `not_evaluated`), oldest first, with checks, prices after the signal and the simulated what-if. |
| GET | `/flow?date=<YYYY-MM-DD>` | `200` `flow-v1`: the Overview's live flow that date (default today): screener chips and the pipeline stages from screeners to verified broker stops, with reconciliation. |
| GET | `/watching?date=<YYYY-MM-DD>` | `200` `watching-v1`: every symbol the active strategy is watching on that date (default today), closest to entering first. |
| GET | `/symbols?date=<YYYY-MM-DD>` | `200` `symbols-v1`: every symbol the bot looked at that date (default today), with the furthest pipeline stage it reached and where and why it stands now (see GET /symbols below). |
| GET | `/movers?date=<YYYY-MM-DD>` | `200` `movers-v1`: the fast movers of that date (default today), newest bar first, with the active strategy's thresholds (see GET /movers below). |
| POST | `/commands/entry-stop` | Body `{"state": "on"}` or `{"state": "off"}`. `202` `command-accepted-v1`. |
| POST | `/commands/cancel-own-orders` | Body `{}`. `202` `command-accepted-v1`. |
| POST | `/commands/flatten` | Body `{}` (everything) or `{"symbol": "<TICKER>"}` (only that position). `202` `command-accepted-v1`. |
| POST | `/commands/stop-bot` | Body `{}`. Graceful stop that sticks. `202` `command-accepted-v1`. |
| POST | `/commands/emergency` | Body `{}`. Entry-stop on, cancel own orders, flatten all. `202` `command-accepted-v1`. |
| POST | `/commands/start-bot` | Body `{}`. Starts the bot job through launchd (not a bot command, see "Start-bot"). `202` `bot-start-v1`; `409 conflict` while a fresh status says the bot is starting, running or stopping, or a live process holds the bot's lock; `503 unavailable` when no start command is configured, the stop marker cannot be removed, or the command fails. |

Errors are `error-v1` `{"error": code, "cause": text, "api_version": 1}`:
`forbidden_host` / `forbidden_origin` 403, `not_found` 404, `method_not_allowed` 405,
`unauthorized` 401 (remote request without the paired token),
`length_required` 411, `payload_too_large` 413, `unsupported_media_type` 415, `bad_request` 400,
`too_many_pending` 429, `rate_limited` 429 (GET /chart: its request budget is spent; `Retry-After`),
`conflict` 409 (an `Idempotency-Key` already used for another command; a Lab replay that cannot
start now),
`internal` 500, `unavailable` 503 (history cannot be read now: database of another schema
version, unreadable capture file).

### GET /status

`status` is the bot's `status-v1` document, or `null` with `unavailable_cause` when the file is
missing, invalid or dated in the future. `age_seconds` = `served_at - status.written_at`.
`stale` is true when the age exceeds `[api] status_stale_seconds`. `summary` is the one line to
show first, in plain words: the single entry-blocking reason, "Entries allowed.", or why the status
cannot be trusted. Clients should poll every `poll_seconds`.

`entry_block` is `null` only when admission could take an entry now. Its `cause` is a stable
code and `attention` says how to show it. `attention: true` is a real block (trading gate,
operator pause, a fence: the code is the fence name with `_`, a daily ceiling, strategy not
loaded, `calendar_unknown`). `attention: false` is an ordinary state that passes on its own:
`no_session_today`, `outside_regular_session`, `after_last_entry`, `own_trade_open`.
`fences[].kind` is the bot's fence name, with its key after a colon when it has one
(`foreign-position:ABCD`, `foreign-order:<client order id>`, `broker-read-failed:open-orders`,
`unprotected:<trade>`): match the part before the colon.
`attention` was added after version 1 shipped: a reader takes a document without it as
`attention: true`; the bot always writes it.

Reason codes, so clients can say these in the reader's language (the English text stays the
fallback). Each `params` is an object of plain-text values: numbers as written, times `HH:MM`
New York, days `YYYY-MM-DD`. Codes are read from the bot's own texts against the exact shapes
it writes (`api/reasons.py`); a text of any other shape is code `unrecognized` with `{}`, and
clients show the text.
- `entry_block.params`: `outside_regular_session` `{open, close}`; `after_last_entry` `{time}`;
  `daily_ceiling` `{limit_usd, min_room_usd}`; `no_session_today` `{day}` (`weekend`,
  `holiday`, `half_day`); `calendar_unknown` `{calendar}` (`not_loaded`, or the calendar or
  admission cause in lower case, e.g. `out_of_coverage`); `trading_gate_closed` `{gate, ...}`
  (the gate's `code` and its params); a fence block `{fence, ...}` (the fence's `code` and its
  params). Every other cause: `{}`.
- `trading_gate.code` / `params`: `trading_enabled`, `authorization_valid`, `trading_disabled`
  `{environment}`, `authorization_file_not_set`, `authorization_file_missing` `{file}`,
  `authorization_not_toml`, `authorization_keys_wrong` `{keys}`,
  `authorization_other_environment`, `authorization_digest_mismatch`.
- `fences[].code` / `params`, by kind: `startup_reconcile_pending`; `positions_read_failed` /
  `open_orders_read_failed` `{error}` (an exception type name); `detail_budget`;
  `send_outcome_unknown`, `crashed_during_send`; `order_missing`; `status_unmapped` `{status}`;
  `detail_unreadable` / `stop_cancel_unconfirmed` `{seconds}`;
  `stop_covers_part` `{verified, expected}`; `stops_over_position` `{covered, held}`;
  `unprotected_exiting` / `unprotected_close_by_hand` / `unprotected_stop_unreadable`
  `{seconds}`; `stop_rejected_exiting`;
  `exit_refused_no_stop`, `exit_refused_stop_again`; `cancel_outcome_unknown` `{outcome}`,
  `cancel_crashed`;
  `cancel_failed`; `cancel_not_sent` `{seconds}`; `given_up_order_filled`,
  `working_not_in_records`; `foreign_position`; `foreign_order`; `quantity_mismatch`
  `{held, expected}`; `shutting_down`; `operator_command`, `operator_emergency`;
  `replace_unresolved` `{stop_price}`; `held_after_replace`, `held_untracked_sell`
  `{orders, symbol}`; `exit_not_filled` `{order, quantity, symbol, seconds, filled, state}`;
  `close_not_sent` `{quantity, expected, symbol}` plus why the close was not due: `{session}`
  (no session, the calendar's text), `{open}` (before the open) or `{cutoff, cutoff_seconds,
  close, day}` (at or after the last close time; `cutoff` is `HH:MM:SS`).
- `code` and `params` were added after version 1 shipped: a reader takes a document without
  them as `unrecorded` and `{}`.

The API's own causes carry their codes in a sibling `<field>_codes`: a list of `{code, params}`,
empty when the cause is `null`, one entry for a cause that reads as one, else one per `; `-joined
part (an entry send's missing rows, a clean day's faults). The bot computes it from the cause on
every build, so the two always agree; a reader of a document without it rebuilds it the same
way. Siblings: `entry_cause_codes` (positions, `trades_today`, day-v1 `trades`),
`prices_after_cause_codes`, `overshoot_cause_codes` (day-v1 trades, skipped signals),
`cause_codes` (each `prices_after` point, `what_if`, observer `trades`, `clean`, performance
`exclusions`), `text_codes` (`clean.causes[]`), `result_cause_codes` / `trades_cause_codes`
(observers), `closeness_cause_codes` / `quote_age_cause_codes` (watching), and one per
performance `<name>_cause`. Codes (params in braces; wire times become `HH:MM` New York):
- entry send: `entry_orders_count` {count}, `no_intent_row`, `never_attempted`, `no_fill_row`,
  `journal_unreadable` {detail}, `not_read`, `not_computed`;
- prices after and overshoot: `session_ended` {time}, `no_bars_for_symbol` {symbol}, `bars_end`
  {time}, `bars_end_before` {time, until}, `no_bar_for_minute` {time}, `no_reference_price`,
  `calendar_refuses` {day, detail}, `no_minute_before_close` {time}, `no_bar_after_exit`
  {until}, `planned_loss_not_positive` {planned_loss};
- simulations: `simulation_refused` {detail}, `simulation_outcome` {outcome, detail},
  `skipped_overlap` {symbol}, `skipped_same_signal` {setup, trigger, symbol}, `no_decisions`,
  `no_captured_bars` {day}, `capture_refused` {detail}, `replay_settings_unreadable` {detail},
  `calendar_unreadable` {detail}, `enters_not_simulated` {count}, `trades_without_quantity`
  {count}, `result_in_r_only`;
- clean day: `day_not_over`, `reconcile_not_recorded`, `no_day_end`, `day_end_unknown`,
  `no_calendar`, `calendar_error` {detail}, `reconcile_gap` {since, until, seconds, max_gap},
  `reconcile_mismatch` {detail}, `fence` {fence, fence_code, ...} (a fence's cause, with the
  fence codes above), `fence_still_set` {fence}, `day_end_not_clean` {broker_flat, agree,
  open_trades}, `unprotected_alert` {detail}, `safety_exit` {exit, trade}, `emergency_command`
  {outcome};
- watching: `all_checks_passed`, `quote_age_not_recorded`, `yes_no_check` {rule},
  `needs_positive_threshold` {rule}, `needs_positive_measured` {rule}, `no_direction` {rule,
  operator}, `check_contradicts_record` {rule};
- performance: `planned_loss_not_positive_r`, `no_trades`, `no_wins`, `no_losses`;
- any of them: `display_failed` {error, detail} (a display part failed unexpectedly).
`{detail}` is untranslated text (an error message, a calendar's own words). A check's `note` is
the strategy's own fixed text per rule and has no code: translate it by `rule` and the exact
text, or show it as is.

Daily loss money (spec 7, check 6), exactly as the bot's admission computes it; `null` means
unknown (a trade closed today has fills without a known price or without recorded fees), never
zero: `realized_loss_today_usd` is today's net realized loss after the broker's fees (wins offset
losses, never below 0; fees counted since 2026-11-16, owner decision), `open_reservations_usd`
the planned loss of every open trade, and `daily_room_usd` = `[risk] max_daily_loss_usd` -
realized - reserved (may be negative). Entries are refused while the room is below
`settings.risk.min_daily_room_usd`. `positions` lists every open position the bot tracks;
`position` is its first element or `null`, kept for older clients.

Fill prices and fees come from the bot's own records: once an order is finished, the loop
records its fees from the order history (`fees[].actual_value`) and, where the account's cash
activity proves it, its exact fill price (Webull's order rows round sub-dollar prices to cents).
Each `trades_today` entry has `result_exact_usd`: the trade's result after those fees, as the
daily loss counts it, `null` until every filled order's fees are recorded (the apps show it in
place of `result_gross_usd` once present).

`result_today_gross_usd` sums today's closed trades before fees. `fees_today_usd` is the recorded
fees on the fills of those trades, and `result_today_net_usd` = gross - fees, the figure the
broker's day P&L shows and the negative of the realized loss when it is a loss. Both are `null`
until every filled order of today's closed trades has its fees recorded.
`result_today_broker_usd` is the broker's own realized result for today: its day P&L less the
unrealized part, from the latest balance read, `null` until a balance read made after the last
of today's trades closed; it covers the whole account (manual trades too). The apps show it as
today's result when present, else the net, else the gross. Display only.

Each `trades_today` entry also has `planned_loss_usd` (1 R as recorded when the trade was opened)
and `stop_confirmed_after_seconds` (see "Per-trade metrics" below). MAE and MFE are only in
`day-v1`: the bot does not read capture files to write its status.
`strategy` (in `trades_today` and in `day-v1` `trades`) is `{name, version}` of the strategy whose
decision opened the trade, as journaled in its `trade-opened` row; `null` for a manual trade, a
trade opened before the bot journaled it, or a row it cannot read (display only, never guessed).
In status it was added after version 1 shipped: a reader takes an entry without it as `null`.
`code_version` (in `trades_today` and in `day-v1` `trades`) is the bot code version the trade was
opened by: the full git commit SHA of the run that opened it, with `-dirty` when a tracked file
differed from that commit (in the Docker image, the commit it was built from). It is read from
that run's own record (`runs.code_version`, via the trade's `run_id`), never re-derived; `null`
for a trade of a run recorded before the bot recorded its version. The status-v1 top-level
`code_version` is the current run's (next to `run_id`). All three were added after version 1
shipped: a reader takes a document without them as `null`.

Every `positions` element (so also `position`) and every `trades_today` entry has `entry` and
`entry_cause`, the first link of the protection chain (see "Entry send" below).

### GET /account

`account` is the bot's `account-v1` document, written (temp file + fsync + rename, like status)
after every successful reconciliation: a broker read of open orders and positions, at startup
and every `[orders] reconcile_interval_seconds`. A failed read writes nothing, so the age grows; a failed write
is journaled once (`account-write-failed`) and the bot carries on. `read_at` is the time the file
was written, just after the reads. `age_seconds` = `served_at - account.read_at`; `stale` is true when the age exceeds
`[api] account_stale_seconds`. A missing, invalid or future-dated file is `503 unavailable` with
the cause.

- `positions`: every non-zero position the broker listed (`avg_price` null when the broker row
  does not show it). `last_price`, `market_value_usd`, `unrealized_pl_usd` and `day_pl_usd` are
  the broker's own figures for the position, display only (null when absent or unreadable).
- `fills`: today's orders with a fill, oldest first, from the broker's order history
  (`GET /trading/orders/historical-orders/list`, no `start_time`, at most
  `[environments.*] history_max_pages` pages, kept to the New York date), read on the same
  thread with the balance and under the same rules (`fills_read_at`, `fills-read-failed` journaled once per stretch, never
  blocks anything). Each has the broker's `side` and `type`, the filled `quantity`, the
  average fill `price`, `filled_at`, `fees_usd` (the sum of the broker's actual fees, null when
  one is unreadable) and `ours`. `net_amount_usd` is the fill's exact signed cash amount from the
  account's cash activities (`GET /trading/activities/cash-activities/list`, TRADE rows of the
  day, matched by symbol, side and the fill time; fees included; a buy is negative) and
  `exact_price` the average price it gives; both null until that activity is listed or when it
  does not match exactly one row (the order rows round sub-dollar prices to cents).
- `orders`: every open order the broker listed. `side`, `type` and `status` are the bot's names
  (`BUY`/`SELL`; `MARKET`/`LIMIT`/`STOP_LOSS`; `ACCEPTED`, `PARTIALLY_FILLED`, ...). `ours` is
  true when the client order id is one of the bot's own. `limit_price` is the broker's, read on
  LIMIT rows only (null on every other type).
- `matches_records` is true only when the broker's positions equal what the bot's open trades
  expect and its open orders are exactly the bot's live orders (the same test as the day-end
  record), AND no fence says otherwise: foreign position or order, quantity mismatch,
  resurrected, unknown send outcome, order missing, unknown order status, over-protected.
  `mismatch` lists those fences' causes (`name: cause; ...`), or one plain sentence when only
  the comparison fails. `resurrected` is an operator fence that stays until it is cleared, so
  `mismatch` may describe something the broker has already resolved.
- Balances come from the latest successful balance read (`GET /trading/assets/balances/get`,
  docs/webull-facts.md), made on the bot's own display-read thread every
  `[orders] balance_read_seconds` (the trading loop never waits on it and only takes the
  latest result when it writes this file); `balance_read_at` is when it succeeded. All are
  null until one succeeds. A failed read keeps the previous values (with their
  `balance_read_at`), is journaled once per stretch (`balance-read-failed`; an unexpected error
  in the thread: `account-reads-failed`) and never blocks anything: balances are display only,
  and no limit or decision reads them (limits live in the platform settings).
  - `equity_usd`: net liquidation value (cash plus positions at market).
  - `buying_power_usd`: day (intraday) buying power; `overnight_buying_power_usd`: overnight.
  - `cash_usd`, `positions_value_usd` (positions at market), `unrealized_pl_usd`, `day_pl_usd`
    (the broker's day profit or loss), `maintenance_margin_usd`.
  - `margin_calls`: how many open margin calls the broker lists.
  - `day_trades_left`: day trades the broker still allows; null with a balance read means no
    limit (`UNLIMITED`).
  - `account_label`: the broker's name for the account kind ("Individual Margin"), read once
    from the accounts list; null until read. Never the account id or number.
- No account id or broker order id is ever in the document.

### History (read-only)

`/days`, `/days/<date>` and `/bars` serve what the bot already recorded, for clients that show past
days and charts. Trades come from the bot database, opened read-only (WAL: the reader never blocks
the bot), limited to runs of the configured environment; a trade belongs to the New York date it
closed. A trade is listed once it is closed and had fills: quantity is what was sold, prices are
fill-weighted averages, `stop_price` is the first protective stop sent (null if none was), and
1 R = `planned_loss_usd`. Bars come from the capture files
(`<capture_dir>/<environment>/<date>/bars.jsonl`): one bar per start time, the last version
received when the provider revised one. On today's file a last line still being written is not
read yet. Only M1 is served; clients aggregate M5/M15 and compute indicators themselves.
Query strings take exactly the documented parameters, each once; anything else is `bad_request`.

#### Clean day

Every `days-v1` entry has `clean`: did the broker and the bot's records agree all that New York
date? Built only from the bot's journal (`src/tickr/api/cleanday.py`), rows of runs of the
configured environment, bucketed by the New York date of their journal time. Display only.

```json
"clean": {
  "verdict": "not_clean",
  "cause": null,
  "causes": [{"kind": "safety_exit", "text": "exit stream-lost for trade t7", "journal_seqs": [8502]}],
  "reconcile_rows": 82,
  "first_reconcile_at": "2026-11-12T14:20:01.000Z",
  "last_reconcile_at": "2026-11-12T21:05:00.000Z",
  "day_end_seq": 8890
}
```

- The record: after every successful reconciliation (positions and open orders read) the bot
  journals `reconcile-checked` `{matches_records, mismatch}` (the same test and text as
  account-v1) when the answer differs from the last such row, and otherwise at most once per
  `[orders] reconcile_proof_seconds` (300). A failed read writes none. Dates before this row
  existed have none. A disagreement is recorded only once confirmed: a mismatch fence is set,
  or the records disagree on two successful reconciliations in a row (a failed read in between
  starts the count again). One pass alone can disagree for a moment: reconcile reads open
  orders, then its step places the stop for a fill it just saw, so the stop is live in the
  records but not in that read; such a pass writes no row.
- `not_clean` when any of these rows exists; `causes` has one entry per kind, `journal_seqs`
  the rows (`journal.seq`) that prove it:
  - `reconcile_mismatch`: a `reconcile-checked` row with `matches_records` false that date (a
    reconciliation that saw an unknown order outcome counts too: account-v1 calls that a
    mismatch), or a `fence-set` row that date of `foreign-position`, `foreign-order`,
    `quantity-mismatch`, `resurrected`, `order-missing` or `over-protected`;
  - `unknown_order_outcome`: an `unknown-order`, `status-unknown`, `cancel-unknown` or
    `replace-unresolved` fence set before the end of the date and not cleared by then (also
    when it was set on an earlier date); its `fence-set` row;
  - `unprotected_position`: a `fence-set` row of `unprotected-timeout` or `stop-rejected`, or an
    `unprotected-position` alert, that date (the short `unprotected` fence while a fresh fill's
    stop is being verified is not a fault);
  - `safety_exit`: an `exit-requested` row that date with cause `stop-rejected`,
    `unprotected-too-long` or `stream-lost`, or an `emergency` command row (any outcome). A
    session flatten or an operator flatten is not a fault;
  - `day_end_not_clean`: the date's `day-end` row has `day_clean` false (not flat, records not
    agreeing, or a trade still open).
- Otherwise `unknown` with one `cause`, checked in this order: the date is today or later
  (`the day is not over`, even with faults already recorded), no `reconcile-checked` row that
  date (`not recorded: ...`), no `day-end` row (the bot was not running after the flatten
  time), the day-end row has `day_clean` null (no broker reads), the session calendar cannot be
  read or has no session that date (`calendar: ...`), or a gap in the regular session longer
  than `[orders] reconcile_max_gap_seconds` (900; the bot down, or hours of failed reads):
  `no reconcile-checked row from <time> to <time> (...)`. The gaps measured are between the last
  row at or before the regular open (else the open itself), every row inside the session, and
  the first row at or after the regular close (else the close; the early close on a half day).
  A journal row of these kinds that cannot be read makes every date `unknown` with `journal
  unreadable: ...`.
- Otherwise `clean`. `reconcile_rows`, `first_reconcile_at`, `last_reconcile_at` and
  `day_end_seq` are the evidence, shown for every verdict.

#### Per-trade metrics

Computed by the reader from what the bot already records; `null` means unknown, never zero.

- `planned_loss_usd` (status `Trade` and `day-v1`): 1 R, the planned loss recorded when the
  trade was opened (`trades.planned_loss_usd`). R multiples are left to the client
  (`result_gross_usd / planned_loss_usd`, `mae_usd / planned_loss_usd`, ...).
- `stop_confirmed_after_seconds` (status `Trade` and `day-v1`): seconds, millisecond precision,
  from the first entry fill to the broker confirming the protective stop. Both ends are journal
  times (`journal.at_utc`) of `order-observed` rows (one per Detail read that changed an
  order): the start is the first read of the entry showing filled quantity > 0 (Webull Detail
  has no fill time, so this is when the fill was seen, not when it happened); the end is the
  first later read of one of the trade's stop orders showing it working at the broker (state
  `ACCEPTED` or `PARTIALLY_FILLED`; the engine's "verified" stop). The same two rows are
  `fill_observed_at` and `stop_verified_at` of the trade's `protection-latency` journal row.
  `null` when the stop was never confirmed (rejected, or filled or gone before a read showed it
  working) or no fill read is journaled.
- `mae_usd` / `mfe_usd` (`day-v1` only): maximum adverse and favourable excursion while shares
  were held, as a **lower bound** (never overstated): `(lowest price - entry_price) x held` and
  `(highest price - entry_price) x held`. Journal times lag the events they record
  (`order-observed` is written only when a read shows a change), so only what the journal
  proves counts:
  - Window start: the minute of the first entry read showing a fill. Its bar is left out (it
    may hold prices from before the fill).
  - Window end ("held until"): for each stop or exit order that sold shares, the latest moment
    the journal proves it had not sold yet. For a close (MARKET exit) that is its send intent
    (`order-intent` row, written before the send); for a stop, its last read still showing
    nothing filled: an `order-observed` row (in practice its working read) or an `order-held`
    row. The bot writes `order-held` from the Detail reads it already makes (no extra broker
    request): when a read shows the stop working (`ACCEPTED` or `PARTIALLY_FILLED`) with
    filled quantity 0 and no change, and the order's last `order-observed` or `order-held` row
    is at least `[orders] stop_held_proof_seconds` (60) old, it journals
    `{state, raw_status, filled_quantity}` keyed by the client order id, at the read's
    journal time like `order-observed`. A held row with filled quantity above 0, or after a
    read of the order that showed a fill, proves nothing. The earliest of these over all
    orders that sold is the end. Its minute's bar is left out too.
  - Prices: the entry fill price (fill-weighted average), the fill price of each order that
    sold shares, and the low and high of the captured M1 bars that start after the start
    minute and before the end minute. If an order that sold has no proving row, no bar
    counts, only the fill prices.
  - `held`: the filled quantity the first entry fill read showed (later fills are not
    counted), never more than the quantity sold. Nothing is sold inside the window, so that
    many shares were held throughout.
  - Capture: the bars of the first entry fill read's minute and of the last read showing
    shares sold must be in the capture file, as proof it covered the trade; their prices are
    not used.

  So a stop-out at -1 R shows about -1 R (its fill), never the fall after it. A stop-out's
  window ends at most about `stop_held_proof_seconds` (plus a Detail poll) before the read of
  its fill, so up to two minutes before the stop-out are not counted; a stop-out within
  that time of the entry shows its fill prices alone. A restart between the last proving read and the read
  of the fill changes nothing (the proven window is used). As the entry price is always
  included, MAE is at most 0 and MFE at least 0; no value is defaulted. `null` when either
  edge minute's bar is not captured, no entry fill read or no read of the sale is journaled.
- A journal row these metrics cannot read gives `null` for the trade's stop timing and
  excursions, never an error. In the bot it is journaled once per trade
  (`stop-timing-failed`) and the step goes on. The daily-loss check never reads them: the
  bot computes them only for the status file.

#### Prices after, overshoot and what if

Display only (`day-v1` trades and `skipped-v1` rows): computed by the API from the captured M1
bars (`bars.jsonl` or its `.gz`) and the session calendar; never read by the bot, the risk
checks or the daily-loss path (`closed_trades` fills them with `prices_after: []` and the
cause `not read on this path`). `null` means unknown, never zero; every `null` has a cause.
Code: `src/tickr/api/after.py`.

- `prices_after` (trades and skipped rows): one `{minutes, at, price, change_usd, cause}` per
  `[api] price_after_minutes` (1, 5, 15, 60). `at` = anchor + `minutes`; `price` is the
  **close of the captured M1 bar covering `at`** (the bar whose start is `at` floored to the
  minute), `change_usd` is `price` minus the reference, per share. Anchor and reference: for a
  trade, `closed_at` (the journal time the bot recorded the close, at or after the exit fill)
  and `exit_price`; for a skipped signal, its decision bar start `at` and that bar's close, so
  a point is the bar ending `minutes` after the decision bar ends (the price research
  `outcomes` measures). A point is `null` with `cause`: `the session ended at <close>` (`at`
  at or after the regular close; post-market bars are never used), `no bar recorded for
  <symbol> that day`, `the bars end at <last bar start>` (also today, until the bars arrive),
  `no bar recorded for the minute <minute>` (a minute without a bar is never filled from
  another minute), `session calendar unreadable: ...`, `calendar refuses <date>: ...`. A
  skipped signal whose decision bar was not captured keeps `price` with `change_usd` `null`
  and that cause. `prices_after` is `[]` with `prices_after_cause` when no point could be
  computed (`no captured bars for <date>`, `capture file refused: ...`); otherwise
  `prices_after_cause` is `null`.
- `overshoot_usd` / `overshoot_r` / `overshoot_cause` (trades): how far the price went beyond
  the exit in the trade's favour (long only): the highest high of the captured bars that start
  after the minute of `closed_at` (that minute's bar is left out: it may hold prices from before
  the exit) up to the bar covering `closed_at` + the longest `price_after_minutes`, or the last
  bar before the regular close if that is sooner, minus `exit_price`, in USD per share. Signed:
  below 0 the price never came back to the exit. `overshoot_r` = `overshoot_usd x quantity /
  planned_loss_usd`, rounded half-even to `[api] after_r_decimal_places`. `null` with the
  cause when the symbol has no bar that day, the bars end before the window does, no bar was
  recorded inside it, the exit was in the session's last minute, or the calendar is unreadable
  (`overshoot_r` alone is `null` when `planned_loss_usd` is not positive).
- `what_if` (skipped rows): `{result_gross_usd, result_r, exit_reason, cause}`, SIMULATED: the
  signal entered alone with the research what-if simulator (`research.whatif.simulate_one`,
  the replay's conservative fill model, `[api] replay_settings_file`, the session clock as
  live), for the recorded quantity, gross (slippage in, commissions out), rounded to the
  replay settings' `output_decimal_places` like `/observers`. `result_r` is per share against
  the simulated entry and stop; `exit_reason` is `stop`, `strategy_exit`, `flatten` or
  `end_of_data` (today: a simulated trade still open ends at the last captured bar's close,
  so the result is provisional). Unlike `/observers`, an earlier simulated trade of the same
  symbol does not skip a row: each row answers "what if this one had been entered". All three
  `null` with `cause`: `missed: ...` (price never traded inside the window in time),
  `rejected: ...` (live admission would refuse it on time alone), `not_simulated: ...` (no
  whole quantity, decision bar not captured), `simulation refused: ...`, or an unreadable
  input as above (`replay settings unreadable: ...`).
- Any unexpected error in these values shows `null` with `<Error>: <detail>` as the cause,
  never an error answer for the whole document.

The `/skipped` answer is kept in memory with the same key as `/observers` (date, whether it is
today, size and modification time of the decision, capture, replay settings and calendar
files), so polling re-simulates only after a new bar or record; an answer an unreadable
calendar or an OSError decided is not kept. `day-v1` points are a few dictionary lookups per
trade on the bars the history reader already caches.

#### Entry send

The protection chain per position is: entry sent once -> filled -> broker stop verified
(`stop`). `entry` (status `Position` and `Trade`, `day-v1` trades) is its first link, read from
the journal rows the engine writes for the trade's entry order (`journal.key` = its client
order id; the engine records every send durably before the HTTP call, sends it once and never
retries it):

```json
"entry": {
  "client_order_id": "ap-1a2b3c4d-t1-e1",
  "intent_at": "2026-11-16T15:00:00.123Z",
  "sent_at": "2026-11-16T15:00:00.125Z",
  "send_outcome": "unknown",
  "latest_outcome": "acknowledged",
  "sends": 1,
  "filled_at": "2026-11-16T15:00:01.625Z"
},
"entry_cause": null
```

- `client_order_id`: the bot's own id for the entry order (`[orders] client_order_id_prefix`,
  the first 8 hex characters of the random run id, trade id, leg and number). No account or
  broker id.
- `intent_at`: the `order-intent` row, written before the send.
- `sent_at`: the first `order-attempted` row, written just before the HTTP call (see `sends`).
- `send_outcome`: the engine's send state (`SendState`) that the send itself recorded:
  `acknowledged`, `refused` (no order exists) or `unknown` (may exist; never resent, resolved
  by broker reads), the row after the attempt. It is not changed by a later resolution: an
  `unknown` send that a read then found at the broker stays `unknown`, with `filled_at` once
  filled. With no attempt row it is the row after the intent (`absent`: never attempted, as
  recovery found it), and while nothing follows it is the last state (`intent` or
  `attempted`).
- `latest_outcome`: the latest send state of the entry order (same values; its last
  `order-<state>` row). With `send_outcome` it says, for example, "unknown at send, confirmed
  by a broker read" (`unknown` then `acknowledged`, as in the example above), or `absent`
  when reads found no such order. Usually the same as `send_outcome`.
- `sends`: the number of send attempts of the entry order (its `order-attempted` rows): 1.
  Shown as counted, never clamped; anything else than 1 is something to look at. An attempt
  is journaled before the request goes out, so an attempt that our own request budget refused
  (`send_outcome` `refused`) counts as one although no HTTP call was made.
- `filled_at`: the first `order-observed` row of the entry showing filled quantity > 0 (when
  the fill was seen, as in "Per-trade metrics").
- `entry_cause`: `null` when every field is known; otherwise why each null one is, joined by
  `; ` (for example `never attempted: no order-attempted row`, `no order-observed row shows
  the entry filled`, or `trade t3 has 2 entry orders, expected 1`, which leaves every field
  null). No field is ever defaulted.

Display only, like the stop timing: the trading loop and the daily-loss check never read it.
A journal row it cannot read makes every field `null` with `entry_cause` = `journal
unreadable: <error>`; in the bot it is journaled once per trade (`entry-send-failed`) and the
step goes on. The bot reads it again on every status write while the trade is open, and keeps
it once the trade is closed (or a row is unreadable). `/days/<date>` computes it the same way.

### GET /jobs (read-only)

When the bot's scheduled work last ran and runs next. Code: `src/tickr/api/jobs.py`. Sources,
none written by this request: the status file, the database opened read-only, `[runtime]`,
`[orders] reconcile_interval_seconds`, and the session calendar with `[session]` cutoffs. The bot
reads and writes nothing for it.

- `bot_state` and `status_stale`: as in GET /status (`bot_state` null without a valid status file).
  `bot_started_at`: when the status's run started (database `runs`), or null.
- `reconcile`: `interval_seconds`, `last_at` (the status's `freshness.last_broker_read_at`), and
  `next_at` = `last_at + interval_seconds` only while the bot runs (state `running`, status not
  stale); otherwise null.
- `backup`: the verified daily database backup, which the bot makes on its first loop pass of each
  New York date. `last_at` is the newest `backup` journal row. `last_failed_at` and
  `last_failed_error` (the error's type name, never its text) are set only when a `backup-failed`
  row is newer than that. `cause` says why `last_at` is null (no database yet, no backup yet).
- `launchd`: the preflight, bot and watchdog jobs as configured: `local_time` "HH:MM" in the Mac's
  own time zone and launchd `weekdays` (1 = Monday ... 5 = Friday). No next time is computed: the
  API does not guess the Mac's time zone rules, and the session calendar still decides each day.
- `session`: today's New York date with `regular_open`, `last_entry_at`, `flatten_at` and
  `regular_close`, and the calendar's extended hours `premarket_open` and `extended_close`
  (shown, not traded), or null with `session_cause` (a weekend, a closure, a half day with
  `half_days_enabled = false`, a calendar that does not cover today). `premarket_open` and
  `extended_close` were added after version 1 shipped: a reader takes a session without them as
  having unknown extended hours; the bot always writes them.
- `nightly_fetch`: the nightly backtest-data refresh's last outcome, from its `nightly.json`
  (docs/backtest.md, "Nightly"): `finished_at`, `last_day` (where the Lab's data now ends),
  `new_sessions` added (0 = already up to date) and `cause` when it was refused or stopped (the
  data then stays as it was). All null with `cause` when it never ran or the file is unreadable.
  `launchd` lists it as `nightly-fetch`.

### GET /journal (read-only)

The Overview's journal card and its "line #N". Code: `src/tickr/api/journal.py`. Reads the
database opened read-only; the bot reads and writes nothing for it. `rows`: the latest
`[api] journal_rows` journal rows, oldest first, each with `seq` (the journal's own row number,
one more per row, never reused), `at`, `kind` (the bot's event name, such as `order-intent`,
`trade-opened`, `stop-from-fill`, `reconcile-checked`: clients map kinds to their own tags and
words) and `symbol` (when the row's body names one). The body itself is not served.
`last_seq` is the newest row's number; it and the rows are empty with `cause` when there is no
database yet, it cannot be read, or the journal has no rows yet.

### GET /advisor (read-only, nothing applied)

The Lab's advisor card and the Overview tag. Code: `src/tickr/api/advisor.py`; the files it
reads are defined in `src/tickr/advisor_files.py` (see `docs/advisor.md`). One entry per run
folder under `[advisor] proposals_dir`, newest first, the newest `[api] advisor_runs` of them:
`status` (`proposed`, `idea`, `no_change`, `refused`), `mode` (`live` or `backtest`), `base` and
`proposed` versions, the advisor's own texts (`summary`, `changes` with `now` and `proposed`
values as text, `expected_effect`, `risks`, `no_change_reason`, `cause`, `idea`), the hold-out
rule (`tuned_on`, `judge_on`), every `judges` result of `python -m tickr.advisor.judge`
(`verdict` and `base_verdict`: `edge`, `negative_edge`, `no_evidence`, `too_few`; `holdout`,
`base_holdout` and `fitting` SIMULATED numbers, where only the hold-out rows are evidence) and
the owner's `decision` (null = awaiting review). `awaiting_review` counts every run (not only the
listed ones) with status `proposed` or `idea` and no decision. A folder that cannot be read
exactly (no `run.json`: a run from before this file, or a malformed file) is listed under
`unreadable` with its cause (one malformed judge or decision file refuses its whole run).
`cause` is set when the folder does not exist yet or cannot be listed. POST /advisor/decision
writes the owner's verdict into the run folder as `decision.json`; a later verdict replaces it.
A decision is a note only: nothing copies, loads or deploys a strategy from it.
### GET and POST /lab/replay (simulated, never the bot)

The Lab's "replay the last days", "compare with another version" and "replay one day": a strategy
version (a folder under `strategies/`, the active one unless asked; its module from
`[observers] strategy_modules`) replayed by the backtest (`docs/backtest.md`) over the last
`[api] lab_replay_sessions` sessions of the data already fetched into `[api] lab_replay_data_dir`,
ending at its last pool day, or over one pool day; optionally a second version over the same
window. It never fetches: `summary.last_day` is where the data ends, and the app shows it. Code:
`src/tickr/api/lab_replay.py`.

POST body: `{}`, or any of `version` (folder to replay, default the active one), `compare` (a
second folder, replayed after the first) and `day` (`YYYY-MM-DD`, one of `days`: replay that
session only). It starts `python -m tickr.backtest run --last-sessions N` (`--last-sessions 1
--end-day D` for one day) as a child process of the API under `nice -n [api] lab_replay_niceness`,
writing into `[api] lab_replay_results_dir`; a comparison starts the second child when the first
ends, never both at once. The bot reads nothing of it. One run at a time, shared by both
listeners; each start deletes the previous run. It is refused (`409 conflict`, one cause) while a
run is going, during the regular session of a trading day when
`[api] lab_replay_outside_session_only = true`, without fetched data (`pool.csv`), for a day that
is not in it, for a folder that is missing, unreadable or has no module, when `compare` names the
first version again, or when the API was started without its config file path (the demo). Any
other key or a non-string value is `400 bad_request`.

- `state`: `idle` (no run yet), `running`, `done` (with `summary`) or `failed` (with `cause`:
  the backtest's own refusal, paths relative to the repo).
- `strategy`, `version`, `folder`: the first version replayed; `sessions`: the number asked for
  (1 for one day); `day`: the one day replayed, null for the last sessions.
- `compare`: null, or the second version (`folder`, `strategy`, `version`, its own `state`,
  `cause` and `summary`). It is `running` while the first one runs, and `failed` with a cause if
  the API stopped before it started. The run's `state` is `running` until both ended (the first
  version's `summary` and `cause` stay null until then); otherwise it is the first version's.
  An unreadable `current.json` reads as `failed` with that cause, and the next start replaces it.
- `versions`: every folder under `strategies/` a run may name, the active one first, then the
  observers, then the others: `folder`, `strategy` and `version` as its strategy.toml says (null
  when unreadable), `role` (`active`, `observer`, `candidate`) and `cause` (null when it can be
  replayed). `days`: the latest `[api] lab_replay_sessions` session dates of the fetched pool,
  newest first (empty without data): the days "replay one day" offers.
- `folder`, `day`, `compare`, `versions` and `days` were added after version 1 shipped: a reader
  takes a document without them as null or empty.
- `summary`: the window (`first_day` ... `last_day`, `sessions_without_replayed_days`: no gainer
  or incomplete data that day), every signal (`trades`, `win_rate_percent`, `mean_r`, `total_r`,
  `profit_factor`, the day-resampled `mean_r_interval_low`/`high` at `confidence_percent`, null
  when too few days, `max_drawdown_r`) and what the bot's risk limits would have taken
  (`bot_trades`, `bot_total_usd`, `bot_max_drawdown_usd`). Twenty sessions are few: the interval
  is wide by construction.

### GET /trades/whatif (another version on one trade, simulated)

The Trades screen's "Another strategy…": strategy folder `version` (under `strategies/`, its
module from `[observers] strategy_modules`) replayed by the replay engine (`docs/replay.md`, the
same fill and exit rules and `[api] replay_settings_file`) on `symbol`'s captured M1 bars of
`date`, with the prior sessions the bot cached for that symbol that day
(`<history_dir>/<environment>/<date>/<symbol>-<[market_data] history_sessions>.jsonl`) as its
history. Offline and read-only: nothing is fetched or written, the bot reads nothing of it.
Code: `src/tickr/api/trade_whatif.py`.

- All three query parameters are required; `symbol` is a ticker without `/`, `version` a single
  folder name. Anything else is `400 bad_request`.
- Refused (`409 conflict`) during the regular session of a trading day when
  `[api] trade_whatif_outside_session_only = true`.
- `signals`: what the simulation did with each ENTER of that day (`filled`, `missed`,
  `rejected`, `ignored`). `trades`: each simulated trade of that day (`signal_at`, `opened_at`,
  `entry_price`, `stop`, `closed_at`, `exit_price`, `exit_reason`, `result_r` per share, and the
  version's own `quantity` with `result_gross_usd` when it chose one). `result_r`,
  `result_gross_usd`: their sums; USD is null when a trade has no quantity. Gross: slippage in,
  commissions and fees out.
- When it cannot be computed (unreadable or unknown folder, no module, a version that needs more
  history than the bot caches, replay.toml `history_sessions` differing from
  `[market_data] history_sessions`, no captured bars for the symbol that day, no or empty cached
  prior sessions, a refused simulation, a version that raises) `cause` says why, `signals` and the sums are null and `trades`
  is empty. Paths in a cause are relative to the repo.

### GET /chart (read-only, market data)

Candles of any US stock for the apps' chart, read from market data by the API process itself (not
from the bot's capture). Code: `src/tickr/api/chart.py`; settings: `[chart]` in bot.toml.

- Query: exactly `symbol` (upper-case ticker, letters and digits with one optional `.` or `-`
  class suffix, e.g. `BRK.B`), `interval` and `range`. Served pairs: `1m`, `5m`, `30m`, `1h` with
  `1d` (the latest session) or `5d` (the latest five); `1d`, `1w`, `1mo` with `1m`, `3m`, `1y`
  (calendar months back from today). Anything else is `400 bad_request`.
- `chart-v1`: `{symbol, interval, range, source, cached_at, cause, bars[], api_version}`; each bar
  `{start, open, high, low, close, volume, extended}`, oldest first. `source` names the provider
  (`webull`, or the demo's own). `cached_at` is when the oldest data in the answer was read from it.
  `cause` is null, or says why `bars` is empty (no data for that symbol and range).
- Sessions: a session is the calendar's day from the pre-market open (04:00) to the extended close
  (20:00; 17:00 on half days); a 1d/5d range counts sessions whose pre-market has begun. Pre- and
  post-market bars are included and `extended: true`, decided by the session calendar; a bar the
  provider labels differently from the calendar, or one outside the day's sessions, refuses the
  answer (`503`). 5m/30m/1h candles are merged from M1, anchored at the start of each part of the
  day (04:00, the regular open, the regular close), so no candle spans a session edge; the last
  candle of a part can be shorter, and minutes without trades simply have no bar.
- Daily candles: one D bar per New York date; `1w` (from Monday) and `1mo` (from the 1st) are
  merged from them; their `start` is 00:00 New York time of the candle's first calendar day and
  `extended` is false. The range start is moved back to the start of its week or month, so the
  first candle is whole. Daily bars are forward-adjusted, minute bars are not (SDK docstring).
- Merging: open of the first bar, close of the last, highest high, lowest low, summed volume,
  exact decimals.
- Cache: a session's M1 bars are kept on disk under `<data_dir>/<[chart] cache_dir>/
  <environment>/<SYMBOL>/<date>.json` once `[chart] closed_day_settle_seconds` have passed after
  its extended close, and only if the day has bars (minute bars are unadjusted, so a settled day
  never changes). Before that, a day without bars, the session in progress, and the daily series
  while the day's session is not over are read again after `[chart] live_ttl_seconds`; after the
  session the daily series is kept until the next pre-market opens.
- Pruning: the disk cache grows by one file per symbol and settled day charted (about 100 KB each
  at most); `tickr maintain --apply` deletes day files older than `[chart] cache_max_age_days`
  (docs/operations.md). A pruned day is read from Webull again when charted.
- Budget: the chart has its own request budget, `[chart] requests_per_minute` per 60 s, never the
  bot's. When it is spent the answer is `429 rate_limited` with `Retry-After` (seconds); nothing is
  queued or retried. A 1d range costs at most one request per uncached session, a daily range one.
  With the chart on, the bot's own budget for the bars route is lowered by the same number, so the
  two processes together never pass one route limit (docs/webull-facts.md, section C).
- Off by default: `[chart] webull_enabled = false` answers every chart `503 unavailable` with that
  cause. Turned on, the API first looks for the chart probe C1: until the newest
  `python -m tickr.probes chart-client` run recorded for the environment (`summary.json`
  under `<data_dir>/<[probe] evidence_dir>/`) says `passed`, it builds no Webull client and every
  chart is `503 unavailable` with that cause; it looks once, when it starts. After that,
  `python -m tickr.api` builds its own Webull market-data client in a
  background thread once it serves (status and commands never wait for it, even while the token
  awaits confirmation): it needs the app key and secret in its environment (the launchd job and
  the container do not load them for the API today) and uses the environment's token folder; it
  never reads the account id and refuses any non-market-data route. Until the client is ready
  `/chart` answers `503` "starting"; if it cannot be built, `503` with the cause. The gate for
  turning it on is in docs/webull-facts.md, section C (C1 and C2, C1 in that environment). `tickr demo` serves its simulated world here (regular session only).
- Delayed data (`delay_minutes` not 0), a failed read or a calendar refusal is `503 unavailable`
  with the cause.

### GET /performance (read-only)

`/performance?from=<YYYY-MM-DD>&to=<YYYY-MM-DD>` (both required) summarizes the trades closed
from `from` to `to`, New York dates, both included: the same closed trades as `day-v1` (bot
database, this environment, a trade belongs to the date it closed), in close-time order.
Everything is **gross**: fill prices only, no commissions or fees, as `result_gross_usd` in
`day-v1`. It never reads the broker and is never used by the daily-loss check.

- A trade's R is `result_gross_usd / planned_loss_usd`. A trade whose `planned_loss_usd` is not
  positive has no 1 R: it is left out of **every** number below (USD ones too, so R and USD
  describe the same trades) and counted in `exclusions` (`{cause, trades}`; empty when none).
- `trades` counts the rest. `wins` have a result above 0, `losses` below 0, `scratches` exactly
  0 (the same split as `days-v1`, which counts wins and losses by the same sign; no threshold).
- `win_rate` = wins / trades, a ratio from 0 to 1. `expectancy_gross_r` and
  `expectancy_gross_usd` are the mean R and mean USD per trade. `average_win_gross_r` is the
  mean R of wins, `average_loss_gross_r` the mean R of losses (negative).
  `profit_factor_gross` = USD won / USD lost (in USD, not R; `0` when there are losses and no
  wins).
- `max_drawdown_gross_r` / `max_drawdown_gross_usd`: the largest fall of the running total
  (trade by trade, in close-time order) below its highest point so far, as a number >= 0. The
  running total starts at 0 before the first trade, so a first trade at -1 R is a 1 R drawdown.
- `result_gross_r` / `result_gross_usd`: the totals.
- `days`: one entry per New York date with counted trades, oldest first: `day`, `trades`,
  `result_gross_r`, `result_gross_usd` and `cumulative_gross_r` (the running R total at the
  end of that day, from the first trade of the range). Dates without counted trades are left
  out.

Numbers that come from a division (every R value, `win_rate`, the expectancies, the averages,
`profit_factor_gross`) are computed exactly and rounded half-even to
`[api] performance_decimal_places` only in the answer; USD sums are exact. Each nullable number
`x` has `x_cause`, `null` exactly when `x` is not: `no counted trades in the range` (every
nullable number), `no winning trades in the range` (`average_win_gross_r`),
`no losing trades in the range` (`average_loss_gross_r`, `profit_factor_gross`). Counts, totals
and drawdowns are `0` for a range without trades, never `null`.

`from` after `to`, a range longer than `[api] performance_max_range_days` days (both ends
counted), a missing, repeated, extra or malformed parameter is `400 bad_request`. An
unreadable database is `503 unavailable`, as for `/days`.

### GET /explain (read-only)

Answers "why did the bot do (or not do) this?" for one symbol, from the decision file the bot
already writes (`<data_dir>/<decisions_dir>/<environment>/<NY date>/decisions.jsonl`); it never
reads the broker and records nothing. "Today" is the New York date of the API's clock. Only
records of the active strategy count (observer records are left out); the last one written wins.
`symbol` must be a ticker in upper case, exactly as the bot records it; nothing is normalized.

`checks` are in the order the strategy evaluated them: `measured` `operator` `threshold` and
`passed`; yes/no rules have operator `"is"` with null `measured` and `threshold`.
`first_failed_check` is the rule of the first check with `passed` false, or null. `admission` is
`taken` / `refused` for an ENTER that reached admission (`refusal_cause` then holds the single
`CAUSE: detail`), `not_evaluated` for an ENTER made too late to act on, `not_applicable` for
every other verdict. The quote age the admission measured is not recorded in the decision file;
when it was the cause it appears in `refusal_cause`.

No record for the symbol today is `404 not_found` (cause names the symbol and date); a malformed
line for the symbol in the decision file is `503 unavailable` (lines of other symbols are not
parsed); a missing, repeated, extra or non-ticker parameter is
`400 bad_request`. The command line has the same read: `tickr explain SYMBOL` (exit 0 found,
1 none or unreadable).

### GET /observers, GET /decisions and GET /skipped (read-only)

All three serve decisions already in the decision file of one New York date (the same file as
`/explain`; an archived day's `decisions.jsonl.gz` is read the same way). They never read the
broker and record nothing; only `/observers` and `/skipped` simulate (each observer's day
result, below; each skipped row's what-if). `at` is the start (UTC) of the decision's M1 bar;
decisions are oldest first (bar start, then decision time). `action` is the recorded verdict,
the same enum as explain-v1 `verdict`: `IGNORE` `WATCH` `ENTER` `HOLD` `EXIT`. A
date without a decision file answers with empty lists; a last line still being written is not
read yet.

`/observers?date=` (date optional, default today in New York) lists the observers of
`[strategy] observers`, matched by the strategy `name` their `strategy.toml` declares, with
only observer records (`role` `observer`). There is one entry per (name, version) recorded that
day, with the recorded version, so records made before a version bump stay listed under their
old version. A configured observer with no records that day gets one entry with the version of
its current `strategy.toml` and an empty list. Entries are in configuration order, then by
version (numeric versions numerically). `reasons` are the rules of the checks that failed,
in the order evaluated (empty when every check passed); the full checks are in `/decisions`.
`result_gross_usd` is the observer's simulated gross day result in USD: exactly the simulation
of the offline report `python -m tickr.research observers` (every ENTER replayed alone like
`whatif`, an ENTER inside an earlier simulated trade of the same symbol skipped, slippage in,
commissions out, for the recorded quantity), run on that day's captured bars
(`bars.jsonl`, or its archived `.gz`) with `[api] replay_settings_file` and the session
calendar, rounded to its `output_decimal_places`. An observer that recorded no ENTER that day
simulates no trade: `"0"`. When it cannot be computed it is `null` and `result_cause` says why
(otherwise `result_cause` is `null`); it is never guessed or set to 0:
`no decisions recorded that day`, `no captured bars for <date>`, `capture file refused: ...`,
`replay settings unreadable: ...`, `session calendar unreadable: ...`,
`simulation refused: ...`, `N ENTER(s) could not be simulated: ...` (the decision bar is not in
the captured bars), `N simulated trade(s) have no recorded whole quantity`. Today's result is
provisional: it uses the bars captured so far, and a simulated trade still open ends at the last
bar's close. The API simulates on request and keeps the last answer in memory, keyed by the
date, whether it is today, and the size and modification time of the decision file, the capture
file, the replay settings file and the calendar files, so polling the same day re-simulates only
after one of them changed (a demo day of about 40 ENTERs takes well under a second). An answer
decided by a failure that can pass without a file changing (an OSError, a calendar or
simulation refusal) is never kept. Nothing is written to disk. An observer folder whose `strategy.toml` cannot be read
is `503 unavailable`.

`trades` (for the Lab chart "Today if each version traded") lists every ENTER of that same
simulation, so it comes from the same memo and costs no extra simulation: one
`{symbol, at, opened_at, entry_price, closed_at, exit_price, quantity, exit_reason,
result_gross_usd, result_r, cause}` per ENTER, oldest first (`at`, the decision bar start, then
symbol), SIMULATED and display only. A filled trade has `opened_at` / `entry_price` (the
simulated fill, slippage in) and `closed_at` / `exit_price`, `exit_reason` (`stop`,
`strategy_exit`, `flatten`, `end_of_data`; today an open trade ends at the last captured bar,
provisional), `result_r` per share against the simulated entry and the recorded stop, and
`quantity` with `result_gross_usd` for the recorded whole quantity; prices, USD and R are
rounded to the replay settings' `output_decimal_places`, each trade on its own, so their sum may
differ from the observer's `result_gross_usd` in the last place by one unit per trade. Without
a recorded whole quantity the trade is simulated for one share: `quantity` and
`result_gross_usd` are `null` with the cause `no recorded whole quantity: result in R only`. An
ENTER without a trade has every price, time and result `null` and a `cause` of the form
`<outcome>: <detail>`: `missed: ...`, `rejected: ...`, `not_simulated: ...` (no captured bars
for the symbol, decision bar not captured), `skipped_overlap: an earlier simulated trade of
<symbol> was still open`, `skipped_same_signal: the same signal (setup ..., trigger ...) already
made a simulated trade of <symbol> that day`; otherwise `cause` is `null`. At most `[api] observer_trades_max_rows`
are listed; `trades_truncated` is `true` when that cap left later ones out. An observer without
an ENTER that day has `trades: []`. `trades` is `null` with `trades_cause` when the simulation
could not run: the same cause as `result_cause` (no decisions, no captured bars, unreadable
inputs, simulation refused) or, for an unexpected error while listing them, `<Error>: <detail>`;
otherwise `trades_cause` is `null`. The active strategy's trades are not here: `/observers`
holds observers only, and the bot's real trades are in `day-v1`.

`/decisions?symbol=&date=` (both required) lists the active strategy's and the observers'
decisions for the symbol, `observer` true for an observer record, `strategy` its `{name,
version}`. `checks` have the explain-v1 shape (`rule` `measured` `operator` `threshold` `passed`
`note`), as recorded. `entry_price` and `stop_price` are the what-if levels of a skipped ENTER
(admission `refused`, or `not_evaluated` as for every observer ENTER): the top of the recorded
entry window (`entry_high`, the price sizing and the what-if simulation use) and the recorded
stop, each `null` when the record does not hold it, and both `null` for any other decision.
Both are rounded the way the order path rounds the prices it sends (`risk/ticks.py`
`round_to_tick`: down to the cent at or above $1, down to 0.0001 below it), so they are the
limit and stop the bot would have sent; the decision file keeps the strategy's raw prices.
`admission` and `refusal_cause` are the recorded admission, as in explain-v1 (`not_evaluated`
for every observer ENTER, `not_applicable` for every other verdict); `refusal` is the same cause
read into fields (below).

`/skipped?date=` (date optional, default today in New York) lists, for the apps' "Not taken"
list, every record of the active strategy (role `active`, as `/explain`) with verdict `ENTER`
and admission `refused` or `not_evaluated`, oldest first: `at` (bar start), `symbol`,
`strategy {name, version}`, `admission`, `refusal_cause` (the single `CAUSE: detail` of a
refusal, else `null`), `refusal` (below), the what-if `entry_price` and `stop_price` (the same
rule and rounding as `/decisions`), `checks` (explain-v1 shape), and `prices_after`,
`prices_after_cause` and `what_if` (see "Prices after, overshoot and what if"). Taken ENTERs, other verdicts
and observer records are left out. A date without a decision file answers with an empty list.
Every refused minute is its own row: consecutive refusals are not merged.

`refusal` (in `/skipped` and `/decisions`) is `null` unless admission is `refused`; then it is
`{code, message, since, until, params}`, read from the recorded `CODE: detail` at the API (the
decision file keeps the text):
- `code`: the admission cause (`risk/causes.py`, in spec order: `GATE_CLOSED` ...
  `STOP_BUDGET_UNAVAILABLE`), `ENGINE_REFUSED` when admission passed but the engine refused to
  open the trade, or `UNKNOWN` (see below). The schema lists the closed set.
- `message`: the detail as a sentence without raw timestamps. For most codes it is the recorded
  detail itself (`1 own position(s) open`); `SYMBOL_COOLDOWN`, `AFTER_LAST_ENTRY`,
  `OUTSIDE_REGULAR_SESSION` and `SESSION_TIMES_INVALID` get a fixed sentence and their times move
  to the fields below.
- `since` / `until`: wire times (UTC, milliseconds) or `null`. `SYMBOL_COOLDOWN`: `since` is when
  the losing trade closed and `until` when the cooldown ends (no entry in that symbol before it).
  `AFTER_LAST_ENTRY`: `since` is the session's last entry time, `until` is `null`. Every other
  code: both `null`.
- `params`: the values in the detail, for a translated sentence (`{}` when the code has none or
  the detail is in no known shape; then show `message`): `POSITION_LIMIT_REACHED` `{open,
  limit}`; `OWN_POSITION_OPEN` / `OWN_ORDER_WORKING` `{count}`; `BROKER_READ_STALE` and
  `QUOTE_STALE` `{age, limit}`; `CLOCK_INVALID` `{seconds}` (a broker read in the future);
  `QUOTE_TIME_INVALID` `{seconds, limit}`; `CALENDAR_UNKNOWN` `{calendar}`;
  `CALENDAR_DAY_MISMATCH` `{calendar_day, today}`; `HALF_DAY` `{day}`;
  `OUTSIDE_REGULAR_SESSION` `{open, close}`; `AFTER_LAST_ENTRY` `{time}`; `SYMBOL_COOLDOWN`
  `{symbol, until}` (`until` as `HH:MM`); `DECISION_NOT_ENTER` `{verdict}`;
  `DECISION_NOT_ACTIVE` `{strategy, version, active}`; `DECISION_INCOMPLETE` `{fields}`;
  `DECISION_INVALID` `{stop, entry_low, entry_high}`; `ORDER_PRICES_INVALID` `{limit, entry_low,
  entry_high}` or `{stop, limit}`; `QUOTE_SYMBOL_MISMATCH` `{quote_symbol, symbol}`;
  `QUOTE_INVALID` `{bid, ask}`; `PRICE_OUTSIDE_WINDOW` `{ask, entry_low, limit}`;
  `QUANTITY_NOT_WHOLE` `{quantity}`; `QUANTITY_BELOW_ONE` `{quantity, maximum}`;
  `NOTIONAL_OVER_CAP` `{notional, cap}`; `PLANNED_LOSS_OVER_LIMIT` `{planned_loss, limit}`;
  `DAILY_MONEY_UNKNOWN` `{missing}`; `DAILY_CEILING` `{room, ceiling, realized, reserved,
  min_room}` (room below the minimum) or `{realized, reserved, new, total, ceiling}` (the new
  trade would cross the ceiling); `GATE_CLOSED` `{gate, ...}` (the gate's code and params, as
  `trading_gate` above). Every other code: `{}`.
- A cause that is not exactly one of the shapes admission and the entry path write (no `: `,
  an unknown code, a detail of a timed code that does not match, or a detail of any other code
  that holds an ISO time) is `code` `UNKNOWN` with the recorded text as `message`, never a guess.

`refusal_cause` stays in both documents for older clients (version 1 never removes or renames a
key); new clients read `refusal`.

A missing, repeated, extra, malformed or non-ticker parameter is `400 bad_request`; a malformed
line for the symbol (`/decisions`), an observer line (`/observers`) or an active line
(`/skipped`, `/watching`, `/flow`) of that day is `503 unavailable` with the file, line and cause.

### GET /flow (read-only)

`/flow?date=` (date optional, default today in New York) is the Overview's live flow. Every
value is derived from what the bot already records; nothing is written, the broker is never
read. Archived (`.gz`) files are read the same way; a last line still being written today is
not read yet.

`chips`: one per symbol a screener listed that day, from the captured `screener.jsonl`, most
recently listed first (within a discovery pass: gainers, losers, top actives, each by rank).
- `sources`: every screener that listed it that day: `GAINERS` `LOSERS` `TOP_ACTIVES`.
- `ranks`: `{kind, rank}` per screener in `sources` order: its rank (1 is first) in the
  latest list of that screener that held it. Added later; older bots omit it.
- `change_pct`: `(price - pre_close) / pre_close x 100` of its latest captured provider row,
  rounded half-even to `[api] flow_change_pct_decimal_places`. `null` when the row lacks a
  number for either key or `pre_close` is not positive; `change_cause` then says which
  (otherwise `null`). The provider's own `change_ratio` is not used: its unit is unproven.
- `excluded_cause`: why the universe filter refused it in the latest pass that listed it, as
  `CAUSE: measured` (`SECURITY_TYPE_NOT_ALLOWED: ETF`,
  `SECURITY_TYPE_UNKNOWN: missing`, `CONFLICTING_ROWS: ...`); `null` when it passed. The bot
  journals only each pass's totals, so the API applies the discovery code's own `merge` and
  `universe_cause` again to every captured pass (the rows of one screener read share a receipt
  time; a pass reads gainers, losers, top actives in that order), with the allowed lists of
  `[market_data]` as the API loaded them. The active strategy's liquidity refusals at discovery
  (spread, dollar volume) and halts are not recorded per symbol and are not shown.

`stages`: always six, in this order, each `{stage, count, symbols}` (distinct symbols, sorted):
- `screeners`: every chip; `universe`: the chips with `excluded_cause` `null`;
- `strategy`: symbols with a decision of the active strategy (role `active`) that day;
- `risk`: symbols with an active `ENTER` whose admission is `taken`;
- `order`: symbols of trades of this environment opened that day whose entry order filled
  (the bot database);
- `stop`: those whose stop is `verified` (below).

`stops`: one per trade in `order`, oldest first (then by trade id), `{trade_id, symbol,
state}`. `state` is its own enum, not status-v1's stop state:
- `verified`: a broker Detail read journaled one of the trade's stop orders working after the
  first entry fill read (the same rows as `stop_confirmed_after_seconds`);
- `pending`: no such read yet, and the trade is still open and the date is today;
- `never_verified`: no such read, and the trade is closed (a rejected stop on a flattened
  trade, a stop that filled before any read saw it working) or the date is past;
- `unknown`: its journal rows cannot be read.
The live state of an open position's stop (`missing` included) is in `/status`.

`reconcile`: the bot keeps only its latest reconciliation and status, so only today has them.
`agreement` (`match` / `mismatch`), `read_at` and `mismatch` come from the account file when
its `read_at` is that day; `fences` (status-v1 shape) from the status file when it was written
that day. Otherwise each is `null` and `agreement_cause` / `fences_cause` says why (no file,
invalid file, written on another day, or a past day).

A capture file with a malformed line is `503 unavailable` (`capture file unreadable: ...`), as
is an unreadable database.

### GET /watching (read-only)

`/watching?date=` (date optional, default today in New York) is "Watching closely": from the
decision file, the symbols whose latest record of the active strategy (role `active`) that day
is `WATCH`: the strategy is waiting for its setup. A symbol whose latest verdict is `IGNORE`,
`ENTER`, `HOLD` or `EXIT` is left out. A past day shows the state at the close. Each row: `symbol`, `at` (bar start), `strategy`, `verdict`
(explain-v1 enum), `decisive_check` (explain-v1 check shape: the first failed check in the
order evaluated, the same as explain-v1 `first_failed_check`; `null` when every recorded check
passed, as for a `WATCH` made without a quote), `closeness`, `closeness_cause`,
`quote_age_seconds` and `quote_age_cause`.

Check values are rounded the same way in every document that shows checks (`/watching`,
`/explain`, `/decisions`, `/skipped`): `measured` and `threshold` go to `[api]
check_decimal_places` so the shown pair keeps the recorded verdict. A failed check rounds each
away from passing (for `>` `>=` measured down and threshold up, for `<` `<=` the reverse), a
passed one toward passing (the opposite); a value with no more decimals than that is shown as
recorded, and yes/no checks keep their nulls. `closeness` uses the recorded values.

`closeness` (0..1, rounded down to `[api] watching_closeness_decimal_places`) says how near the
decisive check is to passing:
- operator `>` or `>=` (passes once measured rises to the threshold): `measured / threshold`,
  defined for `threshold > 0` and `measured >= 0`;
- operator `<` or `<=` (passes once measured falls to the threshold):
  `threshold / measured`, defined for `measured > 0` and `threshold >= 0`.
1 is at the threshold. It is `null` and `closeness_cause` says why for a yes/no check
(operator `is`, nothing measured), an operator without a direction, values outside the domain
above, a record whose failed check already meets its threshold, or no failed check
(`every recorded check passed`).

`quote_age_seconds` is always `null` with `quote_age_cause`
`not recorded: decision records hold no quote time`: the decision file does not keep the quote
the strategy saw.

Rows are every symbol watched that day, closest first (`null` closeness last), then by symbol;
there is no cap (one row per symbol the strategy decided on that day), and `total` is the
number of rows. The apps choose how many to show (the Overview and Positions
cards show the closest three, "Show all" lists every row).

`latest_size` is the Overview's "next size · binding" line: the size the active strategy gave its
newest sized decision that day (any verdict, entering included), read from the record, never
recomputed. The strategy sizes each signal itself (owner decision 2026-11-14); the platform only
verifies that quantity on admission, so there is no size before a signal. Fields: `symbol`, `at`,
`strategy`, `verdict`, `quantity` (the measured value of the recorded `quantity` check: whole
shares, below 1 means too small to enter), and `binding`: `daily_room` only when the record
proves it: a `daily_loss_budget_usd` check (the planned loss was lowered to the room the daily
loss ceiling leaves) and floor(that room / the recorded `sizing_risk_per_share`) equal to the
quantity. The room check alone proves nothing: the notional cap can still cut below it.
Otherwise `binding` is `null` with `binding_cause`. It is the last size given, not a forecast of
the next one (that depends on the next signal's price and stop). `latest_size` is `null` when no
decision that day reached sizing.

### GET /symbols (read-only)

`/symbols?date=` (date optional, default today in New York) is the Symbols screen behind the
Overview funnel: every symbol of the day, one row each, sorted by symbol. Sources: the day's
captured `discovery.jsonl` (one line per discovery pass, written by the bot), the screener
chips of `/flow`, the active strategy's decisions and the bot database. Read incrementally and
archived (`.gz`) files the same way, like `/flow`.

`stages`: always eight, in pipeline order `screeners`, `universe`, `quote`, `bars`, `strategy`,
`risk`, `order`, `stop`, each `{stage, count}`: how many rows have `furthest` at or past it.

Each row:
- `sources`: `{kind, rank}` per screener that listed it, as the `/flow` chip's `ranks` (its
  latest rank in each list); `change_pct` as the chip's.
- `first_seen`: the start of the first discovery pass that listed it.
- `furthest`: the furthest stage it got to that day, judged or waiting there (a refusal at a
  stage got to that stage, a pass candidate and a decision `strategy`, a taken entry `risk`, a
  filled entry `order`, a verified stop `stop`); never before `stage`.
- `stage` and `state`: where it stands now: `passed` (stop verified), `waiting` (nothing
  failed, the next stage has not happened yet: a candidate awaiting the strategy, a `WATCH`,
  a taken entry awaiting its fill, a stop not yet seen working), `dropped` (refused at
  `stage`), `not_reached` (the latest pass stopped before deciding it, or no pass ran since a
  screener listed it). The newer of the latest pass's outcome and the latest decision wins; a
  decision always replaces a pass's `waiting`.
- `cause`: the discovery refusal code (`SECURITY_TYPE_NOT_ALLOWED`, `QUOTE_STALE`,
  `STRATEGY_LIQUIDITY`, ...), `IGNORE`, the admission (`refused`, `not_evaluated`), the stop
  state (`never_verified`, `unknown`), or the stopped pass's `<stage> <cause>`; `null` otherwise.
- `check`: the failed strategy check (rounded like `/watching`) when a rule dropped it;
  `detail` otherwise: `measured (needs threshold)` of a platform refusal, the admission's cause
  or the stopped pass's detail.
- `verdict`, `setup`: of its latest decision that day (`null` without one); `at`: when the
  current state was recorded.
- Added after v1 shipped (a reader takes a document without them as null or empty):
  - `price`: the price in the latest screener row that listed it (also on the `/flow` chip).
  - `reached`: every stage up to `furthest`, in pipeline order, as `{stage, at}`: when it first
    got there (`screeners` the start of the first listing pass, a discovery stage the end of the
    pass that judged it, `strategy` the pass that made it a candidate or its first decision,
    `risk` its first taken entry). `at` is null for `order` and `stop`, which come from trades.
  - `decided_at`, `strategy` (`name@version`) and `checks`: the active strategy's latest decision
    on it and every check of that decision, rounded like `check`.

`as_of` is the end of the latest pass; `null` with `as_of_cause` when none is recorded for the
day. A malformed capture line is `503 unavailable`.

### GET /movers (read-only)

`/movers?date=` (date optional, default today in New York) lists the day's fast movers: a
symbol whose latest completed one-minute bar, measured by discovery on the bars it already
reads, fired at least one signal against the mean of the previous `lookback_bars` bars. One
row per symbol (its latest such bar), newest bar first, then by symbol: `at` (bar start),
`close`, `bar_dollar_volume`, `volume_ratio`, `move_atr` (signed, in ATRs of the previous bars),
`dollar_volume_ratio`, `fired` (the signals that reached their thresholds), `spike_up` (volume
and an upward move both fired: the strategy's spike setup) and `watching` (its latest decision
of the active strategy that day is `WATCH`).

`rules` are the thresholds of the active strategy's `[spike]` section as the API loaded them;
`null` with `rules_cause` when the strategy defines none. An unreadable `[spike]` is
`503 unavailable`.

### Commands

`202` means queued, not executed. The command's result appears later in
`status.recent_commands` (matched by `command_id`) with `outcome` `done` or `refused` and a cause.
`warning` is non-null when the bot does not look alive at the time of the request (the command
is still queued; the bot refuses it if it reads it later than `[api] command_max_age_seconds`).
The server never relies on anything the client checked (Face ID on the phone is client-side
only): the remote port needs the paired token for every request, and the bot applies its own
preconditions to every command.

`command.symbol` is the ticker of a flatten of one position, otherwise `null`.

- **flatten**: every open trade, or only the trades of `symbol`, gets an exit request through
  the normal exit path. Refused when `symbol` has no open position ("no open position in
  XYZ"), while a closing order of a chosen trade was refused before (fence
  `exit-refused:<trade>`: look in the Webull app first), while a chosen trade holds the fence
  `market-closed:<trade>` ("a close of <trade> was due outside the regular session and was not
  sent ..."), and with a
  trade to close outside the regular session ("no closing order is sent now: at or after
  15:59:00 ET, ...": no MARKET order is sent from `[session] close_order_cutoff_seconds`
  before the regular close until the next open; the stops stay; close in the Webull app).
  The emergency's flatten step is refused the same way. `{"symbol": null}`, a lower-case ticker
  or any other key is `400`: a client bug never widens a one-symbol flatten to everything.
- **stop-bot**: a graceful stop, obeyed only when the bot is flat. The bot writes its stop
  marker (`<data_dir>/<[runtime] stop_marker_file>`), journals the command `done`, ends its
  loop at once (no strategy pass after it) and drains as on SIGTERM, status `stopped`, exit 0
  (run end cause `stopped (operator command <id>)`).
  Stop-bot is refused while any trade is not closed: a position open, or an entry working even
  with no fill yet. The cause is "N trade(s) open or entering; flatten first (or send
  emergency), then stop-bot": a broker stop is a DAY order and a stopped bot does not flatten
  at the session's flatten time, so a position (or an entry that fills during the drain)
  could go overnight unprotected. Nothing changes and the bot keeps running. Send emergency
  (it also cancels working entries) or flatten, wait until every trade is closed, then send
  stop-bot again.
  Checked first: stop-bot is refused while any trade has an exit requested or a closing order
  working (an operator flatten, an emergency, the session flatten), with the cause "a close is
  in progress; send stop-bot again after it completes": stopping then would leave an exit
  request that a later start would carry out without anyone asking. Commands the bot reads
  after an obeyed stop-bot, in the same pass, are refused ("read after stop-bot <id>: the bot
  is stopping"), never recorded done.
  The launchd bot job has `KeepAlive` false, so launchd does not restart it today, but its
  calendar start (`[runtime] bot_start_local` on `launch_weekdays`) would start it the next
  trading day. The marker stops that: while it exists preflight fails with cause
  `stopped-by-operator` (the bot job alerts `bot-did-not-start`, the morning preflight job
  `preflight-failed`), `tickr run` refuses (exit 3) and `tickr status` says so. To trade
  again: `tickr clear-stop`, then wait for the next scheduled start or
  `launchctl kickstart gui/$(id -u)/local.tickr.bot`. `tickr resume` and `tickr exit`
  still work while the marker exists.
- **emergency**: three steps in this order, each the existing handler and each journaled as a
  `command-step` row: entry-stop on (the same fence as `/commands/entry-stop`, survives
  restarts), cancel own orders (working entry orders only), flatten all (exit cause
  `operator-emergency`). A step that refuses does not stop the others; the command is `done`
  with a cause listing every step, or `refused` with the cause of each refused step. Cancel own
  orders never cancels a broker stop: the flatten's exit path cancels a trade's stop only right
  before it sends the closing sale for the shares no live stop still covers, and restores
  protection whenever that sale cannot be sent. Cancelling the stops in step 2 would leave the
  position unprotected until the sale fills, so it is not done. After a crash in the middle the
  bot reads the command again and runs only the steps not yet journaled; if by then it is
  older than `[api] command_max_age_seconds`, nothing more is run and the command is recorded
  `refused` with the steps done and the steps not run.

#### Start-bot

`POST /commands/start-bot` is the one command the bot does not read: a stopped bot cannot.
The API runs `[api] start_bot_command` (shipped: `launchctl kickstart gui/<uid>/local.tickr.bot`,
without `-k`, so a job that already runs is never restarted) and answers `202 bot-start-v1`
(`requested_at`, `stop_marker_cleared`) when it exits 0. The bot job then runs exactly as a
scheduled start: preflight first, which may still refuse (no session today, token, disk);
follow `GET /status` and the job log. Nothing runs inside the API process, and nothing talks
to the broker.

- Refused `409 conflict` (nothing changed) while a fresh status says the bot is `starting`,
  `running` or `stopping`. A stale or missing status proves nothing, so the start goes ahead;
  for a job that does run, the kickstart does nothing.
- Refused `409 conflict` (nothing changed) while a live process is recorded in the bot's lock
  file (`tickr.lock` under `data_dir`): a fresh `stopped` is written before the process has
  ended, and `tickr trade` holds the same lock. The kickstart would do nothing, or the job
  would end on `AlreadyRunning`, with the owner's stop already gone.
  A crashed bot leaves its pid in the file; should macOS reuse that pid for another process,
  Start answers `409` until that process ends. A lock file that cannot be read is `503`.
- The operator stop marker left by stop-bot is removed first (pressing Start is the decision
  to trade again, as `tickr clear-stop`), because preflight refuses while it exists. If the
  command then fails (`503`, cause with the exit code, the exception or the timeout
  `[api] start_bot_timeout_seconds`), the marker stays removed and the cause says so: the next
  scheduled start runs. A marker that cannot be removed is `503` with that cause, and nothing
  runs. Removing it is written to the API log (`api.log` under launchd) with the time.
- Starts run one at a time across both listeners, under their own lock, so a slow start
  command never holds back a queued command such as emergency or flatten.
- `503 unavailable` without running anything when `start_bot_command` is empty (Docker, the
  demo, which sets it empty so it can never start the real bot job).
- An `Idempotency-Key` is checked for its form only: a repeat is harmless.

Stopping stays `POST /commands/stop-bot`: the bot drains itself and is refused while any trade
is not closed, which a launchd SIGTERM would not check.

#### Idempotency-Key

Every `POST /commands/*` takes an optional `Idempotency-Key` header: 1-128 characters from
`A-Z a-z 0-9 . _ : -`, else `400 bad_request`. Keys are case-sensitive. The first request with
a key queues the command and the API remembers the key with its `202` answer for
`[api] idempotency_key_ttl_seconds` (in `<data_dir>/<commands_dir>/idempotency/`, one file per
key, named by the key's SHA-256; written atomically with fsync; expired files are deleted when a
new key is stored). Within that lifetime:

- the same key with the same command (kind, and the symbol of a flatten) answers `202` with the
  first answer, byte for byte, and queues nothing, also after the bot consumed the command and
  also when the inbox is full;
- the same key with another command answers `409 conflict` and queues nothing;
- an unreadable record of the key answers `500 internal` and queues nothing.

After its lifetime a key is new again. The lifetime must be above
`[api] command_max_age_seconds` (checked at startup), so a repeat can never queue a second copy of
a command the bot may still execute. The command file is written before the key: if the API dies
between the two, a repeat queues the command again, which every command tolerates (exit requests,
the entry-stop fence and entry cancels are idempotent; a second stop-bot only writes the marker
again).

## Bot side

- Status: after each loop step that changes what `/status` shows, the bot calls
  `contract.write_status(path, StatusV1(...))` (temp file + fsync + rename).
- Account: after each successful reconciliation the bot calls
  `contract.write_account(path, AccountV1(...))` (`runtime/account.py`), the same way. Before
  that it journals `reconcile-checked` when due (`record_reconcile`, see "Clean day").
- Commands: the bot calls `contract.read_commands(inbox, now, command_max_age_seconds)`. Each item
  is a command or a refusal (unreadable file, expired, dated in the future). For each item the bot
  journals it in one transaction, acts if it is a command, then calls `contract.retire_command`
  (moves the file to `<commands_dir>/processed/`). If the bot crashes between journal and retire,
  the file is read again: a `command_id` already in the journal is retired without acting.
  Commands older than `[api] command_max_age_seconds` are refused, never executed late.
- Emergency steps are journaled one by one (`command-step`, keyed by `command_id`) and the
  command record comes last, so a command read again after a crash finishes only its missing
  steps.
