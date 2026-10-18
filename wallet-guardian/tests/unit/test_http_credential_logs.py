"""Production HTTP logs must not contain provider credentials. Synthetic keys only."""

from __future__ import annotations

import logging

import httpx
import pytest

from guardian.cli import main
from guardian.security.redact import register_extra_secret
from guardian.sources.fixtures import PreparedCall
from guardian.sources.transport import HttpxBackend, _CredentialLogFilter, quiet_http_client_logs

ALCHEMY_KEY = "syntheticAlchemyKey"
QUERY_KEY = "synthetic-routescan-test-secret"
NODEREAL_KEY = "syntheticNodeRealKey"
EXTRA_KEY = "syntheticExtraRegisteredSecret"
ALCHEMY_URL = f"https://base-mainnet.g.alchemy.com/v2/{ALCHEMY_KEY}"
PINNED_ALCHEMY_URL = f"https://1.1.1.1/v2/{ALCHEMY_KEY}"
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


def test_credential_log_filter_masks_records_directly(caplog: pytest.LogCaptureFixture) -> None:
    register_extra_secret(EXTRA_KEY)
    log_filter = _CredentialLogFilter()
    for url, secret in (
        (ALCHEMY_URL, ALCHEMY_KEY),
        (PINNED_ALCHEMY_URL, ALCHEMY_KEY),
        (QUERY_URL, QUERY_KEY),
        (NODEREAL_URL, NODEREAL_KEY),
        (f"https://rpc.example.invalid/{EXTRA_KEY}", EXTRA_KEY),
    ):
        record = logging.LogRecord(
            name="httpx",
            level=logging.WARNING,
            pathname=__file__,
            lineno=1,
            msg='HTTP Request: %s "%s"',
            args=("POST", url),
            exc_info=None,
        )
        record.exc_text = f"Traceback: failed calling {url}"
        assert log_filter.filter(record) is True
        assert record.args == ()
        assert secret not in record.getMessage()
        assert "***" in record.getMessage()
        assert secret not in (record.exc_text or "")
        assert "***" in (record.exc_text or "")

    quiet_http_client_logs()
    caplog.set_level(logging.WARNING)
    logging.getLogger("httpx").warning('HTTP Request: %s "%s"', "POST", PINNED_ALCHEMY_URL)
    assert "HTTP Request: POST" in caplog.text
    assert ALCHEMY_KEY not in caplog.text
    assert "***" in caplog.text


def test_cli_entry_quiets_http_client_loggers(capsys: pytest.CaptureFixture[str]) -> None:
    logging.getLogger("httpx").setLevel(logging.DEBUG)
    logging.getLogger("httpcore").setLevel(logging.DEBUG)
    with pytest.raises(SystemExit):
        main(["--help"])
    assert logging.getLogger("httpx").level >= logging.WARNING
    assert logging.getLogger("httpcore").level >= logging.WARNING
    capsys.readouterr()

