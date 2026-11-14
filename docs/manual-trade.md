# Manual protected trade (stage 3)

Run on the owner's Mac, in market hours, in Sandbox (`[run] environment` in `config/bot.toml`).
Credentials are read from the environment variables named in the active `[environments.*]` table.

```
aperture check-config
aperture trade SYMBOL --limit 12.34            # quantity and stop default from [probe]
aperture trade SYMBOL --limit 12.34 --stop 12.10
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

Ctrl-C once: close through the normal exit path. Ctrl-C twice: stop the command and leave the
stop resting at the broker.

After a crash or `kill -9`:

```
aperture resume      # recover, reconcile, keep protecting; ends when no trade is open
aperture exit        # recover, then close every open trade through the normal exit path
aperture status
```

Entries stay blocked while any fence is set (printed at the end of every command). Fences that
need a look (`stop-rejected:*`, `exit-refused:*`) are cleared with
`aperture clear-fence NAME`; all others clear themselves.
