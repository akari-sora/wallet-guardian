"""WP-5. An Optimism internal credit of 0.6 ETH alerts as evm_internal. Offline only."""

from __future__ import annotations

import importlib.util
import json
import socket
from pathlib import Path
from urllib.parse import urlsplit

import httpx
import pytest

from guardian.sources.fixtures import RawResponse

_SPEC = importlib.util.spec_from_file_location(
    "mcm_free_tier_support",
    Path(__file__).with_name("test_mcm_free_tier_catchup.py"),
)
_FREE = importlib.util.module_from_spec(_SPEC)
assert _SPEC is not None and _SPEC.loader is not None
_SPEC.loader.exec_module(_FREE)

ADDRESS_A = _FREE.ADDRESS_A
OTHER = _FREE.OTHER
WALLET_A = _FREE.WALLET_A
_account = _FREE._account
_cli = _FREE._cli
_desk = _FREE._desk

WEI_06 = 6 * 10**17
TX = "0x" + "cd" * 32


@pytest.fixture(autouse=True)
def _block_network(monkeypatch: pytest.MonkeyPatch) -> None:
    def blocked(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("network is disabled")

    monkeypatch.setattr(socket, "socket", blocked)
    monkeypatch.setattr(socket, "create_connection", blocked)
    monkeypatch.setattr(socket, "getaddrinfo", blocked)
    monkeypatch.setattr(httpx, "Client", blocked)


def test_op_internal_credit_alerts_with_evm_internal_origin(
    tmp_path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    def _only_optimism(document: dict) -> None:
        document["plugins"] = []
        document["enabled_networks"] = ["optimism-mainnet"]
        document["routing"] = [row for row in document["routing"] if row.get("network") == "optimism-mainnet"]
        document["polling"]["bootstrap_origin"] = 1

    desk = _desk(
        tmp_path,
        monkeypatch,
        [_account(WALLET_A, ADDRESS_A, ("optimism-mainnet",))],
        mutate=_only_optimism,
    )
    urls: list[str] = []
    inner = desk.backend.send

    def send(call: object) -> RawResponse:
        url = str(getattr(call, "url", ""))
        if "/internal-transactions" in url:
            urls.append(url)
            body = {
                "items": [{
                    "block_number": 12,
                    "error": None,
                    "from": {"hash": OTHER},
                    "index": 1,
                    "success": True,
                    "to": {"hash": ADDRESS_A},
                    "transaction_hash": TX,
                    "type": "call",
                    "value": str(WEI_06),
                }],
                "next_page_params": None,
            }
            return RawResponse(200, {}, json.dumps(body).encode())
        return inner(call)

    desk.backend.send = send
    report = _cli(capsys, desk)
    alerts = [
        row for row in report["decisions"]
        if row.get("transfer_origin") == "evm_internal" and row.get("alert") is True
    ]
    assert len(alerts) == 1
    assert alerts[0]["direction"] == "in"
    assert alerts[0]["network"] == "optimism-mainnet"
    assert int(alerts[0]["raw_amount"]) == WEI_06
    assert urls
    path = urlsplit(urls[0]).path
    query = urlsplit(urls[0]).query
    assert path.endswith("/internal-transactions")
    assert "/api/v2/addresses/" in path
    assert "limit=" not in query
    assert "api.blockscout.com" not in urls[0]
    assert all("apikey=" not in url.lower() for url in urls)
