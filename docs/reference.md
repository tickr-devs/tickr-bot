# Reference: refusal causes and alerts

This page lists every reason the bot gives for refusing something, and every alert it can send you.
It is the one source the apps and the site will translate (English and Spanish) later.
`tests/test_reference_docs.py` fails when a code in the bot is missing here or a code here no
longer exists, so keep each table's first column to the code alone, in backticks.

How to read it:

- A **refusal** means the bot declined to do something and wrote down exactly one cause. It never
  guesses and never carries on with a default. Most refusals only stop a new entry. Protection
  (stops), exits and the end-of-day close always go on.
- "You do" says what, if anything, you need to do. "Nothing" means the bot clears it by itself
  once the condition is gone.
- "Broker" means Webull. The broker's own records are the truth; the bot's records are what it
  expects.

## Refusal causes

### Entry admission: `tickr.risk.causes.AdmissionCause`

Every new entry passes these checks in this order. The first one that fails is the single recorded
cause. Each blocks that one entry only; the bot tries again at the next signal.

| Code | What it means | You do |
| --- | --- | --- |
| `GATE_CLOSED` | The trading gate is closed: the configuration or the owner authorization does not allow trading in this environment. | Open the gate on purpose (config plus authorization file), or leave it closed. |
| `CLOCK_INVALID` | The computer clock gave no usable time, so no time rule can be checked. | Check the Mac clock and its time zone. |
| `RECONCILIATION_NOT_DONE` | The bot has not yet compared its records with the broker since it started. | Nothing; it clears after the first successful comparison. |
| `BROKER_READ_MISSING` | There is no recent read of positions and orders from the broker. | Nothing if short; if it stays, see the `broker-reads-failing` alert. |
| `BROKER_READ_STALE` | The last broker read is older than the allowed age. | Same as above. |
| `UNRESOLVED_EXPOSURE` | An order or position exists whose state is not settled yet. | Nothing; if it stays for long, see the entry fences below. |
| `FOREIGN_EXPOSURE` | The account holds a position or an order the bot did not create (for example a hand trade), in this symbol or of an unknown symbol. | Close or cancel it in the Webull app, or leave it; the bot will not trade over it. |
| `ENTRY_STOP_SET` | You (or a command) switched new entries off. | Switch entries on again with the `entry-stop-off` command when you want to. |
| `CALENDAR_UNKNOWN` | The bot cannot tell whether today is a trading day. | See the calendar causes below. |
| `CALENDAR_DAY_MISMATCH` | The calendar answered for a different day than today in New York. | Check the Mac clock and the calendar file. |
| `HALF_DAY` | Today closes early and half days are switched off in the settings. | Nothing, or allow half days in the settings. |
| `OUTSIDE_REGULAR_SESSION` | The market is not in a session in which this strategy may enter. | Nothing. |
| `BEFORE_FIRST_ENTRY` | It is too early: entries start a set number of minutes after the open. | Nothing. |
| `SESSION_TIMES_INVALID` | The entry time settings do not fit today's session. | Fix the session settings. |
| `AFTER_LAST_ENTRY` | It is too late: no new entries near the close. | Nothing. |
| `COUNT_INVALID` | An internal count (open positions or working orders) is not a valid number. | Report it; the bot refuses rather than guess. |
| `OWN_POSITION_OPEN` | The bot already holds this symbol. One trade per symbol. | Nothing. |
| `OWN_ORDER_WORKING` | An order of the bot in this symbol is still working. | Nothing. |
| `POSITION_LIMIT_REACHED` | The most open positions allowed at once is reached. | Nothing, or raise the limit in the settings. |
| `WORKING_ENTRY_LIMIT_REACHED` | The most entry orders waiting at once is reached. | Nothing. |
| `SYMBOL_COOLDOWN` | This symbol just closed at a loss; the bot waits before trying it again. | Nothing. |
| `DECISION_NOT_ENTER` | The strategy did not say "enter". | Nothing. |
| `DECISION_NOT_ACTIVE` | The decision came from a strategy that is not the active one. | Check which strategy is active. |
| `STRATEGY_NOT_EXTENDED` | This strategy may not enter outside the regular session. | Nothing, or allow it in the settings. |
| `DECISION_INCOMPLETE` | The strategy said "enter" without a price window, stop or quantity. | Report it to the strategy author; the bot will not fill gaps. |
| `DECISION_INVALID` | The strategy's numbers are not valid numbers. | Same as above. |
| `ORDER_PRICES_MISSING` | The order prices could not be worked out. | Report it. |
| `ORDER_PRICES_INVALID` | The worked-out prices do not make sense (the stop must be below the limit and above zero). | Check the strategy and the price settings. |
| `SETUP_INVALIDATED` | The setup that triggered the entry is no longer valid. | Nothing. |
| `QUOTE_MISSING` | No price quote for the symbol. | Nothing; it retries. |
| `QUOTE_SYMBOL_MISMATCH` | The quote is for a different symbol. | Report it. |
| `QUOTE_INVALID` | The quote has impossible values (for example bid above ask). | Nothing; it waits for a good quote. |
| `QUOTE_TIME_INVALID` | The quote has no usable time. | Nothing. |
| `QUOTE_STALE` | The quote is too old. | Nothing. |
| `SPREAD_TOO_WIDE` | The gap between bid and ask is wider than allowed. | Nothing. |
| `PRICE_OUTSIDE_WINDOW` | The current price left the entry price window. | Nothing. |
| `PRICE_COLLAR_UNKNOWN` | The price collar is on, but the last bar's close or the previous quote it compares the limit with is missing or too old. | Nothing; it waits for fresh prices. |
| `PRICE_COLLAR_BREACH` | The limit price is too far from the last bar's close or from the previous quote, more than the price collar allows. | Nothing, or review the collar settings. |
| `QUANTITY_BELOW_ONE` | The size works out to less than one share. | Nothing, or review the risk settings. |
| `QUANTITY_NOT_WHOLE` | The size is not a whole number of shares. | Report it. |
| `NOTIONAL_OVER_CAP` | The trade would cost more than the per-trade limit. | Nothing, or raise the limit. |
| `PLANNED_LOSS_OVER_LIMIT` | The loss if the stop is hit would be above the per-trade limit. | Nothing. |
| `DAILY_MONEY_UNKNOWN` | Today's profit and loss is not known (for example the broker fees are missing). | See the `fees-unknown` alert. |
| `DAILY_MONEY_INVALID` | A money figure for the day is not a valid amount. | Report it. |
| `DAILY_CEILING` | The most the bot may lose today is reached (or would be). | Nothing; trading resumes the next day. |
| `BUYING_POWER_UNKNOWN` | The broker did not say how much buying power there is. | Nothing; it retries. |
| `BUYING_POWER_SHORT` | There is not enough buying power for the trade. | Nothing, or add funds. |
| `ENTRY_BUDGET_UNAVAILABLE` | The broker request budget has no room for the entry order. | Nothing. |
| `STOP_BUDGET_UNAVAILABLE` | There is no request room left for the protective stops, so no entry is allowed. | Nothing. |
| `TRADABILITY_BUDGET_UNAVAILABLE` | The tradability check is on, but the broker request budget has no room for its two reads. | Nothing. |
| `TRADABILITY_UNKNOWN` | The tradability check is on, but the broker did not say clearly whether the stock can be traded now (an answer was missing, unknown or not read). | Nothing; if it stays, check that `tickr probe-tradability` has run and the settings match its answers. |
| `INSTRUMENT_HALTED` | The broker says trading in this stock is halted. | Nothing. |
| `INSTRUMENT_CLOSE_ONLY` | The broker allows only closing trades in this stock, so no new entry. | Nothing. |
| `INSTRUMENT_DENIED` | The broker does not allow trading this stock. | Nothing. |
| `INSTRUMENT_NOT_TRADABLE` | The broker's profile of this stock has a status the settings list as not tradable. | Nothing, or review that list in the settings. |
| `ENGINE_REFUSED` | The checks passed but the trading engine refused to open the trade; its own text says why. | Read the recorded text. |

