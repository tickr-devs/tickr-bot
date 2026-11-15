# Tickr

Single-owner intraday trading bot for US common stocks on Webull OpenAPI.
Renamed from Aperture on 2026-11-15; a Mac set up under the old name moves with `ops/migrate-from-aperture.sh` (docs/operations.md).  <!-- rename-keep -->

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

`open Tickr.xcworkspace` opens the operator apps and their shared package in Xcode. The projects are kept
current with Xcode 27.2 (the owner's version) and still open in Xcode 16 or newer.
Scheme `Tickr macOS` runs on this Mac. The iOS app joins the workspace with its committed
Xcode project (in progress). Details: `apps/macos/README.md`, `apps/ios/README.md`.
