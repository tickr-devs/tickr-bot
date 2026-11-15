# Observer strategies (shadow versions, never trade)

An observer is another strategy version that sees exactly what the active strategy sees and
whose decisions are only recorded. It cannot place, cancel or size an order and never reaches
the safety layer. Use it to compare a candidate version against the active one on live days;
the daily report then shows both side by side (`python -m aperture.research observers`).

## Configure

`config/bot.toml`:

```
[strategy]
active = "price-action-v1"
observers = ["price-action-v1-lookback8"]      # folders under strategies/

[observers]
strategy_modules = ["price-action-v1=aperture.strategy.price_action_v1", "price-action-v2=aperture.strategy.price_action_v2"]
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

## Candidate: price-action-v2

`strategies/price-action-v2/` is price-action-v1 (same values) plus two backlog rules, in
`src/aperture/strategy/price_action_v2.py`. New rules need new code, so it is a new strategy
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
python -m aperture.replay --bars <days> --strategy aperture.strategy.price_action_v2 \
  --values strategies/price-action-v2/strategy.toml --out out/replay-v2
python -m aperture.research decide --bars <days> --strategy aperture.strategy.price_action_v2 \
  --values strategies/price-action-v2/strategy.toml --out out/v2
python -m aperture.research diff --baseline <v1 decisions.jsonl> \
  --candidate out/v2/decisions.jsonl --settings config/research.toml --out out/diff
```

To change a value, copy the folder and bump `version`. Making it the active strategy is an
owner decision (`[strategy] active`), not something this candidate does.

## Engine wiring (to do in the engine, not here)

```python
from aperture.observers.loader import load_observers
from aperture.observers.log import DecisionLog
from aperture.observers.runner import ObserverSet

settings = config.observers
observers = ObserverSet(load_observers(config), DecisionLog.from_config(config),
                        settings.max_evaluation_seconds, settings.queue_size)

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
python -m aperture.research observers \
  --decisions data/decisions/sandbox/2026-11-17/decisions.jsonl \
  --bars data/capture/sandbox/2026-11-17/bars.jsonl --out out/observers-2026-11-17
```

Per identity (active first): decisions by verdict, ENTERs (taken / refused for the active),
every ENTER simulated like `whatif` (taken or not, so every version is measured the same way;
an ENTER while an earlier simulated trade of the same identity in that symbol is open is
`skipped_overlap`), trades, wins, losses, total and average R, gross USD for ENTERs with a
recorded quantity; for observers the bar-by-bar diff against the active decisions and the
number of ENTER bars shared with it. Outputs `observers.csv` and `observers.json`, labelled
`simulated`. The local API serves each observer's gross USD from this same simulation at
`GET /observers` (`result_gross_usd`, or `null` with `result_cause`; docs/api.md).
