# AI advisor

The advisor reads N recorded trading days and asks Claude to propose changes to one
strategy's `strategy.toml`. It never applies anything: the output is a new strategy
folder with a bumped version plus a markdown report. The owner reviews it and, if it
looks sensible, runs it as an observer (see `docs/observers.md`).

## Run

```
pip install -e '.[advisor]'          # the Anthropic SDK is an optional extra
export ANTHROPIC_API_KEY=...         # set in your shell; never in the repo
python -m aperture.advisor --days 2026-11-16 2026-11-17 2026-11-18 2026-11-19 2026-11-20
```

Options: `--strategy-folder` (default: the active strategy from `bot.toml`), `--out`
(default `<data_dir>/<advisor.proposals_dir>`), `--config`, `--research-settings`,
`--replay-settings`. Exit code 0 = proposal or "no change" written, 2 = refused.

## What it sends

For each day it reads `<data_dir>/<decisions_dir>/<env>/<day>/decisions.jsonl` and
`<data_dir>/<capture_dir>/<env>/<day>/bars.jsonl`, keeps the active decisions of the
chosen strategy name and version, and builds an evidence bundle:

- verdict counts, the first failed check on IGNORE/WATCH, ENTER refusal causes;
- the outcomes summary and the what-if summary (same code as `python -m aperture.research`);
- which observer versions were recorded.

The prompt carries the current `strategy.toml` and this bundle. Nothing else is sent:
no account ids, orders or credentials. The bundle is refused, not cut, when it exceeds
`max_evidence_bytes`.

## What it accepts

Claude answers in a fixed JSON schema. Every proposed change is checked and the whole
proposal is refused (with all causes listed in the report) when a change:

- touches `[strategy]` or a key that does not exist;
- is not a TOML literal, has a different type, or equals the current value;
- changes the same key twice, or there are more than `max_changes` changes.

The new `strategy.toml` keeps every comment, adds an advisor comment above each changed
line, and must load through the observer loader (so the strategy's own `create()`
validation runs). A value the strategy rejects refuses the proposal.

## Output

`<out>/<UTC timestamp>-<base folder>/`:

- `report.md`: status, summary, changes table, expected effect, risks, how to try it;
- `strategies/<name>-v<N>/strategy.toml`: only when something was proposed;
- `evidence.json`, `request.txt`, `response.json`: exactly what was sent and received.

To try a proposal: copy the folder into `strategies/` and add it to
`[strategy] observers` in `config/bot.toml`.

## API use

Model, effort, output limit and timeout are in `[advisor]` in `config/bot.toml`. The
request uses adaptive thinking, structured JSON output and streaming. It also enables
server-side model fallback (`fallbacks="default"`), so if the configured model is
overloaded the API may answer with another model; the report records the model that
actually answered. The SDK's default retries (two, on 429/5xx/connection errors) still apply, so
one run may bill up to three attempts; this is a read-only advisory call, not a broker send.
The code only checks that `ANTHROPIC_API_KEY` is set; the SDK reads
the value itself.

Tests use a fake client and a fake SDK module and never reach the network.
