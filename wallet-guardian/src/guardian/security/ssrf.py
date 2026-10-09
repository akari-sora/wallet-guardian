"""Block fetches that can reach loopback, link-local, or private networks."""

from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urljoin, urlsplit

from guardian.errors import SecurityError
from guardian.sources.resolver import BENCHMARK_MESSAGE, destination_block_reason

_LOCAL_NAMES = {
    "localhost",
    "localhost.localdomain",
    "metadata.google.internal",
    "metadata",
}


def _blocked_ip(addr: str, *, host: str | None = None) -> bool:
    """Blocked when the address is not a global unicast destination.

    The hostname does not create an exception. 198.18.0.0/15 stays blocked,
    including answers for public API names.
    """
    del host
    return destination_block_reason(addr) is not None


def _reject_resolved(addr: str, *, literal: bool) -> None:
    reason = destination_block_reason(addr)
    if reason is None:
        return
    if reason == "benchmark":
        raise SecurityError(BENCHMARK_MESSAGE)
    if literal:
        raise SecurityError("private or non-global ip is not allowed")
    raise SecurityError("dns answer points at a blocked address")


def _default_resolve(host: str) -> list[str]:
    infos = socket.getaddrinfo(host, None)
    return [item[4][0] for item in infos]


def validate_url(url: str, *, resolve=None) -> str:
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"}:
        raise SecurityError("url scheme is not allowed")
    host = parsed.hostname
    if not host:
        raise SecurityError("url is missing a host")
    lowered = host.lower().rstrip(".")
    if lowered in _LOCAL_NAMES or lowered.endswith(".localhost") or lowered.endswith(".local"):
        raise SecurityError("local host is not allowed")
    literal: ipaddress._BaseAddress | None
    try:
        literal = ipaddress.ip_address(lowered)
    except ValueError:
        literal = None
    if literal is not None:
        _reject_resolved(str(literal), literal=True)
    if literal is None:
        resolver = resolve or _default_resolve
        try:
            addresses = resolver(lowered)
        except OSError as exc:
            raise SecurityError("dns resolution failed") from exc
        if not addresses:
            raise SecurityError("dns returned no addresses")
        for addr in addresses:
            _reject_resolved(addr, literal=False)
    return url


def next_redirect(current: str, location: str, *, resolve=None) -> str:
    target = urljoin(current, location)
    return validate_url(target, resolve=resolve)