### Entry fences: `tickr.engine.core.Fence`

A fence blocks all new entries until its condition is gone. It never blocks stops, cancels or exits.
Most clear by themselves; those marked "You:" need you to look first.

| Code | What it means | You do |
| --- | --- | --- |
| `startup-reconcile` | The bot has not finished comparing with the broker since it started. | Nothing. |
| `broker-read-failed` | A read of positions or open orders failed. | Nothing if short; see the `broker-reads-failing` alert. |
| `detail-budget` | The bot ran out of request room to read an order's detail. | Nothing. |
| `unknown-order` | A send had an unknown outcome; the bot reads the broker and never resends. | Nothing; see the `unknown-order-status` alert if it stays. |
| `order-missing` | The broker says an order the bot sent does not exist. | If it never ends, see "An `order-conflict`, `order-missing` or `status-unknown` that never ends" in operations. |
| `status-unknown` | The broker gave an order status the bot does not know. | Same as above. |
| `unprotected` | A position has no verified stop yet. | Nothing; the bot places the stop. See the `unprotected-position` alert. |
| `over-protected` | More stops are working than shares held. | Nothing; the bot fixes it. |
| `unprotected-timeout` | A position stayed without a verified stop too long. | You: look in the Webull app. |
| `stop-rejected` | The broker refused the stop order. | You: see the `stop-rejected` alert. |
| `exit-refused` | The broker refused a closing order. | You: see the `exit-refused` alert. |
| `cancel-unknown` | A cancel had an unknown outcome. | Nothing; the bot reads the broker. |
| `cancel-failed` | A cancel failed. | You: look in the Webull app. |
| `cancel-not-sent` | A cancel was held back by the request budget. | Nothing. |
| `resurrected` | An order the bot thought finished is working again at the broker. | You: look in the Webull app. |
| `foreign-position` | The account holds a position the bot did not open. | Close it or leave it. |
| `foreign-order` | The account has an open order the bot did not send. | Cancel it or leave it. |
| `quantity-mismatch` | The account holds more of the bot's symbol than its orders explain. | Sell or explain the extra shares in the Webull app (see "`quantity-mismatch`" in operations). |
| `shutting-down` | The bot is stopping. | Nothing. |
| `entry-stop` | You switched entries off. | Run `entry-stop-off` when ready. |
| `replace-unresolved` | A stop change was sent and its result is not confirmed. | You: look in the Webull app. |
| `stop-held` | There is no stop and no close was sent: an unseen sell order may rest at the broker. | You: look in the Webull app. |
| `exit-not-filled` | A closing order is still not filled after the allowed time. | You: see the `exit-not-filled` alert. |
| `market-closed` | A close was due while the market was shut, so no market order was sent; the stop stays. | You: close by hand if you want it closed. |
| `detail-unreadable` | An order's detail keeps failing to read, so its state is unknown. | Nothing; it clears on the next good read. |
| `watchdog-started` | The watchdog started the bot early to protect and flatten; no new entry. | You: clear it when ready. |
| `disk-low` | The data disk is nearly full. | Free disk space; see the `disk-low` alert. |
| `order-conflict` | A broker row disagrees with the order as the bot sent it. | If it never ends, see operations (same section as `order-missing`). |

