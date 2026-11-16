# Recorded Webull responses

Real responses from the owner's Sandbox account, copied from the frozen `Trading` project's evidence
(read only). Each file is `{"source", "status", "body"}`; `body` is the HTTP body as received, or,
for errors, the `error_code` and `message` the SDK's `ServerException` carried.

Anonymised: no account id appears anywhere (tested); broker order ids are replaced with
`ORDERID…`. Client order ids are our own probe ids and are kept.

Recorded 2026-11-16 from the bot's first Sandbox trade: a long position, its working STOP_LOSS, the filled
LIMIT entry in history and the balance with the position. Not recorded yet: a triggered stop, a place
acknowledgement body, the accounts list (its rows carry account ids, so it is described in
docs/webull-facts.md instead). Tests that need those shapes derive them
from the recorded order row and say so.
