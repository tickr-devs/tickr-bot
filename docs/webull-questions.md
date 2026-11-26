# Questions for Webull OpenAPI support

For the owner to paste to Webull OpenAPI support (backlog M5, with the open unknowns of M3, T1, T8 and X4).
Each question is complete on its own. "Facts" says what `docs/webull-facts.md` already holds, so the owner
can see what we know and what we are asking to confirm. No account id, key or personal detail is here.
Questions the facts file answers by probe are left out (for example: stops outside the regular session are
refused, F7; a Sandbox `/trade/calendar` answers 404, Calendar section).

## 1. Sandbox calendar endpoint
- **Question:** Is there a documented trading-calendar endpoint that works in the Sandbox environment? `GET /trade/calendar` answers 404 on `api.sandbox.webull.com` and works only on Production.
- **Why:** The bot must not call Production just to learn a trading day; today it keeps its own calendar files.
- **Facts:** "Trading endpoints" table (Calendar row) and the "Calendar" section: 404 on Sandbox, Production only, temporary closures not listed.

## 2. Real-time entitlement for Sandbox L1
- **Question:** Which market-data entitlement makes Sandbox quotes real time rather than 15 minutes delayed: a Nasdaq Basic (L1) OpenAPI subscription on the Production account, or something else? How long after subscribing does the Sandbox upgrade apply?
- **Why:** The bot refuses any quote older than 5 s, so delayed quotes mean it never enters a trade.
- **Facts:** "Market data entitlement": delayed by default, upgraded if Production has a real-time subscription (docs), L1 is free for OpenAPI, app subscriptions do not carry over. Whether our Sandbox quotes are real time is UNPROVEN.

## 3. Replace-order behaviour
- **Question:** For `POST /trading/orders/replace` on a STOP_LOSS order: (a) does the order keep its `client_order_id` and `order_id`, or is a new order created and the old one given a status (which)? (b) What does the success body contain? (c) What error codes does a refused replace return? (d) Is `quantity` required? (e) Does the stop stay working during the replace, with no unprotected gap, and is there a pending-replace status? (f) How soon is the new price listed in open orders?
- **Why:** Moving a stop (trailing, breakeven) must never leave a position without its broker-side stop or send an order twice.
- **Facts:** "F5 - replace": SDK reading only, not probed; the request names the order by its existing `client_order_id` and has no field for a new id.

## 4. Halts, LULD pauses and non-tradable instruments
- **Question:** How is a trading halt or a limit-up/limit-down pause shown in the snapshot (`trade_status`, `list_status`), in the instrument profile (`status`) and on the L1 stream? What do `trade_policy` and `forbid_reason` (CLOSE_ONLY, DENIED, HALTED) on `GET /trade/instrument` mean exactly, and how fast do they change? Can a resting SELL stop be placed or kept while a symbol is halted?
- **Why:** The bot must refuse entries in a halted stock, never block exits, and know what happens to a protected position during a halt.
- **Facts:** Only the profile `status` values OC, CO and NT have been seen (M1); `trade_policy`/`forbid_reason` are never read by us. The facts list halt display as an open probe.

## 5. Buying power for extended-hours buys
- **Question:** Which buying power field pays for a pre-market or post-market BUY (`day_buying_power`, `overnight_buying_power` or `night_trading_buying_power`) on a cash or margin account?
- **Why:** Admission checks `day_buying_power` before every entry and must not send an order the account cannot pay for.
- **Facts:** "Account and balance": UNPROVEN.

## 6. gRPC trade events
- **Question:** Is the gRPC trade-events subscription (`events-api`, port 443) supported in the Sandbox? Which events does it carry (placed, filled, partially filled, cancelled, rejected), how soon after the order service does each arrive, are events replayed after a reconnect, and how many connections are allowed per App Key?
- **Why:** Fill and order updates are polled today; events could be faster, but we only use them if they are reliable and we can still verify by reading the broker.
- **Facts:** "Hosts, credentials, auth" lists the host and gRPC port 443 only; nothing was probed.

## 7. Fees on an order
- **Question:** When are `fees[]` on an order final: at the first fill, at order completion, or after settlement? Why does a working, unfilled order already list a fee? Does an order filled in several executions appear as one cash-activity row or one row per execution?
- **Why:** The daily loss rule counts the broker's fees, so we need to know when a fee can still change.
- **Facts:** "Sub-dollar prices..." section: both points UNPROVEN; the cash activities are treated as the truth.

## 8. Request limits in Production
- **Question:** Are the Production limits per App Key, per route or per account? The docs give "market data 60/60 s (display) or 300/60 s (non-display)" and "Detail, open orders, history, positions 2 per 2 s" in places that conflict. Which value applies to the bars route and to order Detail?
- **Why:** The request budget is a hard cap in config; we must never assume the looser value.
- **Facts:** "Rate limits": Production values are docs-only and unverified. In the Sandbox the bars limit is shared per App Key across processes (C2, probed 2026-11-16).

## 9. Two processes, one token
- **Question:** Is it supported to run two processes with the same App Key and token folder at the same time (a trading bot and a separate watchdog), each with its own SDK client? Can one process's token create or refresh invalidate the other's, or put a new token into PENDING?
- **Why:** The watchdog must be able to read and cancel while the bot is down, without touching the bot's token.
- **Facts:** "Two processes, one token (UNPROVEN)": Sandbox has token checking disabled, so it cannot show this.

## 10. Streaming symbol capacity
- **Question:** Do QUOTE and TICK each have their own 100-symbol limit per connection (200 different symbols on one connection), or do they share 100? Do two connections give 2 x 100? Is a stream notice 1002 raised only when another device takes the entitlement?
- **Why:** It decides how many symbols the bot can watch with a single entitlement and when it may open a second connection.
- **Facts:** "Rate limits" (two connections untested), "K - TICK" (K2: 100 per connection for TICK; a QUOTE for a symbol already held for TICK was accepted; QUOTE for a new symbol not tried), "Market data entitlement" (notice 1002).

## 11. Symbols refused as nonexistent
- **Question:** Why does the snapshot and bars route answer HTTP 417 `INVALID_SYMBOL` ("The symbols does not exist in the category") for a symbol the screener and the instrument profile both list (seen with DJT, status OC, category US_STOCK)? Is it a coverage gap, and is there a field that tells us beforehand?
- **Why:** One such symbol fails the whole request, so discovery has to split chunks to find it.
- **Facts:** "M5 - a screener symbol the snapshot and bars routes refuse": cause on the provider side, unknown; the bot works around it by splitting.

## 12. Trailing stop on Detail
- **Question:** Does Detail for a TRAILING_STOP_LOSS order show the current trailed stop price or only the initial one? Is the 1 % minimum PERCENTAGE step a documented limit?
- **Why:** The bot must know the real protection level of a position at any moment.
- **Facts:** "Open broker questions" (probe T): the step is a ratio, 1 % is the smallest accepted, 0.5 % is refused with `OPENAPI_PARAM_ERR`; T3 (Detail shows the initial level, trail unproven) is open.
