"""Live DNS and outbound sockets fail immediately and stay failed reads."""

from __future__ import annotations

import socket
import subprocess
import sys
import time

import pytest

from guardian.errors import SecurityError
from guardian.security.ssrf import validate_url


def test_hostname_lookup_does_not_wait_on_dns():
    started = time.monotonic()
    with pytest.raises(socket.gaierror, match="live DNS"):
        socket.getaddrinfo("example.invalid", 443)
    assert time.monotonic() - started < 0.5


def test_reverse_lookup_does_not_wait_on_dns():
    started = time.monotonic()
    assert socket.getfqdn("example.invalid") == "example.invalid"
    with pytest.raises(socket.herror, match="live DNS"):
        socket.gethostbyaddr("1.1.1.1")
    assert time.monotonic() - started < 0.5


def test_public_ip_connect_is_refused_without_a_timeout():
    started = time.monotonic()
    with pytest.raises(OSError, match="network sockets"):
        socket.create_connection(("1.1.1.1", 443), timeout=30)
    assert time.monotonic() - started < 0.5


def test_unresolved_https_url_is_a_failed_read_not_an_empty_success():
    with pytest.raises(SecurityError, match="dns resolution failed"):
        validate_url("https://example.invalid/balance")


def test_loopback_lookup_is_still_local():
    infos = socket.getaddrinfo("127.0.0.1", 9)
    assert infos
    assert infos[0][4][0] == "127.0.0.1"


def test_child_python_does_not_use_live_dns():
    code = (
        "import socket\n"
        "try:\n"
        "    socket.getaddrinfo('example.invalid', 443)\n"
        "except socket.gaierror as exc:\n"
        "    print(exc)\n"
        "else:\n"
        "    raise SystemExit(2)\n"
    )
    completed = subprocess.run(
        [sys.executable, "-c", code],
        check=False,
        capture_output=True,
        text=True,
        timeout=5,
    )
    assert completed.returncode == 0, completed.stderr
    assert "live DNS" in completed.stdout
