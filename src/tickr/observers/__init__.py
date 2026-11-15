"""Live observer strategies: shadow versions that decide on the same data as the active
strategy and never trade.

- `loader`: build the observer strategies listed in bot.toml `[strategy] observers`.
- `runner.ObserverSet.on_evaluation`: the hook the engine calls once per active evaluation.
- `log.DecisionLog`: per-day decision record files (format: tickr.research.records).

Nothing here can place, cancel or size an order: observers only return decisions, which are
recorded with role `observer`. See docs/observers.md.
"""
