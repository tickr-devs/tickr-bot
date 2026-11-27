# Tickr

Single-owner intraday trading bot for US common stocks on Webull OpenAPI.
Renamed from Aperture on 2026-11-15.  <!-- rename-keep -->

- What v1 does: [`docs/spec-v1.md`](docs/spec-v1.md)
- Rules for contributors and agents: [`CLAUDE.md`](CLAUDE.md)
- Broker facts: [`docs/webull-facts.md`](docs/webull-facts.md)
- Owner decisions: [`docs/decisions.md`](docs/decisions.md)
- Later work: [`docs/backlog.md`](docs/backlog.md)

```
pip install -e ".[dev]"
pytest -q
```

## The other repositories

Tickr was split on 2026-11-27. This repository holds the bot, its API, the model and research.

- [tickr-ios](https://github.com/tickr-devs/tickr-ios): the macOS, iOS and iPadOS apps. They are
  built against the API contract this repository publishes: `src/tickr/api/schema/`,
  `tests/api/fixtures/` and `contract/` (codes the bot sends, the demo day).
  `.github/workflows/contract-sync.yml` opens a pull request there when it changes.
- [tickr-web](https://github.com/tickr-devs/tickr-web): the tickr.bot site.
- [tickr-infra](https://github.com/tickr-devs/tickr-infra): Terraform for the bot and site servers
  and the AWS account (including the role that lets `server-image.yml` push the image).

## Releases

Actions > release > Run workflow tags main with the next semver and opens a GitHub Release.
