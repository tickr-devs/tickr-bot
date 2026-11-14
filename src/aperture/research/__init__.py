"""Signal research over captured bars and recorded decisions. Offline; never trades.

- `outcomes`: price moves after WATCH/ENTER decisions (observed, never called profit).
- `whatif`: ENTER decisions that were not taken, simulated through the replay engine.
- `compare`: record another strategy version over the same bars and diff its decisions.

CLI: `python -m aperture.research {outcomes,whatif,decide,diff}`; see docs/research.md.
"""
