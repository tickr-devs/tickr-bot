"""The trained model's path in the bot: the versioned bundle (`bundle`) and the shared feature
code (`features`). Only this package imports the model libraries (the `model` extra:
LightGBM, numba, numpy, pandas at the training stack's pinned versions), and it imports them
lazily, so the bot without the extra is unchanged while `[model] enabled` is false.

No module here sends orders, reads a broker or knows a user setting: layer 1 sees market data
only (owner, 2026-11-20). Decisions stay with the strategy interface and the risk layer.
"""
