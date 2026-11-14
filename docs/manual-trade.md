# Manual protected trade (stage 3)

Run on the owner's Mac, in market hours, in Sandbox (`[run] environment` in `config/bot.toml`).
Credentials are read from the environment variables named in the active `[environments.*]` table.

## One-time setup (fresh clone)

```
gh repo clone valentyn-dymaretskyi/Aperture && cd Aperture
python3.12 -m venv .venv && source .venv/bin/activate     # Python 3.12 or newer
pip install -e ".[webull,dev]"
mkdir -p data/webull-token-sandbox                         # [run] data_dir / token_dir
```

Every command below runs from the repository root: `--config` defaults to the relative path
`config/bot.toml`. In each new terminal, activate the venv and export the three Sandbox variables
(names from `[environments.sandbox]`; values from the Webull developer portal, never committed):

```
source .venv/bin/activate
export WEBULL_SANDBOX_APP_KEY=...
export WEBULL_SANDBOX_APP_SECRET=...
export WEBULL_SANDBOX_ACCOUNT_ID=...
aperture check-config        # must print "trading gate: open"
```

The first command that talks to Webull may wait up to `token_check_duration_seconds` (300 s) for
you to approve the new token in the Webull app. When it waits, it prints
`waiting: approve the API token in the Webull app within 300 s` on stderr first. The token is
then kept in `data/webull-token-sandbox/`. A token Webull reports INVALID or EXPIRED, or one not
approved in time, stops the command with `token initialisation failed: <cause>`.

This manual path sends no notifications: everything it has to tell you is printed to the
terminal (errors and "needs the operator" lines on stderr). Keep the terminal visible.

## Commands

```
aperture check-config
aperture trade SYMBOL --limit 12.34            # quantity and stop default from [probe]
aperture trade SYMBOL --limit 12.34 --stop 12.10
aperture check-quote SYMBOL                     # read-only: is the snapshot quote fresh?
```

Stop price: without `--stop`, the planned-loss check uses the worst case (2 % below the
limit), and the stop actually placed is `stop_percent_below_fill` below the real average fill
(owner decision 2026-11-14), rounded down to the cent.

What `trade` does: refuses outside the regular session, outside `[probe] window_start..window_end`
or after the last-entry time; refuses a price above `max_price_usd` or a planned loss above
`max_loss_usd`. Then: LIMIT BUY (DAY, core session) -> on every observed fill a SELL STOP_LOSS for
exactly that quantity -> once the stop is seen working, hold `hold_seconds` -> cancel the stop,
confirm it is cancelled -> MARKET SELL exactly the held quantity -> closed when the broker is flat.
An entry that does not fill within `entry_wait_seconds` is cancelled.

Watch in the Webull app during the run:

1. Right after the fill: one working SELL stop for the filled quantity at the printed stop price.
2. After the close: no position in the symbol and no working orders left.
3. If you see a position without a working stop for more than a few seconds, close it in the
   app and run `aperture status`; the bot fences entries and will not touch it again.

Crash test: do the `kill -9` only while the stop is visible as working in the app, then run
`aperture resume`. It must find the same trade, keep the stop and close at the hold time or at
the flatten time.

If a closing order is refused, the bot puts the stop back, stops, and prints what to do
(`aperture clear-fence exit-refused:<trade>` after looking, then `aperture exit`).

`resume` and `exit` use today's flatten time from the calendar; after the close, on a
non-trading day, or if the calendar cannot be read, they close open trades right away.

Ctrl-C once: close through the normal exit path. Ctrl-C twice, closing the terminal, or a
SIGTERM: the command stops and the stop stays resting at the broker, with the position still
open. Run `aperture resume` (keeps protecting and closes at the flatten time) or `aperture exit`
(closes now) afterwards; the position never closes by itself while no command runs, except by
its stop.

After a crash or `kill -9`:

```
aperture resume      # recover, reconcile, keep protecting; ends when no trade is open
aperture exit        # recover, then close every open trade through the normal exit path
aperture status
```

Entries stay blocked while any fence is set (printed at the end of every command). Fences that
need a look (`stop-rejected:*`, `exit-refused:*`) are cleared with
`aperture clear-fence NAME`; all others clear themselves.

## Probe F5: replace the stop (Monday 2026-11-16)

Goal: learn how Webull replaces the price of a working STOP_LOSS (docs/webull-facts.md, F5),
so the trailing stop can be built. One replace, tightening only, on the bot's own trade.

