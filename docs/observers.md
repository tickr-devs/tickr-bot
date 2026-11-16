# Observer strategies (shadow versions, never trade)

An observer is another strategy version that sees exactly what the active strategy sees and
whose decisions are only recorded. It cannot place, cancel or size an order and never reaches
the safety layer. Use it to compare a candidate version against the active one on live days;
the daily report then shows both side by side (`python -m tickr.research observers`).

## Configure

`config/bot.toml`:

```
[strategy]
active = "price-action-v1"
observers = ["price-action-v1-lookback8"]      # folders under strategies/

[observers]
strategy_modules = ["price-action-v1=tickr.strategy.price_action_v1", "price-action-v2=tickr.strategy.price_action_v2"]
max_observers = 4
max_evaluation_seconds = 0.5
decisions_dir = "decisions"
```

Each observer folder holds a full `strategy.toml`. Its `[strategy] name` picks the module from
`strategy_modules`; its `version` makes the identity. A value variant of the active strategy is
a new folder with the same name and a new version (owner rule: changing a value means a new
version). Every identity (name + version) must be distinct, the active one included. Setup
problems are refused with one cause (`ObserverError`): missing folder, unknown module, values
refused by the strategy, duplicate identity, more than `max_observers`.

## Shipped observers: relative-volume variants of price-action-v3

`config/bot.toml` ships with two observers, `strategies/price-action-v3-relvol-1.5` and
`price-action-v3-relvol-1.75`: full copies of the active `price-action-v3` with only
`[indicators] minimum_relative_volume` lowered from 2.0 (and their own `version`), so the apps
show live, on the same data, what a looser relative-volume filter would have done (owner
2026-11-16; most active decisions are refused by that check). `tests/observers` checks that each
differs from the active strategy in that value and the version only. When the active strategy
changes, these folders must follow it or be replaced.

## Candidate: price-action-v2

`strategies/price-action-v2/` is price-action-v1 (same values) plus two backlog rules, in
`src/tickr/strategy/price_action_v2.py`. New rules need new code, so it is a new strategy
name with its own module (already mapped in `strategy_modules`), not a value variant of v1.
It is not active and not an observer by default.

- **Trigger confirmation** (`[confirmation] confirmation_quotes`): v1's ENTER stands only after
  that many consecutive minute evaluations of the symbol, in the same session, had an
  executable ask through the trigger (inside the price window); before that the decision is
  WATCH with `trigger_confirmation` as its cause. A quote not through, or no quote, resets.
- **No-progress exit** (`[no_progress] minimum_progress_r` = X, `window_minutes` = N): with a
  position, EXIT once N minutes have passed since the first bar seen with it and the best high
  since then is below entry + X R (R = entry - stop); otherwise HOLD. Observers never see a
  position, so as an observer only the confirmation rule shows; replay and `research decide`
  simulate both.

X, N and the count are provisional (the owner never chose them, old Q43). The counts live in
the strategy instance's memory and restart at zero after a bot restart.

Enable as an observer: `observers = ["price-action-v2"]` in `config/bot.toml [strategy]`.
Compare offline without enabling anything:

```
python -m tickr.replay --bars <days> --strategy tickr.strategy.price_action_v2 \
  --values strategies/price-action-v2/strategy.toml --out out/replay-v2
python -m tickr.research decide --bars <days> --strategy tickr.strategy.price_action_v2 \
  --values strategies/price-action-v2/strategy.toml --out out/v2
python -m tickr.research diff --baseline <v1 decisions.jsonl> \
  --candidate out/v2/decisions.jsonl --settings config/research.toml --out out/diff
```

To change a value, copy the folder and bump `version`. Making it the active strategy is an
owner decision (`[strategy] active`), not something this candidate does.

## Candidate: volume-shock-v1

