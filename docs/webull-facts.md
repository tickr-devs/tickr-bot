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
- Parse all prices and quantities with `Decimal`, never float.

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
F1 a Sandbox order fills, Detail shape of a fill and fees; F2 STOP_LOSS SELL accepted and visible in RTH; F3 stop triggers and fills; F4 cancel of a working stop, cancel-vs-fill race; F5 replace of a stop; F6 MARKET SELL in RTH; F7 stops in PRE/POST; F9 non-empty positions/open-orders/balance shapes; F10 DAY expiry status; OTO entry+stop combo; real-time vs delayed Sandbox quotes; a closing SELL while a stop reserves the shares.
