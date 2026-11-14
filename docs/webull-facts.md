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
- Streaming: at most 5 concurrent connections per App Key (docs). Symbols per subscribe request 100 (docs, unverified). Symbols per connection/account: UNPROVEN.

## Trading endpoints (SDK, v3)
| Purpose | Route | Notes |
|---|---|---|
| Place | POST `/trading/orders/place` | `account_id`, `new_orders[]` |
| Preview | POST `/trading/orders/preview` | same body as place |
| Replace | POST `/trading/orders/replace` | behaviour UNPROVEN |
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

## Known SDK defects (3.0.3)
- KD-1: streaming `SnapshotResult.close` is filled from `open`. Never use it.
- KD-2: tick request docstring/default disagree. Use measured limits only.
- KD-3: `TradeEventsClient` prints signature metadata to stdout. Do not construct it.
- KD-4: data and trade clients attach their own file/stream log handlers. Remove/redirect them before constructing any client.
- KD-5: `OrderStatus` in `trade/common/position_intent.py` is a misnamed enum; import by module.
- `PlaceOrderRequest` raises TypeError if `market` or `instrument_type` is missing.

## Calendar
- `/trade/calendar` works only on Production (read-only, the single allowed Production call). We keep `config/calendar/us-equity-<year>.json` with standard hours and exceptions (half days 2026-11-27 and 2026-12-24 close RTH at 13:00, POST at 17:00), cross-checked daily. Unknown or contradictory day = no trading. Next year's exceptions must be added before 1 December.

## Open broker questions (to prove by probe)
F1 a Sandbox order fills, Detail shape of a fill and fees; F2 STOP_LOSS SELL accepted and visible in RTH; F3 stop triggers and fills; F4 cancel of a working stop, cancel-vs-fill race; F5 replace of a stop; F6 MARKET SELL in RTH; F7 stops in PRE/POST; F9 non-empty positions/open-orders/balance shapes; F10 DAY expiry status; OTO entry+stop combo; real-time vs delayed Sandbox quotes; a closing SELL while a stop reserves the shares; a raw bars body and a snapshot during the regular session (M1/M2 above were probed on a closed market); M3 L1 stream: a recorded quote frame (is bids[0]/asks[0] the best level, how many levels arrive), notice 1001/1002, resubscribe after a forced reconnect.
