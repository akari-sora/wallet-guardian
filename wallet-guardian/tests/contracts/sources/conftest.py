"""These tests are offline. A real socket or HTTP client is a failure."""

from __future__ import annotations

import http.client
import socket
import sys
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))


@pytest.fixture(autouse=True)
def _block_network(monkeypatch):
    def blocked(*_args, **_kwargs):
        raise AssertionError("network is disabled")

    monkeypatch.setattr(socket, "socket", blocked)
    monkeypatch.setattr(socket, "create_connection", blocked)
    monkeypatch.setattr(socket, "getaddrinfo", blocked)
    monkeypatch.setattr(http.client, "HTTPConnection", blocked)
    monkeypatch.setattr(http.client, "HTTPSConnection", blocked)
    monkeypatch.setattr(httpx, "Client", blocked)
