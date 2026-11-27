The API contract from tickr-bot (schemas, fixtures, `contract/codes.json`, the demo day),
brought by tickr-bot's `.github/workflows/contract-sync.yml`. `contract/SOURCE` names the bot commit.

- Green: the apps read the new contract as they are. Review and queue it.
- Red (usually ContractTests: a new or changed schema has no Swift model yet): the apps' thread
  pushes the Swift model to this branch (`contract-sync`), so the schema and its model land in one
  pull request. Do not close this pull request; later bot changes are added to it.

---
_Opened by the contract-sync workflow of tickr-devs/tickr-bot._