`strategies/volume-shock-v1/` with `src/tickr/strategy/volume_shock_v1.py` (shared entry and
sizing in `strategy/simple.py`). It is family L4 of `docs/strategy-ledger.md`: a volume shock in
a stock trading a little above its previous close; the module docstring and the strategy.toml
hold the rules and values. The first signal of the session stays valid for `signal_valid_minutes` unless a later bar
trades below its low; the stop is at least `minimum_stop_percent` below the entry window's
floor and the bid (the bar's low only when lower), so
results are not directly comparable with the history test, which used the bar's low. The
session flatten is the exit. A half day among the prior sessions refuses afternoon bars
(`prior_window_complete`) until it leaves the window. On history it was positive only on days that later became gainers, which is look-ahead,
so it runs as an observer to test the idea where nothing can look ahead. `simple.py` sizes and
sets stops, so a strategy built on it needs the trading-core review before it becomes active.
Enable: `observers = ["volume-shock-v1"]`.

## Candidate: noise-band-v1

`strategies/noise-band-v1/` with `src/tickr/strategy/noise_band_v1.py` (entry and sizing in
`strategy/simple.py`). It is family L2 of `docs/strategy-ledger.md`: the first close between
10:00 and 15:00 that is further above the session open than the stock's usual move at that
minute over the prior sessions; the docstring and the strategy.toml hold the rules and values.
The first signal of the session stays valid for `signal_valid_minutes` unless a later bar
trades below its stop level; the stop is the lower of the band level (or VWAP when it is
higher and below the close) and the minimum distance below the entry window's floor and the
bid.
Since the first crossing closes barely above the band, the fixed minimum distance is almost
always the stop used, so forward results do not test a band stop. The session flatten is the
exit. Like volume-shock-v1 it was positive on history only through look-ahead, so it runs
as an observer. Enable: `observers = ["noise-band-v1"]`.

Notes for both candidates (from the trading-core review of `strategy/simple.py`):

- An observer gets no daily loss budget (it sizes as if it traded alone) and no quote whose
  times entry admission would refuse: older than `[risk] max_quote_age_seconds`, more than
  `max_clock_skew_seconds` ahead, received after the evaluation, or naive
  (`observers/runner.freeze`).
- The stop is at least `minimum_stop_percent` below both the window floor and the bid.
- With the shipped values the notional cap usually sets the quantity, so the planned loss is
  about 37 to 50 USD, not a constant 50: compare results in R or per share. The platform's
  tick rounding of the stop can refuse an entry whose loss-based size binds (fails closed).
- Both refuse a session whose bars do not start at 09:30 (`session_from_open`), which holds
  while `[market_data] bars_per_symbol` covers the whole session.
- The previous close is the last bar of the latest fetched prior session, not the official
  close.
- Observers' WATCH and ENTER symbols (for `[observers] watching_seconds` after the bar they
  were decided on) get L1 quotes as the lowest stream tier (`ObserverSet.watching`, `runtime/market._sync_subscriptions`): after the position, the
  active strategy's WATCH symbols and the discovery candidates, up to
  `[market_data] observer_subscriptions` and never more than the slots the higher tiers leave
  free above `stream_reserved_symbols` (it shrinks at once when they need room). It changes at
  most once per `observer_resync_seconds` and not at all while a position or a working entry
  exists, except to give slots back. Its requests go through `subscribe_optional` /
  `unsubscribe_optional` (broker/webull.py StreamSession): they wait while fewer than
  `stream_reserved_subscribe_requests` would stay free in the route budget, and a failure is
  journaled without a reconnect, so it never cuts the position's quotes. The quote arrives after the signal bar, so the first
  ENTER usually comes a minute or more after it, inside `signal_valid_minutes`. A symbol left
  at WATCH (no slot, refused, or quote too late) counts as missed for no quote.
- A valid signal can log ENTER on each of its up to `signal_valid_minutes`: count one
  entry per signal, not one per record. The daily observer report simulates at most one
  trade per signal (`skipped_same_signal`); the `outcomes` research command still measures
  every ENTER record.

## Engine wiring (to do in the engine, not here)

```python
from tickr.observers.loader import load_observers
from tickr.observers.log import DecisionLog
from tickr.observers.runner import ObserverSet

settings = config.observers
observers = ObserverSet(load_observers(config), DecisionLog.from_config(config),
                        settings.max_evaluation_seconds, settings.queue_size,
                        config.risk.max_quote_age_seconds, config.risk.max_clock_skew_seconds)

# per evaluation, right after the active strategy decided on `context` at time `now`:
for failure in observers.on_evaluation(context, now):   # only "queue full: dropped"
    journal("observer-dropped", failure)
# on every engine step:
for failure in observers.take_failures():
    journal("observer-off", failure)                     # and alert once
# at shutdown:
observers.close(timeout)                                  # False = a hung observer
```

- `on_evaluation` never evaluates on the caller's thread and never blocks: it freezes the
  context (bars as tuples, position removed) and puts it on a bounded queue (`queue_size`).
  One worker thread evaluates the observers in order and writes their records. When the queue
  is full the evaluation is dropped and returned as a failure.
- On the worker, an exception, a contract break (wrong tag or symbol) or an evaluation slower
  than `max_evaluation_seconds` switches that observer off for the run; a failed log write
  switches all of them off. `take_failures()` hands these to the trading thread. A hung
  observer keeps the worker busy; the queue then overflows and further evaluations are dropped
  and recorded, still without blocking trading.
- The active decision should be written through the same log, `observers.log.append(record)`
  (role `active`, admission `taken` / `refused` + cause, `not_applicable` for non-ENTER), so the
  daily report has both sides in one file. The log is thread-safe (one lock, whole lines).
- `on_evaluation` raises ValueError only for a caller bug (decision time before the bar ended).
- The log flushes each line but does not fsync: research data, not trading state.

Files: `<data_dir>/<decisions_dir>/<environment>/<NY date>/decisions.jsonl`, one decision record
per line (format in `docs/research.md`).

## Daily report

```
python -m tickr.research observers \
  --decisions data/decisions/sandbox/2026-11-17/decisions.jsonl \
  --bars data/capture/sandbox/2026-11-17/bars.jsonl --out out/observers-2026-11-17
```

Per identity (active first): decisions by verdict, ENTERs (taken / refused for the active),
every ENTER simulated like `whatif` (taken or not, so every version is measured the same way;
an ENTER while an earlier simulated trade of the same identity in that symbol is open is
`skipped_overlap`, and an observer's ENTER repeating a signal, same day, symbol, setup and
trigger, that already made a simulated trade is `skipped_same_signal`), trades, wins, losses, total and average R, gross USD for ENTERs with a
recorded quantity; for observers the bar-by-bar diff against the active decisions and the
number of ENTER bars shared with it. Outputs `observers.csv` and `observers.json`, labelled
`simulated`. The local API serves each observer's gross USD from this same simulation at
`GET /observers` (`result_gross_usd`, or `null` with `result_cause`; docs/api.md).
