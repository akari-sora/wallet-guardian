"""Production HTTP logs must not contain provider credentials. Synthetic keys only."""

from __future__ import annotations

import logging

import httpx
import pytest

from guardian.cli import main
from guardian.sources.fixtures import PreparedCall
from guardian.sources.transport import HttpxBackend

ALCHEMY_KEY = "syntheticAlchemyKey"
QUERY_KEY = "synthetic-routescan-test-secret"
NODEREAL_KEY = "syntheticNodeRealKey"
ALCHEMY_URL = f"https://base-mainnet.g.alchemy.com/v2/{ALCHEMY_KEY}"
QUERY_URL = (
    "https://api.routescan.io/v2/network/mainnet/evm/1/etherscan/api"
    f"?action=txlist&apikey={QUERY_KEY}&module=account"
)
NODEREAL_URL = f"https://bsc-mainnet.nodereal.io/v1/{NODEREAL_KEY}"


def _handler(_request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, json={"ok": True})


def test_httpx_backend_debug_logs_omit_credentials(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    logging.getLogger("httpx").setLevel(logging.DEBUG)
    logging.getLogger("httpcore").setLevel(logging.DEBUG)
    client = httpx.Client(transport=httpx.MockTransport(_handler))
    backend = HttpxBackend(client=client)
    calls = (
        PreparedCall("alchemy-base", "POST", ALCHEMY_URL, b"{}", "jsonrpc", connect_ip="1.1.1.1"),
        PreparedCall("routescan", "GET", QUERY_URL, b"", "rest"),
        PreparedCall("nodereal-bsc", "POST", NODEREAL_URL, b"{}", "jsonrpc", connect_ip="1.1.1.1"),
    )
    try:
        for call in calls:
            backend.send(call)
    finally:
        client.close()
    blob = caplog.text
    for secret in (ALCHEMY_KEY, QUERY_KEY, NODEREAL_KEY, ALCHEMY_URL, QUERY_URL, NODEREAL_URL):
        assert secret not in blob


def test_cli_entry_quiets_http_client_loggers(capsys: pytest.CaptureFixture[str]) -> None:
    logging.getLogger("httpx").setLevel(logging.DEBUG)
    logging.getLogger("httpcore").setLevel(logging.DEBUG)
    with pytest.raises(SystemExit):
        main(["--help"])
    assert logging.getLogger("httpx").level >= logging.WARNING
    assert logging.getLogger("httpcore").level >= logging.WARNING
    capsys.readouterr()
