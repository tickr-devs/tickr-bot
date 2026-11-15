# AI advisor

The advisor reads evidence about one strategy and asks Claude to propose changes to its
`strategy.toml`, a new strategy idea, or both. The evidence is either N recorded trading
days (live mode) or the fitting period of a simulated backtest (backtest mode, below). It
never applies anything: the output is a new strategy folder with a bumped version, an idea
note, and a markdown report. The owner reviews it and, if it
looks sensible, runs it as an observer (see `docs/observers.md`).

## Run

```
pip install -e '.[advisor]'          # the Anthropic SDK is an optional extra
export ANTHROPIC_API_KEY=...         # set in your shell; never in the repo
python -m tickr.advisor --days 2026-11-16 2026-11-17 2026-11-18 2026-11-19 2026-11-20
```

Options: `--notes <file>` (text the model also reads, sent as written, for example ideas
already tested and rejected from docs/strategy-ledger.md; give only fitting-period results so no
hold-out number reaches it), `--strategy-folder` (default: the active strategy from `bot.toml`), `--out`
(default `<data_dir>/<advisor.proposals_dir>`), `--config`, `--research-settings`,
`--replay-settings`. Exit code 0 = proposal or "no change" written, 2 = refused.

## Backtest mode

```
python -m tickr.backtest run --out data/backtest --results out/backtest-v1 \
  --strategy tickr.strategy.price_action_v1 --values strategies/price-action-v1/strategy.toml
python -m tickr.advisor --backtest out/backtest-v1 --backtest-data data/backtest
```

The strategy folder (`--strategy-folder`, default active) must be the version the backtest
ran. The backtest report splits its window at `holdout.starts` (the last `holdout_percent`
of sessions, `config/backtest.toml`). The advisor reads `trades.csv`, `signals.csv` and the
traded symbols' minute bars **only before that date** (the fitting period); no hold-out
number or date goes into the request. The bundle (aggregates, no rows): signal outcomes and
causes; trade statistics overall and by month, setup, exit reason, entry hour, entry price,
stop distance; and excursions in R: the best high while a trade was open and, after a stop,
until the session end (was the entry wrong, or the stop?). Fewer fitting trades than
`[advisor] backtest_min_fitting_trades` is refused. Bucket edges are in `[advisor]`.

The run folder also gets `tuning.json` (window, hold-out start, the base's hold-out numbers;
never sent). Then judge the proposal on the hold-out it never saw:

```
python -m tickr.backtest run ... --results out/backtest-v2 --values <run>/strategies/price-action-v1-v2/strategy.toml
python -m tickr.advisor.judge --run <run folder> --results out/backtest-v2
```

`judge` refuses a candidate on other data (the report's pool counts differ), another window
or hold-out split, or equal to the base. Run base and candidate on the same fetch. It
writes `judge-<name>-v<N>.md`: the base and candidate hold-out rows with their bootstrap
intervals, and the candidate's fitting period marked as no evidence. A module written from
an idea (below) is judged the same way.

## New strategy idea

When the evidence says the entry idea itself fails, Claude may describe a new strategy in
`new_strategy_idea` (name like `pullback-v1`, hypothesis, entry and exit rules, filters,
evidence, test plan), in either mode. It is text, never code: `idea.md` tells a developer
to write the module and its commented `strategy.toml` in a reviewed pull request (rule 8),
backtest it, and judge it on the hold-out. A person or agent writing that module has usually
seen the hold-out numbers already (docs/backtest-results.md publishes them), so for a
hand-written module this window's hold-out is not clean: judge it on sessions after the
window (a fresh fetch) or on observer days. Only an advisor value proposal, which nobody
tuned by hand, is judged cleanly by `judge` on this window. A partial idea (no hypothesis, entry rule or
evidence) or a malformed name refuses the answer. To see the logic its values steer, the
request carries the strategy module's source code as well.

## What it sends (live mode)

For each day it reads `<data_dir>/<decisions_dir>/<env>/<day>/decisions.jsonl` and
`<data_dir>/<capture_dir>/<env>/<day>/bars.jsonl`, keeps the active decisions of the
chosen strategy name and version, and builds an evidence bundle:

- verdict counts, the first failed check on IGNORE/WATCH, ENTER refusal causes;
- the outcomes summary and the what-if summary (same code as `python -m tickr.research`);
- which observer versions were recorded.

The prompt carries the current `strategy.toml`, the strategy module's source and this bundle. Nothing else is sent:
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

## Hold-out rule

A proposal is fitted to the days it was given, so those days cannot judge it. The rule
(from the independent review, 2026-11-14; to be confirmed by the owner) applies to every new strategy version, from the advisor or by hand:

- Write down the days the change was chosen on. The advisor does it for you: the new
  `strategy.toml` header and `report.md` list them.
- Judge the version only on other days: live observer days after the last tuning day, or
  replay/backtest days outside the tuning list.
- Results that include tuning days are not evidence for the change.

This is not a minimum-sample gate (2026-10-16 still stands: the owner decides when to
switch); it only says which days count when he looks.

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
