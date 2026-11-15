"""Requests under /demo/ forwarded to the demo day's API (`tickr demo`, docs/demo.md).

The demo day serves its own API on `[api] demo_port` of this API's loopback host. The apps reach
it through their usual address with `/demo` in front of the path, so the phone needs no second
pairing and no second address: its request arrives here, passes this API's own checks (the Host
guard and, on the remote port, the paired token), and only then goes on to the demo.

Only the path after `/demo`, the query, the body and the headers the API's routes read
(Content-Type and Idempotency-Key, with a Content-Length of the body actually sent; a GET sends
neither body nor body headers) are forwarded; the token, cookies and every other header stay
here. The target host and port come from config, never from the request. Nothing under /demo/ is
ever handled by this API's own routes: a command sent there reaches the demo's inbox, never the
bot's. The demo's HTML page gets this API's page headers (CSP and others).
"""

from __future__ import annotations

import http.client
from dataclasses import dataclass
from decimal import Decimal
from urllib.parse import unquote

DEMO_ROOT = "/demo"  # the prefix the apps put in front of every demo request (protocol constant)
DEMO_PREFIX = DEMO_ROOT + "/"
# Request headers forwarded to the demo's API; everything else (Authorization above all) stays.
# Content-Length is never taken from the client: it is set from the body actually forwarded.
FORWARDED_REQUEST_HEADERS = ("Content-Type", "Idempotency-Key")
# Headers that describe a body: dropped when there is none (a GET), so the demo never waits for
# bytes that are not coming.
BODY_HEADERS = ("Content-Type",)
# Response headers passed back to the app.
FORWARDED_RESPONSE_HEADERS = ("Content-Type", "Allow", "Retry-After")


@dataclass(frozen=True)
class Forwarded:
    """The demo API's answer, passed back as it came."""

    status: int
    headers: dict[str, str]
    body: bytes


class DemoUnavailable(Exception):
    """The demo day's API did not answer; the message is the single cause."""


def is_demo_path(path: str) -> bool:
    return path == DEMO_ROOT or path.startswith(DEMO_PREFIX)


# Path segments that would step out of the path they are in, and characters that would start a
# new segment, once percent-decoded.
DOT_SEGMENTS = (".", "..")
SEPARATORS = ("/", "\\")


def stays_in_demo(path: str) -> bool:
    """True when no segment of `path` (without its query), decoded as often as it decodes
    (`%2e`, `%252e`, ...), is `.` or `..` or hides a separator (`%2f`, `%5c`): such a request
    could mean another path than it shows, so it is refused, never forwarded or normalised."""
    for segment in path.split("/"):
        decoded = segment
        while (once := unquote(decoded)) != decoded:
            decoded = once
        if decoded in DOT_SEGMENTS or any(s in decoded for s in SEPARATORS):
            return False
    return True


def target(raw_path: str) -> str:
    """`/demo/status?x=1` -> `/status?x=1`; `/demo` -> `/`."""
    rest = raw_path[len(DEMO_ROOT):]
    return rest if rest.startswith("/") else "/" + rest


def forward(host: str, port: int, timeout_seconds: Decimal, method: str, raw_path: str,
            headers: dict[str, str], body: bytes | None) -> Forwarded:
    """One request to the demo's API at host:port, answered as it came. Refused with
    DemoUnavailable when the demo is not listening or does not answer in time."""
    sent = {name: headers[name] for name in FORWARDED_REQUEST_HEADERS if name in headers}
    if body is None:
        for name in BODY_HEADERS:
            sent.pop(name, None)
    else:
        sent["Content-Length"] = str(len(body))
    sent["Host"] = f"{host}:{port}"  # the demo's own allowed host: its loopback address
    connection = http.client.HTTPConnection(host, port, timeout=float(timeout_seconds))
    try:
        connection.request(method, target(raw_path), body=body, headers=sent)
        response = connection.getresponse()
        answer = response.read()
        passed = {name: value for name in FORWARDED_RESPONSE_HEADERS
                  if (value := response.getheader(name)) is not None}
        return Forwarded(response.status, passed, answer)
    except (OSError, http.client.HTTPException) as exc:
        raise DemoUnavailable(f"the demo day is not answering on port {port} "
                              f"({type(exc).__name__}); start it with the local.tickr.demo "
                              "job or `tickr demo` (docs/demo.md)") from exc
    finally:
        connection.close()
