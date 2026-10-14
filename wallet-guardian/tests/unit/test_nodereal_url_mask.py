"""NodeReal /v1/<key> stays out of error text and budget.providers.

The key below is a fake unit-test string. It is not a live credential.
"""

from __future__ import annotations

import json
import socket

import httpx
import pytest

from guardian.chains.evm.adapter import evm_rpc
from guardian.security.redact import redact
from guardian.sources.budget import BudgetBook
from guardian.sources.fixtures import RawResponse
from guardian.sources.provenance import CallLedger
from guardian.sources.transport import ReadOnlyTransport, build_endpoint_registry

_URL = "https://bsc-mainnet.nodereal.io/v1/THISISFAKEKEY12"
_KEY = "THISISFAKEKEY12"


@pytest.fixture(autouse=True)
def _block_network(monkeypatch: pytest.MonkeyPatch) -> None:
    def blocked(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("network is disabled")

    monkeypatch.setattr(socket, "socket", blocked)
    monkeypatch.setattr(socket, "create_connection", blocked)
    monkeypatch.setattr(socket, "getaddrinfo", blocked)
    monkeypatch.setattr(httpx, "Client", blocked)


def test_nodereal_v1_path_keeps_the_host_and_masks_the_key() -> None:
    masked = redact(f"rpc failed {_URL} after timeout")
    assert _URL not in masked
    assert _KEY not in masked
    assert "https://bsc-mainnet.nodereal.io/v1/***" in masked
    assert masked.index("https://bsc-mainnet.nodereal.io/v1/***") < masked.index("after timeout")


class _Echo:
    def send(self, call: object) -> RawResponse:
        url = str(getattr(call, "url", ""))
        body = json.dumps({"error": {"code": -32000, "message": url}}).encode()
        return RawResponse(500, {"x-ratelimit-limit": url, "content-type": "application/json"}, body)


def test_error_text_and_budget_providers_omit_the_nodereal_url() -> None:
    document = {
        "budgets": {
            "nodereal-key": {
                "max_concurrency": 1,
                "max_requests_per_day": 100,
                "max_rps": 10,
                "retry_attempts": 0,
            },
        },
        "sources": {
            "bsc-logs-nodereal": {
                "adapter": "evm_rpc",
                "budget_group": "nodereal-key",
                "enabled": True,
                "endpoint_env": "NODEREAL_BSC_URL",
            },
        },
    }
    endpoints = build_endpoint_registry(document, secrets={"NODEREAL_BSC_URL": _URL})
    book = BudgetBook.from_document(document["budgets"], clock=lambda: 0.0, sleep=lambda _seconds: None)
    ledger = CallLedger()
    transport = ReadOnlyTransport(endpoints, book, _Echo(), resolve=lambda _host: ["1.1.1.1"])
    transport.provenance = ledger
    result = transport.request("bsc-logs-nodereal", evm_rpc("eth_blockNumber", []))
    detail = result.detail
    providers = json.dumps(ledger.as_dict()["providers"])
    error = redact(f"{detail} {_URL}")
    assert _URL not in detail
    assert _KEY not in detail
    assert "https://bsc-mainnet.nodereal.io/v1/***" in detail
    assert _URL not in providers
    assert _KEY not in providers
    assert _URL not in error
    assert _KEY not in error
    assert result.body is None or _KEY not in json.dumps(result.body)


class _DataEcho:
    def __init__(self, data: object) -> None:
        self.data = data

    def send(self, call: object) -> RawResponse:
        payload = {
            "error": {"code": -32000, "data": self.data, "message": "fail"},
            "id": 1,
            "jsonrpc": "2.0",
        }
        return RawResponse(200, {"content-type": "application/json"}, json.dumps(payload).encode())


@pytest.mark.parametrize(
    "data",
    [
        _URL,
        {"url": _URL, "nested": [_URL, {"inner": _URL}]},
        [_URL, {"note": _URL}],
    ],
)
def test_error_data_masks_nodereal_urls_in_strings_dicts_and_lists(data: object) -> None:
    document = {
        "budgets": {
            "nodereal-key": {
                "max_concurrency": 1,
                "max_requests_per_day": 100,
                "max_rps": 10,
                "retry_attempts": 0,
            },
        },
        "sources": {
            "bsc-logs-nodereal": {
                "adapter": "evm_rpc",
                "budget_group": "nodereal-key",
                "enabled": True,
                "endpoint_env": "NODEREAL_BSC_URL",
            },
        },
    }
    endpoints = build_endpoint_registry(document, secrets={"NODEREAL_BSC_URL": _URL})
    book = BudgetBook.from_document(document["budgets"], clock=lambda: 0.0, sleep=lambda _seconds: None)
    transport = ReadOnlyTransport(endpoints, book, _DataEcho(data), resolve=lambda _host: ["1.1.1.1"])
    result = transport.request("bsc-logs-nodereal", evm_rpc("eth_blockNumber", []))
    blob = json.dumps(result.body)
    assert _URL not in blob
    assert _KEY not in blob
    assert "https://bsc-mainnet.nodereal.io/v1/***" in blob