Step 0 before any probe on Monday: right after the open, run

```
aperture check-quote SYMBOL      # a liquid symbol, e.g. the one you plan to trade
```

It reads one snapshot quote through the same path as `probe-replace-stop` and sends no
orders. It prints bid, ask, provider time, age and the limit (`[risk] max_quote_age_seconds`).
- `OK: quote is N s old` (exit 0): go ahead.
- `STALE: ...` (exit 3), or `refused: quote read failed` / `no quote` (exit 3): Sandbox quotes
  may be delayed, and `probe-replace-stop` would refuse every time. Run it once more a minute
  later on another liquid symbol; if it still says STALE, do no probe that day and send us the
  output.

1. Terminal A: `aperture trade SYMBOL --limit PRICE` as usual. Wait until the app shows the
   working SELL stop and the terminal is in the hold (`hold_seconds`, 60 s, from the moment
   the stop is seen working: step 2 must happen inside it).
2. Terminal B: `aperture status` (note the pid), then `kill -TERM <pid>`. The trade command
   stops; the position and its stop stay at the broker. Do not use Ctrl-C (the first one asks
   for an exit).
3. Look up the bid in the app. Pick a new stop above the current stop, below the bid, at most
   `[probe] replace_max_move_usd` (0.50) above the current stop, whole cents.
4. Terminal A: `aperture probe-replace-stop --to NEW_STOP`.
   It refuses (exit 3, nothing sent) unless: gate open, inside the probe window, exactly one
   open trade with no exit requested, no fence, the broker position equals our records, one
   verified working unfilled stop, a quote no older than `max_quote_age_seconds`, and the
   price rules above. Then it journals the intent and sets the operator fence
   `replace-unresolved:<stop id>`, sends ONE replace, polls Detail for
   `replace_observe_seconds`, reads open orders, and prints all of it.
5. Write down (screenshot the terminal and the app's order list):
   - the replace answer line (success / unknown / not present, echoed ids);
   - every `Detail:` line: client_order_id, order_id, status, stop_price (or "not shown");
   - every `open order:` line;
   - the `F5 result:` line and what the app shows for the stop (same order edited, or a new
     order; price; still working throughout?).
6. Close: `aperture exit`. Check in the app: no position, no working orders.

Results:
- `F5 result: confirmed` (exit 0): same client_order_id, working at the new price. Fence
  cleared; our record carries the new price.
- `F5 result: adopted` (exit 4): Webull made a NEW order at the new price and the old one ended.
  The bot now tracks the new order as the trade's stop, so `aperture exit` cancels it. Check the
  app shows exactly one working stop, then `aperture clear-fence replace-unresolved:<stop id>`.
- `F5 result: not-sent` (exit 3): refused before or by the broker; nothing changed.
- `F5 result: unresolved` (exit 4), including an unknown outcome: the command never resends.
  Do NOT run another replace. Look in the app: count the working SELL stops in the symbol.
  - Exactly one, ours, any price: `aperture exit` closes normally (it cancels our stop id).
  - None: the position is unprotected; run `aperture exit` at once.
  - A stop with an id that is not ours (the bot then reports `foreign-order:<id>`), or more
    than one: cancel the extra stops in the app and close the position there, then
    `aperture status`; send the screenshots before running anything else.
  Clear `replace-unresolved:<stop id>` only after the app shows a clean state.

  What the bot does meanwhile (`aperture exit` or `resume` running): while
  `replace-unresolved:<stop id>` is set and the bot would place a stop or send a close, it
  reads open orders itself, at most once per `reconcile_interval_seconds`. It places no stop
  and sends no close until two such reads in a row show no working SELL in the symbol that is
  not ours (Webull may list a new order a moment late); a failed read holds too. The fence
  `stop-held:<trade>` says so once, with the order id when one is seen ("stop and close held:
  untracked SELL <id> rests in ABC after an unresolved replace; resolve it in the Webull
  app"); the screen may otherwise only say "exiting". Once you cancel that order in the app,
  two clean reads (about 10-20 s) release the hold and, because the exit is already requested
  (by `aperture exit`, or by the unprotected-too-long rule), the bot sells the position at
  MARKET. It does not place a new stop first. In the exit path a release is good for one pass
  only: after it the bot cancels our stop and then holds again for two reads (about 10 to
  20 s) with no stop of ours before the MARKET close. This is expected and does not mean the
  bot is stuck.
