"""Refuse live DNS and non-loopback connects during tests.

Hostname lookup raises ``gaierror`` immediately. Readers that catch ``OSError``
keep a failed read (``dns resolution failed`` / ``dns_failed``) and do not turn
it into an empty success. Loopback, IP literals, and Unix sockets still work.
"""

from __future__ import annotations

import ipaddress
import socket
import _socket

_LOCAL_NAMES = frozenset({"localhost", "localhost.localdomain", "ip6-localhost", "ip6-loopback"})
_ORIGINAL_SOCKET = socket.socket
_ORIGINAL_GETADDRINFO_C = _socket.getaddrinfo
_ORIGINAL_GETADDRINFO = socket.getaddrinfo
_ORIGINAL_CREATE = socket.create_connection
_ORIGINAL_GETHOSTBYNAME = socket.gethostbyname
_ORIGINAL_GETHOSTBYADDR = socket.gethostbyaddr
_ORIGINAL_GETNAMEINFO = socket.getnameinfo


def _text(host: object) -> str:
    if isinstance(host, bytes):
        host = host.decode("ascii", "replace")
    return str(host).strip().strip("[]").split("%", 1)[0]


def _parsed_ip(host: object) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    if host is None:
        return None
    try:
        return ipaddress.ip_address(_text(host))
    except ValueError:
        return None


def lookup_allowed(host: object) -> bool:
    """Wildcard, localhost names, and numeric addresses. Other hostnames need DNS."""
    if host is None:
        return True
    text = _text(host)
    if text in {"", "0.0.0.0", "::"}:
        return True
    if text.lower().rstrip(".") in _LOCAL_NAMES:
        return True
    return _parsed_ip(host) is not None


def _loopback_host(host: object) -> bool:
    if host is None:
        return False
    text = _text(host)
    if text.lower().rstrip(".") in _LOCAL_NAMES:
        return True
    parsed = _parsed_ip(host)
    return parsed is not None and parsed.is_loopback


def address_allowed(address: object) -> bool:
    """Unix sockets and loopback may connect. Every other destination is refused."""
    if isinstance(address, (str, bytes)):
        return True
    if isinstance(address, tuple) and address:
        host = address[0]
        if isinstance(host, (str, bytes)) and _text(host).startswith("/"):
            return True
        return _loopback_host(host)
    return False


def _deny_lookup() -> None:
    raise socket.gaierror(socket.EAI_NONAME, "tests do not use live DNS")


def _guarded_c_getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):  # noqa: A002
    if not lookup_allowed(host):
        _deny_lookup()
    return _ORIGINAL_GETADDRINFO_C(host, port, family, type, proto, flags)


def _guarded_getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):  # noqa: A002
    if not lookup_allowed(host):
        _deny_lookup()
    return _ORIGINAL_GETADDRINFO(host, port, family, type, proto, flags)


def _guarded_gethostbyname(host):
    if not lookup_allowed(host):
        _deny_lookup()
    return _ORIGINAL_GETHOSTBYNAME(host)


def _guarded_gethostbyaddr(host):
    # Reverse lookup is DNS even for a numeric public address.
    if not _loopback_host(host):
        raise socket.herror(1, "tests do not use live DNS")
    return _ORIGINAL_GETHOSTBYADDR(host)


def _guarded_getnameinfo(sockaddr, flags=0):
    host = sockaddr[0] if isinstance(sockaddr, tuple) and sockaddr else sockaddr
    if not _loopback_host(host):
        numeric = socket.NI_NUMERICHOST | socket.NI_NUMERICSERV
        return _ORIGINAL_GETNAMEINFO(sockaddr, flags | numeric)
    return _ORIGINAL_GETNAMEINFO(sockaddr, flags)


def _guarded_getfqdn(name: str = "") -> str:
    """Skip the reverse lookup. An empty name stays the local hostname."""
    text = str(name).strip()
    if not text or text in {"0.0.0.0", "::"}:
        return socket.gethostname()
    return text


def _guarded_create_connection(address, timeout=socket._GLOBAL_DEFAULT_TIMEOUT, source_address=None, *, all_errors=False):
    if not address_allowed(address):
        raise OSError(101, "tests do not open network sockets")
    return _ORIGINAL_CREATE(address, timeout, source_address, all_errors=all_errors)


class GuardedSocket(_ORIGINAL_SOCKET):
    def connect(self, address):  # type: ignore[override]
        if not address_allowed(address):
            raise OSError(101, "tests do not open network sockets")
        return super().connect(address)

    def connect_ex(self, address):  # type: ignore[override]
        if not address_allowed(address):
            return 101
        return super().connect_ex(address)


def install(monkeypatch=None) -> None:
    """Apply the guard. ``monkeypatch`` restores it after the test."""

    def set_(obj, name: str, value) -> None:
        if monkeypatch is not None:
            monkeypatch.setattr(obj, name, value)
        else:
            setattr(obj, name, value)

    set_(_socket, "getaddrinfo", _guarded_c_getaddrinfo)
    set_(socket, "getaddrinfo", _guarded_getaddrinfo)
    set_(socket, "gethostbyname", _guarded_gethostbyname)
    set_(socket, "gethostbyaddr", _guarded_gethostbyaddr)
    set_(socket, "getnameinfo", _guarded_getnameinfo)
    set_(socket, "getfqdn", _guarded_getfqdn)
    set_(socket, "create_connection", _guarded_create_connection)
    set_(socket, "socket", GuardedSocket)
