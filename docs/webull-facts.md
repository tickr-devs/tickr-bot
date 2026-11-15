# Webull facts

What we know about Webull OpenAPI, with the source of each fact. Ported from the old `Trading` project (2026-11-14).
Sources: **SDK** = webull-openapi-python-sdk 3.0.3 source; **docs** = developer.webull.com (as read on the date given); **probe** = observed on our Sandbox account.
A fact marked UNPROVEN must not be relied on by trading code until a probe proves it.

## Hosts, credentials, auth
- Production hosts (SDK): `api.webull.com`, `data-api.webull.com`, `events-api.webull.com`. Sandbox hosts (docs): `api.sandbox.webull.com`, `data-api.sandbox.webull.com`, `events-api.sandbox.webull.com`. Ports: HTTPS 443, MQTT 1883 (TLS), gRPC 443. Probe: only allowlisted hosts were contacted.
- Credentials by environment variable NAME: Sandbox `WEBULL_SANDBOX_APP_KEY`, `WEBULL_SANDBOX_APP_SECRET`, `WEBULL_SANDBOX_ACCOUNT_ID`; Production `WEBULL_APP_KEY`, `WEBULL_APP_SECRET`, `WEBULL_ACCOUNT_ID`. Separate token directory per environment.
- Requests are HMAC-signed by the SDK. Token states PENDING / NORMAL / INVALID / EXPIRED. PENDING requires the owner to confirm in the Webull app within about 5 minutes (SDK). Sandbox reported token checking disabled; Production requires it (probe 2026-11-01).
- SDK HTTP defaults: connect 5 s, read 10 s; GETs auto-retried up to 3 times, POSTs not (SDK `core/data/retry_config.json`).

