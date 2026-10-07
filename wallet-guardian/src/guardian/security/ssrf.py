"""Block fetches that can reach loopback, link-local, or private networks."""

from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urljoin, urlsplit

from guardian.errors import SecurityError

_LOCAL_NAMES = {
    "localhost",
    "localhost.localdomain",
    "metadata.google.internal",
    "metadata",
}


def _blocked_ip(addr: str) -> bool:
    ip = ipaddress.ip_address(addr)
    return (
        not ip.is_global
        or ip.is_loopback
        or ip.is_private
        or ip.is_link_local
        or ip.is_reserved
        or ip.is_multicast
        or ip.is_unspecified
    )


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
    if literal is not None and _blocked_ip(str(literal)):
        raise SecurityError("private or non-global ip is not allowed")
    if literal is None:
        resolver = resolve or _default_resolve
        try:
            addresses = resolver(lowered)
        except OSError as exc:
            raise SecurityError("dns resolution failed") from exc
        if not addresses:
            raise SecurityError("dns returned no addresses")
        for addr in addresses:
            if _blocked_ip(addr):
                raise SecurityError("dns answer points at a blocked address")
    return url


def next_redirect(current: str, location: str, *, resolve=None) -> str:
    target = urljoin(current, location)
    return validate_url(target, resolve=resolve)