### Calendar: `tickr.calendar.CalendarCause`

The bot never trades on a day it cannot prove is a trading day.

| Code | What it means | You do |
| --- | --- | --- |
| `CALENDAR_INVALID` | The calendar file is broken or inconsistent. | Fix or restore the calendar file. |
| `OUT_OF_COVERAGE` | The date is outside the years the calendar file covers. | Extend the calendar file. |
| `WEEKEND` | The day is a Saturday or Sunday. | Nothing. |
| `HOLIDAY` | The market is closed that day. | Nothing. |
| `NAIVE_TIME` | A time without a time zone was given. | Report it. |
| `INVALID_OFFSET` | A time had an unexpected time zone offset. | Report it. |

### Start-up checks: `tickr.runtime.preflight.PreflightCause`

Run before the session. Any failure stops the bot from starting and sends the `preflight-failed`
alert. See "Preflight checks" in [operations.md](operations.md#preflight-checks).

| Code | What it means | You do |
| --- | --- | --- |
| `config-invalid` | The settings file has an error. | Fix `config/bot.toml`. |
| `code-version-unknown` | The bot cannot tell which version it is. | Reinstall or update the bot. |
| `data-dir-missing` | The data folder does not exist. | Create it or fix the setting. |
| `disk-low` | Too little free disk space. | Free space. |
| `calendar-unknown` | Cannot tell if today is a trading day. | See the calendar causes. |
| `credentials-missing` | A broker key variable is not set. | Set the named variable (the message names it, never its value). |
| `alert-recipient-missing` | The variable holding the iMessage recipient is not set. | Set the named variable. |
| `alert-channel-missing` | No alert channel is working. | Fix the notification setup. |
| `token-dir-missing` | The folder for the broker token does not exist. | Create it. |
| `token-dir-open` | The token folder can be read by other users. | Restrict its permissions. |
| `token-unusable` | The broker token is missing, expired or rejected. | Renew it and confirm it in the Webull app (see the `token-needs-confirmation` alert). |
| `strategy-refused` | The chosen strategy cannot be loaded or is invalid. | Fix the strategy setting. |
| `stopped-by-operator` | A stop marker you set is present. | Remove the marker when you want the bot to run. |
| `database-other-environment` | The database holds runs of another environment. | Move the old database aside or use another data folder (see [operations.md](operations.md#refused-the-database-holds-runs-of-environment-)). |
| `database-unreadable` | The database cannot be read. | Restore it from backup. |

### Symbol screening: `tickr.data.discovery.Cause`

Why one stock was dropped when the bot looked for candidates. It affects only that symbol, only
for that pass.

| Code | What it means | You do |
| --- | --- | --- |
| `CONFLICTING_ROWS` | Two data rows for the same instrument disagree. | Nothing. |
| `EXCHANGE_NOT_ALLOWED` | The stock trades on an exchange the bot does not use. | Nothing. |
| `CURRENCY_NOT_ALLOWED` | The stock is not priced in US dollars. | Nothing. |
| `SECURITY_TYPE_UNKNOWN` | The data does not say what kind of security it is. | Nothing. |
| `SECURITY_TYPE_NOT_ALLOWED` | It is not a common stock. | Nothing. |
| `NO_QUOTE` | No quote available. | Nothing. |
| `QUOTE_STALE` | The quote is too old. | Nothing. |
| `QUOTE_FROM_FUTURE` | The quote's time is ahead of the clock. | Check the Mac clock if it keeps happening. |
| `QUOTE_INVALID` | The quote has impossible values. | Nothing. |
| `NO_BARS` | No price bars for the symbol. | Nothing. |
| `SYMBOL_REFUSED` | The symbol is not in a form the bot accepts. | Nothing. |
| `STRATEGY_LIQUIDITY` | The strategy found the stock too thinly traded. | Nothing. |

### Screening stopped early: `tickr.data.discovery.StopCause`

| Code | What it means | You do |
| --- | --- | --- |
| `budget` | The request budget ran out, so the pass ended before all symbols were judged. | Nothing. |
| `delayed data` | The data provider answered with delayed data, which the bot does not trade on. | Check the data subscription. |

### Symbol not reached: `tickr.api.contract.NotReachedCause`

The same two reasons as above, as the apps show them for a symbol that was not judged.

| Code | What it means | You do |
| --- | --- | --- |
| `REQUEST_BUDGET` | A request budget was used up before this symbol was judged. | Nothing. |
| `DELAYED_DATA` | The data provider answered with delayed data. | Check the data subscription. |

### Price history: `tickr.data.history.HistoryCause`

Why the bot could not build the earlier sessions' price history for a symbol. That symbol is not
traded that day.

| Code | What it means | You do |
| --- | --- | --- |
| `CALENDAR` | The calendar could not say which earlier days were sessions. | See the calendar causes. |
| `LOOKBACK` | There are not enough earlier sessions. | Nothing. |
| `NO_PROGRESS` | The data provider returned nothing new. | Nothing. |
| `INCOMPLETE` | The history has gaps. | Nothing. |
| `PAGE_LIMIT` | More pages were needed than allowed. | Nothing. |
| `EMPTY_SESSION` | A session that should have trading has no bars. | Nothing. |
| `OUTSIDE_SESSION` | Bars arrived outside the regular session. | Nothing. |
| `CONFLICT` | Cached and fresh data disagree. | Clear the cache if it repeats. |
| `SYMBOL_REFUSED` | The symbol is not in a form the bot accepts. | Nothing. |
| `CACHE` | The history cache is unreadable. | Clear the cache. |
| `INVALID_REQUEST` | The request was malformed. | Report it. |

### Settings changes: `tickr.settings_schema.Cause`

Why a settings change from the app or the site was declined. Nothing was changed.

| Code | What it means | You do |
| --- | --- | --- |
| `UNKNOWN_KEY` | There is no such setting. | Pick a listed setting. |
| `WRONG_TYPE` | The value is of the wrong kind (for example text instead of a number). | Enter a value of the right kind. |
| `BELOW_MINIMUM` | The value is under the smallest allowed. | Enter a larger value. |
| `ABOVE_MAXIMUM` | The value is over the largest allowed. | Enter a smaller value. |
| `OFF_STEP` | The value is not one of the allowed steps. | Use a multiple of the step. |
| `RULE_BROKEN` | The value breaks a rule that links it to another setting. | Change the related setting too. |
| `VALUE_MISSING` | A required value was not given. | Enter a value. |

## Alerts

Every alert goes to a Mac notification, to iMessage and to the alerts feed `data/alerts.jsonl`
(the kind is the title). The same kind and text inside the repeat window is shown once. See
[Alerts in operations.md](operations.md#alerts).

Urgency: **Now** means act as soon as you see it (money or an unprotected position may be at
risk). **Today** means look before the next session. **Info** means no action is needed.

### Alert kinds: `tickr.runtime.alerts.AlertKind`

| Code | Urgency | What it means and what you do |
| --- | --- | --- |
| `unprotected-position` | Now | A position has no stop at the broker. The bot tries to place one. If it stays, open the Webull app and protect or close the position. |
| `stop-rejected` | Now | The broker refused the protective stop. Entries are blocked. Look in the Webull app and close the position or set a stop by hand. |
| `exit-refused` | Now | The broker refused a closing order; the stop is back in place. Look in the Webull app and close by hand if needed. |
| `unknown-order-status` | Now | An order's outcome is unknown; the bot never resends. See [operations.md](operations.md#unknown-order-status-on-a-closing-order-or-stop). |
| `foreign-exposure` | Today | A position or order the bot did not create is in the account. Entries are blocked in that symbol. Close or cancel it, or leave it. |
| `broker-reads-failing` | Today | Positions or orders cannot be read from the broker. See [operations.md](operations.md#broker-reads-failing). |
| `stream-lost-with-position` | Now | The live data stream dropped while a position is open. The broker-side stop still protects it; check the position in the Webull app. |
| `daily-ceiling-reached` | Info | The day's loss limit is reached; no more entries today. Trading resumes tomorrow. |
| `bot-crashed` | Now | The bot stopped unexpectedly. Check any open position in the Webull app, then restart the bot. |
| `bot-did-not-start` | Today | The scheduled start failed. Read the job log, fix the cause and start the bot. |
| `bot-did-not-stop` | Today | The bot was told to stop and is still running. Check the job log and stop it safely (see "Stopping the bot safely" in [operations.md](operations.md#stopping-the-bot-safely)). |
| `preflight-failed` | Today | A start-up check failed, so the bot did not start. See the start-up causes above and [operations.md](operations.md#preflight-checks). |
| `token-needs-confirmation` | Today | The broker token needs your confirmation in the Webull app before the bot can trade. Confirm it there. |
| `observers-stopped` | Info | The side recorders were switched off for this run. Trading goes on. |
| `not-flat-after-flatten` | Now | After the end-of-day close the bot still sees a position or order. See [operations.md](operations.md#not-flat-after-flatten). |
| `watchdog-not-flat` | Now | The watchdog sees a position or open order after the close. See [operations.md](operations.md#watchdog-not-flat). |
| `watchdog-cannot-check` | Today | The watchdog could not tell whether the account is flat. See [operations.md](operations.md#watchdog-cannot-check). |
| `bot-down-with-open-trade` | Now | The bot is not running during the session while a trade is open. See [operations.md](operations.md#bot-down-with-open-trade). |
| `bot-down` | Today | The bot is not running (or is hung) without a clean stop. See [operations.md](operations.md#bot-down). |
| `disk-low` | Today | The data disk is nearly full. See [operations.md](operations.md#disk-low). |
| `watchdog-restarted-bot` | Info | Near the close the watchdog started the bot so it would flatten. See [operations.md](operations.md#one-restart-a-day-watchdog-restarted-bot). |
| `watchdog-restart-refused` | Today | The watchdog wanted to restart the bot but may not (stop marker, unreadable record, unknown day). Start it by hand if you want it to flatten. See [Watchdog](operations.md#watchdog). |
| `watchdog-restart-failed` | Now | The restart failed or the account is still not flat. The loudest alert: act by hand now in the Webull app. See [Watchdog](operations.md#watchdog). |
| `exit-not-filled` | Now | A closing order is still not filled after the allowed time. See [operations.md](operations.md#exit-not-filled). |
| `flatten-after-close` | Now | A close was due while the market was shut; no order was sent. See [operations.md](operations.md#flatten-after-close). |
| `fees-unknown` | Today | An order's broker fees are not known, so today's money is unknown and entries are refused. Wait for the fees to appear; if they do not, report it. |
| `fees-corrected` | Info | The account's cash activity showed the recorded fees were wrong, and they were replaced. |
| `api-credentials-unexpected` | Today | The local API holds a credential variable it must not have. See [operations.md](operations.md#the-api-job-and-the-webull-keys). |
