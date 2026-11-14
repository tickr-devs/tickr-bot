"""AI advisor: proposes a new strategy version from recorded evidence. Never applies it.

Flow (`python -m aperture.advisor`, see docs/advisor.md):
1. `evidence.build_evidence`: outcomes, what-if and observer summaries over N recorded days.
2. `client.AnthropicClient`: one Claude request with the evidence and the current
   strategy.toml, answered as JSON in a fixed schema.
3. `proposal`: every proposed change is checked (existing key, same type, accepted by the
   strategy's own create()), then written as a NEW strategy folder with a bumped version plus
   a markdown report. The owner decides; the proposal can run as an observer first.

The bot never imports this package.
"""