## Market data entitlement (critical)
- Sandbox market data is **15 minutes delayed by default**; if the Production account has a real-time market-data subscription, Sandbox is upgraded too (docs, overview, read 2026-11-07).
- Stocks need an OpenAPI market-data subscription: Nasdaq Basic (L1) or TotalView (L2). Owner (2026-10-16): L1 is free, L2 paid. App/desktop subscriptions do NOT carry over to OpenAPI (docs FAQ #3).
- Only one device may access L1/L2 at a time (docs FAQ #8). Stream notice 1002 = entitlement taken by another device.
- UNPROVEN: whether our Sandbox quotes are real time. Check: compare snapshot `quote_time` with the clock during market hours. Delayed quotes = the bot never enters (5 s quote-age rule).

## Rate limits
- Sandbox: 30 requests per 60 s per route (docs; probe: Detail polled about once per second returned HTTP 429 `TOO_MANY_REQUESTS` after about 33 calls).
- Token create/check: 10 per 10 s (docs).
- Production (docs, unverified): market data 60/60 s (display) or 300/60 s (non-display); place/replace/cancel 600/60 s; preview 150/10 s; Detail, open orders, history, positions 2 per 2 s; accounts 10/30 s. Docs conflict with each other in places; never assume the looser value.
- Streaming: at most 5 concurrent connections per App Key (docs). Symbols per connection: 100, cumulative (probe M3, below). Two connections giving 2 x 100: untested.
- Our request budget (`broker/budget.py`, per process, per route) mirrors these from config: Sandbox `requests_per_route_per_minute = 30`; Production 20 per minute by default and `route_limits` 2 per 2 s for Detail, open orders and positions. Each process has its own budget, so two processes on one account can together exceed a limit. The probe-day read script (docs/manual-trade.md) uses its own budget built the same way; the runbook therefore keeps it to one open-orders read per 10 s and single Detail reads while a trade command runs.

## Trading endpoints (SDK, v3)
| Purpose | Route | Notes |
|---|---|---|
| Place | POST `/trading/orders/place` | `account_id`, `new_orders[]` |
| Preview | POST `/trading/orders/preview` | same body as place |
| Replace | POST `/trading/orders/replace` | `account_id`, `modify_orders[]`; behaviour UNPROVEN (F5 below) |
| Cancel | POST `/trading/orders/cancel` | by `client_order_id` |
| Detail | GET `/trading/orders/get` | by `client_order_id` |
| Open orders | GET `/trading/orders/open-orders/list` | empty account returns `{"data": []}` (probe) |
| History | GET `/trading/orders/historical-orders/list` | `start_time` format UNPROVEN (one format got 417) |
| Accounts | GET `/trading/accounts/list` | |
| Balance | GET `/trading/assets/balances/get` | field names UNPROVEN |
| Positions | GET `/trading/assets/positions/list` | non-empty shape UNPROVEN |
| Calendar | GET `/trade/calendar` | 404 on Sandbox (probe); read from Production, read-only; does not list temporary closures (SDK) |

Not usable for US accounts: per-execution list (HK only), position details (JP only) (SDK docstrings). Fills come only as cumulative aggregates in Detail.

## Order vocabulary (SDK)
- Types: MARKET, LIMIT, STOP_LOSS, STOP_LOSS_LIMIT, TRAILING_STOP_LOSS. Sides: BUY, SELL, SHORT. TIF: DAY, GTC, IOC. Entrust type: QTY, AMOUNT. Combo types exist (OTO, OCO, OTOCO): all UNPROVEN.
- Request body accepted by Sandbox (probe 2026-10-12 and 2026-11-04, LIMIT BUY 1 share AAPL): `client_order_id, combo_type="NORMAL", entrust_type="QTY", instrument_type="EQUITY", market="US", limit_price, order_type, quantity, side, support_trading_session, symbol, time_in_force`.
- `support_trading_session`: CORE (regular), ALL (extended), NIGHT. ALL accepted in PRE and POST (probe). NIGHT UNPROVEN.
- `client_order_id`: at most 32 characters for US stock (docs 2026-11-09; an SDK docstring says 40, use 32).
- TIF by type (docs): DAY/GTC for MARKET and non-trailing stops; trailing stops DAY only.
- Statuses (SDK enum): SUBMITTED, CANCELLED, FAILED, FILLED, `"PARTIAL FILLED"` (with a space; docs write PARTIAL_FILLED). PENDING and EXPIRED are not in the enum; whether they are emitted is UNPROVEN.
- Ticks: 0.01 at or above 1 USD, 0.0001 below (SEC Rule 612). Webull's own tick validation UNPROVEN. Round BUY limits down, SELL limits up.

## Observed Sandbox behaviour (probe)
- LIMIT BUY DAY, session ALL, accepted in PRE (2026-10-12) and POST (2026-11-04); Detail showed it within about 0.2 s; cancel -> CANCELLED, filled 0. Place acknowledgement alone is not proof; Detail is.
- Sandbox follows real market hours: weekend placement -> HTTP 417 `OPENAPI_CAN_NOT_TRADING_FOR_NON_TRADING_HOURS`, no order created.
- Detail or cancel of an unknown id -> HTTP 417 `OPENAPI_PARAM_ERR` ("Order not present").
- Latency (single samples): place ack 180-210 ms, Detail 130-170 ms, cancel 180 ms, screeners 160-255 ms, snapshot 100-130 ms.
- **Never observed: a fill, a stop order, a non-empty position or open-order list.**

## Market data shapes (probe 2026-11-10)
- Screeners (`/market-data/screeners/gainers-losers/list`, `/market-data/screeners/top-actives/list`): bare JSON array of exactly 200 rows, not paginated; 20 keys: `amplitude, change, change_ratio, close, currency, currency_code, exchange_code, high, instrument_id, low, market_value, name, open, pe_ttm, pre_close, price, relative_volume_10d, symbol, turnover_rate, volume`; all values strings; no provider timestamp; `turnover` absent; `relative_volume_10d` missing in 33-68 of 200 rows. Losers = gainers route with `direction=ASC`.
- Snapshot (`/market-data/stocks/snapshots/list`, up to 100 symbols): strings except `last_trade_time`, `quote_time` (epoch ms numbers); keys include `ask, ask_size, bid, bid_size, price, volume, open, high, low, pre_close, trade_status, list_status, quote_time, last_trade_time`; `extend_hour_*` with `extend_hour_required` (sometimes missing in PRE); `trade_status` seen `ATH`, `CLS`; on weekends returns HTTP 200 with Friday's quote.
- Bars (POST `/market-data/stocks/bars/list`): 20 symbols per request (docs); count max 1200 (SDK) vs 1650 (docs), request at most 1200; shape `result[] {delay_minutes, instrument_id, symbol, result[] {time, open, high, low, close, volume, trading_session}}`; unadjusted; default RTH only. `delay_minutes` reveals delayed data.
- L1 stream: MQTT + POST `/market-data/streaming/subscribe`; protobuf, all-string fields: `Quote {basic{symbol, instrument_id, timestamp ms, trading_session}, asks[], bids[]}`; `notice` JSON 1001 (status), 1002 (entitlement pre-empted). The SDK reconnects every 10 s forever but does NOT resubscribe; the adapter must resubscribe the full set on every connect.
- Categories (SDK `webull/data/common/category.py`): US_STOCK, US_OPTION, HK_STOCK, US_ETF, HK_ETF, CN_STOCK, US_CRYPTO, US_FUTURES, US_EVENT, HK_FUTURES. Screeners take `category` (docstring: "Enum values: US_STOCK"). ETFs have their own category, but that a US_STOCK screener never lists ETFs or warrants is UNPROVEN. No screener or snapshot row states a security type (fixtures `market_data/`); the adapter looks it up in the instrument profile `sub_category` (M1 below).
- Bars request (SDK): `symbols, category, timespan (M1...), count`, optional `real_time_required` (default off: only finalized minute bars), `trading_sessions`, `start_time`/`end_time` in epoch ms. Bar `time` format: see M2 below.
- Parse all prices and quantities with `Decimal`, never float.

## M1 — security type in instrument profiles (probed 2026-11-14, Sandbox, read-only)

Route: `GET /trading/instruments/stocks/profiles/list` (SDK `list_instruments`, version v3), `category=US_STOCK`.

- `category` is `US_STOCK` for every row, including the ETF and warrants. It is not the security type.
- The security type is `sub_category`:
  - AAPL (common stock): `COMMON_STOCK`
  - SPY (ETF): `ETF` (ETF rows also carry `crypto_etf`, `single_stock_etf`, `etf_leveraged_factor`)
  - GRABW, SOUNW, OPENW (warrants): `WARRANT`
  - TSM and BABA (ADRs): `COMMON_STOCK` — Webull does not mark ADRs (ADRs are allowed by the owner, so no ADR filter is needed).
- One symbol can return more than one row: OPENW returned two `instrument_id`s (one `status=NT`, one `status=CO`). Identity must be by `instrument_id`, and a symbol with several rows must be resolved (or refused), not taken first-match.
- `status` values seen: `OC` (AAPL, SPY, TSM, BABA), `CO` (warrants), `NT` (stale OPENW row). Meaning not documented; treat as unverified.
- Warrant rows: `marginable=false`, `shortable=false`, `fractionable=false`, margin fields `1.0`; the `NT` row omits margin fields entirely (optional fields).
- `exchange_code` values seen: `NSQ`, `NYSE`, `PSE` (SPY), `NMS` (SOUNW).
- Numbers are decimal strings (`"0.2500000000"`).

Fixture: `tests/fixtures/webull/market_data/instruments_stocks_profiles_list_mixed_types.json`.

## M2 — M1 bar timestamp format (probed 2026-11-14, Sandbox, read-only)

Route: SDK `get_history_bar("AAPL","US_STOCK","M1",count=5)` → `POST /market-data/stocks/bars/list` (v3; market-data read, not trading).

- Envelope: `{"result":[{"symbol","instrument_id","delay_minutes","result":[bars]}]}`; `delay_minutes=0`.
- Bar `time`: ISO-8601 string with millisecond precision and a `+0000` offset (no colon), e.g. `"2026-11-13T20:59:00.000+0000"`. Parse with `strptime(..., "%Y-%m-%dT%H:%M:%S.%f%z")` (verified); `datetime.fromisoformat` accepts it on Python 3.14, rejects it on 3.9.
- Order: newest first.
- The label is the bar start: the last RTH bar of Friday 2026-11-13 is `20:59` UTC = 15:59 ET. Its volume (6,884,218 vs ~200k for neighbours) includes the closing auction.
- Each bar has `trading_session` (`RTH` here; `PRE`/`ATH` are the other requested values in the old bot).
- OHLC and volume are decimal strings.

Fixture: `tests/fixtures/webull/market_data/stocks_bars_list_aapl_m1.json`.

## M4 — M1 history by end_time / start_time (probed 2026-11-14, Sandbox, read-only)

Route: SDK `get_batch_history_bar(["AAPL"], "US_STOCK", "M1", ...)` → `POST /market-data/stocks/bars/list` (v3). Six requests; all returned HTTP OK, `delay_minutes=0`. Times below are America/New_York (EST from 2026-11-01, EDT before).

- **end_time is honored and exclusive.** Every request returned its newest bar at exactly `end_time − 1 min` (end 15:00 → newest bar 14:59; end 10:00 → 09:59). Bar `time` is the bar start (see M2).
- **start_time is honored, but returns one extra bar before it.** Window start 09:00, end 10:00 (2026-11-12) returned 61 bars, 08:59 … 09:59. Consistent with filtering on bar end inclusive on both sides (`start_time ≤ bar_start + 1 min ≤ end_time`). A caller wanting `[start, end)` by bar start must drop the bar labelled `start − 1 min`.
- **count is honored exactly up to 1200** (SDK doc maximum); there is no pagination key. To go further back, repeat with `end_time` = oldest returned bar time (exclusive end means no duplicate).
- **Order:** newest first.
- **Depth:** a 3-month-old window (end 2026-08-14 15:00 EDT) returned full bars; no depth limit was reached. Larger depth not tested.
- **Sessions:**
  - `trading_sessions` omitted → RTH only, 09:30–15:59 (count 1200 from 2026-11-10 10:00 reached back to 2026-11-05 09:30, skipping the weekend with no bars).
  - `["PRE","RTH","ATH"]` → PRE 04:00–09:29, RTH 09:30–15:59, ATH 16:00–19:59. No overnight bars (20:00–03:59) are returned with these values.
  - Each bar carries `trading_session`; the day boundary in a mixed response is 19:59 (prior day ATH) → 04:00 (PRE).
- **Missing minutes:** extended-hours minutes with no trades have no bar (e.g. 2026-11-12 04:44 ET absent; several 1-minute holes in 2026-11-09 ATH). RTH had no holes in the sample. Consumers must not assume a dense minute grid outside RTH.

Fixtures:
- `tests/fixtures/webull/market_data/stocks_bars_list_aapl_m1_window_pre_rth.json` — 09:00–10:00 window (PRE→RTH boundary, extra 08:59 bar); trimmed to 6 of 61 bars (full file is on the owner's Mac evidence folder).
- `tests/fixtures/webull/market_data/stocks_bars_list_aapl_m1_end_time_all_sessions_trimmed.json` — 12 representative bars of a 1200-bar all-sessions response; `_meta.trimmed` says so.
- **How Aperture uses it:** `WebullMarketData.bars_before` sends `end_time` only (never `start_time`, so the extra bar never arrives) and omits `trading_sessions` (RTH only; any other session is refused). `data.history` pages back with `end_time` = oldest bar start; its INCOMPLETE and NO_PROGRESS refusals remain as guards should the behaviour change.

## M3 — L1 QUOTE subscription capacity per stream connection (probed 2026-11-14, Sandbox, read-only, market closed)

One MQTT session; QUOTE subscribes via `POST /market-data/streaming/subscribe` (SDK `MarketDataStreaming.subscribe`), each request carrying only new symbols (never more than 100 in one request), cumulative 5 → 10 → 20 → 40 → 80 → 100 → 150.

- **The limit is 100 symbols per connection (session), cumulative**, not only per request. Steps up to 100 returned HTTP 200 with an empty body. The step to 150 sent only 50 new symbols and was refused:
  - HTTP `417`, error code `TOO_MANY_SYMBOLS_SUBSCRIPTION`, message `Maximum number of subscribe tickers:100`.
  - The refusal is a REST error on the subscribe call (SDK raises `ServerException`); no stream notice reports it. The already-subscribed 100 stayed (unsubscribe of exactly those 100 then succeeded).
- Success gives no per-symbol confirmation: an empty 200 body. A caller cannot learn from the response which symbols are live.
- Unsubscribe by explicit symbol list (≤100, `unsubscribe_all=false`) raised no error (the SDK does not surface its status on success).
- Stream `notice` messages arrive about once a minute, type `1001`, fields `drop`, `rtt`, `sent` (e.g. `{'drop': 0, 'rtt': 43, 'sent': 0, 'type': '1001'}`); no other notice types were seen.
  - **How Aperture uses it:** `StreamSession` turns each 1001 into a `StreamNotice` (every counter a whole number >= 0, else `MalformedResponse`, journaled as a stream error, stream unaffected); capture writes it to `notices.jsonl`; `python -m aperture.research latency` summarises it per hour. Units and per-interval vs cumulative counting are unproven.
- **Not measured (market closed):** whether quotes actually flow for all 100 symbols, update rates, and silent drops under load. Zero quote messages arrived in ~90 s, as expected on a Saturday. Repeat during RTH for that half.
- Not tested: the number of concurrent connections per App Key (docs claim ≤5) and whether two connections give 2 × 100.

Fixture: `tests/fixtures/webull/market_data/streaming_subscribe_capacity_ramp.json` (event log of the ramp; the universe filter excluded BK, EA, FI, MMC as ambiguous or not `COMMON_STOCK`/`OC`).
- **How Aperture uses it:** `[market_data] stream_max_symbols` (100, never above) caps one connection; `StreamSession` refuses a subscribe that would pass it before sending (`SubscriptionLimit`). A provider 417 `TOO_MANY_SYMBOLS_SUBSCRIPTION` under our cap would mean Webull counts differently (not expected since M3b; fallback only): the refused symbols are taken back out, the transport is reported down and a reconnect is requested (a fresh connection resets the count), and `SubscriptionLimit` is raised. `stream_reserved_symbols` keeps slots free of discovery candidates for the position and WATCH symbols. Observer symbols use `StreamSession.subscribe_optional` / `unsubscribe_optional`: sent only on a live, in-sync connection, never past the budget reserve, and a failure is raised without a reconnect (the provider may then hold symbols we do not count until the next connection; the 417 fallback covers that).

## M3b — unsubscribe frees slots (probed 2026-11-14, Sandbox, read-only, market closed)

One MQTT session, `MarketDataStreaming.subscribe/unsubscribe`, QUOTE, US_STOCK. Every step returned HTTP 200 with an empty body:
1. subscribe 100 symbols in **one** request (accepted on a fresh connection);
2. unsubscribe 20 of them by explicit list (`unsubscribe_all=false`);
3. subscribe 20 new symbols → **accepted** (no `417 TOO_MANY_SYMBOLS_SUBSCRIPTION`): explicit unsubscribe frees slots;
4. `unsubscribe_all=true` (symbols, category and sub_types omitted) → accepted;
5. subscribe 100 symbols not currently subscribed (92 never used on this session) → **accepted**: `unsubscribe_all` frees all 100;
6. unsubscribe those 100 explicitly → accepted.

So the 100-symbol cap is on the current subscribed set per session, not on total symbols ever subscribed. Rotation inside one connection is possible. Not re-probed: a fresh 417 at the boundary after reuse (e.g. a 101st symbol after step 3); quote flow (market closed).

Fixture: `tests/fixtures/webull/market_data/streaming_subscribe_slot_reuse.json`.
- **How Aperture uses it:** `StreamSession` counts the current desired set against `stream_max_symbols` and sends unsubscribes before subscribes, so candidates rotate on one connection. The reconnect on a provider 417 under our cap stays only as a fallback.

## C — chart bars for GET /chart (SDK reading 2026-11-15; NOT probed beyond M1)

What is established:
- Route: the same `POST /market-data/stocks/bars/list` the bot uses (SDK `get_batch_history_bar`, v3). `count` max 1200 (SDK docstring; M4 proved it for M1); `end_time` exclusive, newest first (M4).
- M1 with `trading_sessions` `PRE,RTH,ATH` returns pre-market 04:00-09:29, regular and post-market 16:00-19:59 ET, each bar labelled in `trading_session`; extended minutes without trades have no bar (M4). The SDK sends the list comma-joined (`BatchHistoricalBarsRequest.set_trading_sessions`).
- SDK `Timespan` enum (`webull/data/common/timespan.py`): S5, S15, M1, M5, M15, M30, M60, M120, M240, D, W, M, Y. The docstring says daily and longer bars are forward-adjusted, minute bars unadjusted. Only M1 has been probed; D is read by the backtest's daily fetch (`WebullHistory.daily_bars`), but no raw D body is recorded.
- Rate limits: Sandbox 30 per 60 s per route (docs, probe). Whether the count is per App Key (shared by every process using the key) or per connection/process is not documented; Production docs give "market data 60/60 s" without saying whether that is per route or for all market-data routes together.

UNPROVEN, to be answered on the owner's Mac (read-only, any time; C4 needs the regular session). The gate for `[chart] webull_enabled = true` (the same words are in the key's comment in `config/bot.toml`):
- **Hard gate: C1 and C2.** C1 must pass in the environment being enabled: a Sandbox pass does not prove Production (other host, other token rules), so Production stays off until C1 passes there. C2 decides whether the chart's slice of the bars limit is needed (it is built and on either way).
- **C3 gates daily bars only** (ranges 1m, 3m, 1y). **C4 gates trusting the newest minute and half days** (1d/5d ranges on a running or half session). **C5 is optional.**
- C1 the API process as a second Webull client: the "Two processes, one token" check below, with `python -m aperture.api` (chart on) as the second process while the bot runs, in the environment to be enabled.
- C2 whether the bars-route limit is per App Key across processes: with the bot idle, two processes each send 20 bars requests within 60 s; a 429 in either proves the limit is shared (the chart's slice is then required, as built), none proves it is not.
- C3 a raw D body for AAPL (count 30, `end_time` now): the `time` label (which UTC hour marks a New York date), whether a `trading_session` is present, whether today's partial bar is included during the session, and whether it includes extended-hours volume. Record it as `stocks_bars_list_aapl_d.json`. **Precondition for enabling:** the chart maps each D bar to `ny_date_of(time)`. If Webull labels a day at 00:00 UTC (19:00 or 20:00 ET of the day before), every daily candle would land one New York date early. Enable the chart only after this record shows the label falls on the bar's own New York date (for example 04:00/05:00 UTC, or later); otherwise the mapping must change first.
- C4 an M1 `PRE,RTH,ATH` body during the regular session with `end_time` = now: is the minute in progress ever returned (`real_time_required` off), and a half day (2026-11-27): does ATH end at 17:00 ET as the calendar says.
- C5 whether M5, M30, M60, W and M are accepted on this route (not needed: we aggregate from M1 and D ourselves; a check only).

How Aperture uses it now: `WebullMarketData.history_bars` (M1 with all sessions, or D), called only by `aperture.api.chart`, through `chart_market_data_from_config`: its own market-data-only SDK transport (never reads the account id; refuses every non-market-data route before sending) and its own request budget of `[chart] requests_per_minute` per 60 s. With `[chart] webull_enabled` the bot's own budget for the bars route is lowered by the same number, so the bot and the API together stay within one route limit. A chart refused by its budget answers 429 at once; nothing is queued or retried. Everything else (5m/30m/1h from M1, W/M from D, the extended flag from the session calendar) is computed by us. `webull_enabled` ships false: GET /chart answers 503 until the gate above is met. With it on, the API builds the chart's client in a background thread after it starts serving, so status and commands never wait for a token confirmation; GET /chart answers 503 until the client is ready, or with the cause if building it failed.

## F5 — replace (SDK reading 2026-11-14; NOT probed yet)

What the SDK 3.0.3 source says (`trade/v3/order_operation_v3.py` `replace_order`, `trade/request/v3/replace_order_request.py`, `samples/trade/trade_client_v3.py`):
- `POST /trading/orders/replace` (v3), body `{account_id, modify_orders: [...]}`, optional `client_combo_order_id`. No `category` header (unlike place).
- Each `modify_orders[]` element names the order by its EXISTING `client_order_id`; the request has no field for a new id. Samples always send `quantity` with the new price (`limit_price`; by analogy `stop_price` for a stop). After a replace the samples cancel by the same `client_order_id`.
- Docs (unverified): Production limit for place/replace/cancel 600 per 60 s; Sandbox 30 per 60 s per route.

UNPROVEN, to be answered by the Monday probe (`aperture probe-replace-stop`, docs/manual-trade.md):
- whether a replaced STOP_LOSS keeps its `client_order_id` and `order_id`, or Webull creates a new order (and which status the old one then shows: CANCELLED, a REPLACED-like status outside our vocabulary, or none);
- the success body (does it echo `client_order_id` / `order_id`?) and the error codes of a refused replace; until a refusal code is proven, every non-success answer except "order not present" is treated as OutcomeUnknown;
- whether `quantity` is required (we send the unchanged quantity) and whether Detail rows show `stop_price` under that name (read as optional; `limit_price` is the recorded analogue);
- the status sequence during a replace (a pending-replace status would map to UNKNOWN and fence entries);
- whether the stop stays working throughout (no unprotected gap).
- how long after the request the new price, or a new order, is listed in open orders (the `stop-held` release assumes Webull lists it within two reads `[orders] reconcile_interval_seconds` apart).

How Aperture uses it now: only the manual `probe-replace-stop` command. Trading code does not replace orders until this section records a probe.

## Known SDK defects (3.0.3)
- KD-1: streaming `SnapshotResult.close` is filled from `open`. Never use it.
- KD-2: tick request docstring/default disagree. Use measured limits only.
- KD-3: `TradeEventsClient` prints signature metadata to stdout. Do not construct it.
- KD-4: data and trade clients attach their own file/stream log handlers. Remove/redirect them before constructing any client.
- KD-5: `OrderStatus` in `trade/common/position_intent.py` is a misnamed enum; import by module.
- `PlaceOrderRequest` raises TypeError if `market` or `instrument_type` is missing.

## Calendar
- `/trade/calendar` works only on Production (read-only, the single allowed Production call). We keep `config/calendar/us-equity-<year>.json` with standard hours and exceptions (half days 2026-11-27 and 2026-12-24 close RTH at 13:00, POST at 17:00), cross-checked daily. Unknown or contradictory day = no trading. Next year's exceptions must be added before 1 December.

## Two processes, one token (UNPROVEN)
- UNPROVEN: the bot (`aperture run`) and the watchdog (`python -m aperture.runtime watchdog`) are two processes with the same App Key and the same token folder (`[environments.*] token_dir`), each with its own SDK client, running at the same time. In Production (token checking on) one process may create, refresh or replace the token and so invalidate or disturb the other's token, or put a new token into PENDING that waits for the owner in the Webull app. Sandbox reports token checking disabled (see above), so Sandbox cannot prove or disprove this.
- Check before Production (owner, on the Mac, Production read-only, market closed is fine): with a NORMAL token, start a read-only process that holds the SDK client and reads positions every 30 s (`aperture status`-style reads, or the bot with the trading gate closed); while it runs, start `python -m aperture.runtime watchdog` (or a one-off read in a second process) twice; then confirm (1) neither process logs a token error or a PENDING wait, (2) the token file in `token_dir` is not rewritten (compare modification time and size before and after; never print its content), (3) the first process's reads keep succeeding for 10 minutes afterwards. If any fails, give the watchdog its own token folder or App Key before Production.

## Open broker questions (to prove by probe)
F1 a Sandbox order fills, Detail shape of a fill and fees; F2 STOP_LOSS SELL accepted and visible in RTH; F3 stop triggers and fills; F4 cancel of a working stop, cancel-vs-fill race; F5 replace of a stop; F6 MARKET SELL in RTH; F6b a MARKET SELL, DAY, session CORE sent after the regular close (or before the open): rejected (which code), or accepted and queued for the next open (Aperture refuses to send one until this is proven, see `[session] close_order_cutoff_seconds`; no probe-day step sends one); F7 stops in PRE/POST, and what Webull answers to a STOP_LOSS SELL, DAY, session CORE placed outside the regular session (rejected with which code, accepted and working only from the next open, or queued): the engine keeps calling `_protect` on an open trade after its DAY stop expires at the close, so a bot running after the close (for example with a `market-closed` fence) places one; F9 non-empty positions/open-orders/balance shapes; F10 DAY expiry status; OTO entry+stop combo; real-time vs delayed Sandbox quotes; a closing SELL while a stop reserves the shares; a raw bars body and a snapshot during the regular session (M1/M2 above were probed on a closed market); L1 stream: a 417 at the boundary after slot reuse (101st symbol after a rotation); during RTH (M3 second half): a recorded quote frame (is bids[0]/asks[0] the best level, how many levels arrive), do quotes flow for all 100 symbols, notice 1002, resubscribe after a forced reconnect; how long a newly placed stop takes to be listed in open orders; how a halt or a non-tradable instrument shows in the snapshot (`trade_status`, `list_status`), the profile `status` (OC/CO/NT seen) and `GET /trade/instrument` (`trade_policy`, `forbid_reason`; never read by us yet); chart bars C1-C5 (section C above: the API as a second client of one token, whether the bars limit is per App Key, a raw D body and its date label, the minute in progress and the half-day post-market in M1, other timespans).
The Monday 2026-11-16 probe day (docs/manual-trade.md, "Probe day", step 4) lists the output that answers each of these it can.
