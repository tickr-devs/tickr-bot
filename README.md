# Aperture

Single-owner intraday trading bot for US common stocks on Webull OpenAPI.

- What v1 does: [`docs/spec-v1.md`](docs/spec-v1.md)
- Rules for contributors and agents: [`CLAUDE.md`](CLAUDE.md)
- Broker facts: [`docs/webull-facts.md`](docs/webull-facts.md)
- Owner decisions: [`docs/decisions.md`](docs/decisions.md)
- Later work: [`docs/backlog.md`](docs/backlog.md)

```
pip install -e ".[dev]"
pytest -q
```

## Apps in Xcode

`open Aperture.xcworkspace` opens the operator apps and their shared package in Xcode 16+.
Scheme `Aperture macOS` runs on this Mac. The iOS app joins the workspace with its committed
Xcode project (in progress). Details: `apps/macos/README.md`, `apps/ios/README.md`.
