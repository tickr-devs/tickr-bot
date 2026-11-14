# Recorded Webull responses

Real responses from the owner's Sandbox account, copied from the frozen `Trading` project's evidence
(read only). Each file is `{"source", "status", "body"}`; `body` is the HTTP body as received, or,
for errors, the `error_code` and `message` the SDK's `ServerException` carried.

Anonymised: no account id appears anywhere (tested); broker order ids are replaced with
`ORDERID…`. Client order ids are our own probe ids and are kept.

Not recorded yet (stage 0 probes): a fill, a working STOP_LOSS, a non-empty position or open-order
list, a place acknowledgement body, the accounts list. Tests that need those shapes derive them
from the recorded order row and say so.
