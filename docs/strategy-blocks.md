# Strategies from building blocks (`tickr.strategy.composed`)

Owner goal (2026-11-17): changing the strategy is a change to a `strategies/<folder>/strategy.toml`,
not to code. A strategy whose `[strategy] name` maps to `tickr.strategy.composed` in
`config/bot.toml [observers] strategy_modules` is assembled from the blocks below. Its file is
the whole rule; `strategies/ignition-v1/strategy.toml` is the first one.

A file naming a block, key or value this module does not know is refused at load (one
ValueError naming it). Adding a block is a code change made once in `strategy/composed.py`,
with its tests; every later strategy that uses it needs none.

| Section | Block | Values | What it does |
|---|---|---|---|
| `[liquidity]` | (fixed) | `max_spread_ratio`, `min_session_dollar_volume_usd`, `max_session_dollar_volume_usd` | Discovery applies them to the active strategy's candidates; `evaluate` applies them again. |
| `[[signal]]` | `minutes_after_open` | `minutes` | The signal bar started at least this long after the regular open. |
| `[[signal]]` | `new_session_high` | none | The signal bar closed above every earlier high of the session. |
| `[[signal]]` | `rise_from_low` | `window_minutes`, `min_percent` | The close is this far above the lowest low of the window. |
| `[entry]` | `marketable_window` | `window_percent` | A marketable buy while the ask is within this percent of the signal close. |
| `[stop]` | `percent_below_entry` | `percent` | The broker stop this far under the top of the entry window and under the bid. |
| `[[exit]]` | `trailing_percent` | `percent`, `bid_max_spread_ratio` | EXIT once the last close, or the bid when the spread ratio is within `bid_max_spread_ratio`, is `percent` under the highest price since the fill. The platform passes a strategy only a quote within `[risk] max_quote_age_seconds`, so an old quote never fires it. |
| `[sizing]` | (fixed) | `max_position_notional_usd`, `planned_loss_usd` | Quantity is the smaller of notional / entry top and planned loss / (entry top - stop + spread). |

All `[[signal]]` blocks must pass on the last completed M1 bar of the regular session, so a
signal can fire again after an exit (re-entry). Before them, `session_whole` requires the
platform's proof that the bars hold the whole session from the regular open
(`SymbolContext.session_open`: the bars read reached back to the open, or returned fewer bars
than asked). A missing 09:30 bar is not a refusal: minutes without trades have no bar, about
one thin stock in ten has none at 09:30 (docs/webull-facts.md), and every block counts time by
the clock, not by bars. Any `[[exit]]` block firing answers EXIT. The
platform's rules still apply on top: slots, daily loss ceiling, symbol pause after a loss,
last entry time, session flatten, a broker stop for every position.
