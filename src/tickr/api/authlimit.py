"""Failed-authentication limit of the remote port, per client address.

Each budget (the device token, POST /pair) counts failed checks per client address in a sliding
window, in memory. An address with `failures` failed checks inside the window is refused before
its credential is looked at, until the oldest of them leaves the window. A success resets nothing,
so an attacker sharing an address with a paired device gains nothing from the device's successes.

Concurrent requests reserve their attempt before the check (`acquire`) and settle it after
(`release`): a burst of parallel connections gets the same budget as a sequential one.

Memory is bounded: at most `max_addresses` addresses are tracked, the one whose last failure is
the oldest is dropped first, and only once it no longer counts. When every tracked address still
counts, an address not in the table is refused (fail closed) rather than checked unlimited.

The client address comes from `client_key`: the last X-Forwarded-For entry when the TCP peer is
the trusted proxy (Caddy on the server, `tailscale serve` on the Mac, both from loopback: each
replaces or appends to what the caller sent, so the last entry is the one it saw), else the peer.
IPv6 addresses count per prefix, since one host may use any address of its prefix.
"""

from __future__ import annotations

import ipaddress
import math
import threading
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass, field

FORWARDED_FOR = "X-Forwarded-For"
LIST_SEPARATOR = ","  # X-Forwarded-For entries (RFC 7239 section 7.4, protocol constant)
MIN_RETRY_AFTER_SECONDS = 1  # Retry-After is a whole number of seconds, at least one
BAD_FORWARDED_FOR = "X-Forwarded-For from the proxy does not end in an IP address"


class BadForwardedFor(ValueError):
    """The trusted proxy's X-Forwarded-For does not end in an IP address."""


def client_key(peer: str, forwarded_for: list[str], trusted_proxy: str,
               ipv6_prefix_length: int) -> str:
    """The address the budgets count: the last X-Forwarded-For entry when `peer` is
    `trusted_proxy` and the header is present, else `peer`. An IPv6 address counts as its
    `ipv6_prefix_length` network; an IPv4-mapped IPv6 address as its IPv4 address. Raises
    BadForwardedFor for a last entry that is not an IP address (the caller counts it under the
    peer)."""
    text = peer
    if forwarded_for and _same_address(peer, trusted_proxy):
        text = LIST_SEPARATOR.join(forwarded_for).rsplit(LIST_SEPARATOR, 1)[-1].strip()
        try:
            ipaddress.ip_address(text)
        except ValueError as exc:
            raise BadForwardedFor(BAD_FORWARDED_FOR) from exc
    return address_key(text, ipv6_prefix_length)


def address_key(text: str, ipv6_prefix_length: int) -> str:
    address = ipaddress.ip_address(text)
    if isinstance(address, ipaddress.IPv6Address):
        if address.ipv4_mapped is not None:
            return str(address.ipv4_mapped)
        return str(ipaddress.IPv6Network((address, ipv6_prefix_length), strict=False))
    return str(address)


def _same_address(peer: str, trusted: str) -> bool:
    try:
        return ipaddress.ip_address(peer) == ipaddress.ip_address(trusted)
    except ValueError:
        return False


@dataclass
class _Entry:
    failures: list[float] = field(default_factory=list)  # monotonic times, oldest first
    in_flight: int = 0


@dataclass(frozen=True)
class Refused:
    """An address refused before its check: `retry_after` whole seconds; `table_full` when it is
    refused only because every tracked address still counts (fail closed)."""

    retry_after: int
    table_full: bool


class AuthLimiter:
    """One budget: `failures` failed checks per address in `window_seconds`. Thread-safe."""

    def __init__(self, failures: int, window_seconds: int, max_addresses: int,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.failures = failures
        self.window = window_seconds
        self.max_addresses = max_addresses
        self._clock = clock
        self._lock = threading.Lock()
        # Ordered by last failure (a new address last): the first one is evicted first.
        self._table: OrderedDict[str, _Entry] = OrderedDict()

    def __len__(self) -> int:
        return len(self._table)

    def acquire(self, key: str) -> Refused | None:
        """Reserve one check for `key`, or say why not. Every None must be `release`d."""
        with self._lock:
            now = self._clock()
            entry = self._table.get(key)
            if entry is None:
                full = self._make_room(now)
                if full is not None:
                    return full
                entry = self._table[key] = _Entry()
            self._prune(entry, now)
            if len(entry.failures) + entry.in_flight >= self.failures:
                return Refused(self._wait(entry, now), table_full=False)
            entry.in_flight += 1
            return None

    def release(self, key: str, failed: bool) -> bool:
        """Settle a reserved check; True when this failure used up the address's budget (the
        start of a block, logged and alerted once by the caller)."""
        with self._lock:
            entry = self._table[key]
            entry.in_flight -= 1
            if failed:
                entry.failures.append(self._clock())
                self._table.move_to_end(key)
                return len(entry.failures) == self.failures
            if not entry.failures and not entry.in_flight:
                del self._table[key]  # a success holds no slot
            return False

    def _make_room(self, now: float) -> Refused | None:
        while len(self._table) >= self.max_addresses:
            oldest_key, oldest = next(iter(self._table.items()))
            self._prune(oldest, now)
            if oldest.failures or oldest.in_flight:
                # Room comes when its newest failure leaves the window.
                return Refused(self._wait(oldest, now, newest=True), table_full=True)
            del self._table[oldest_key]
        return None

    def _prune(self, entry: _Entry, now: float) -> None:
        while entry.failures and entry.failures[0] <= now - self.window:
            entry.failures.pop(0)

    def _wait(self, entry: _Entry, now: float, newest: bool = False) -> int:
        """Seconds until the oldest (or `newest`) counted failure leaves the window; one when
        only checks still in flight hold the budget."""
        if not entry.failures:
            return MIN_RETRY_AFTER_SECONDS
        at = entry.failures[-1] if newest else entry.failures[0]
        return max(MIN_RETRY_AFTER_SECONDS, math.ceil(at + self.window - now))
