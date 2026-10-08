"""Credential header allowlist. Offline. The secret stays out of repr, logs, and errors."""

from __future__ import annotations

import base64
import json
import logging
import socket

import httpx
import pytest

from guardian.errors import SecurityError
from guardian.pricing.catalog import load_risk_sources
from guardian.risk.chainabuse import ChainabuseClient
from guardian.risk.goplus import GoPlusClient
from guardian.security.transport_policy import Operation, assert_operation_allowed
from guardian.sources.budget import BudgetBook
from guardian.sources.fixtures import RawResponse
from guardian.sources.transport import ReadOnlyTransport, build_endpoint_registry, pinned_request

GP_SECRET = "goplus-header-secret-value"
CA_SECRET = "chainabuse-header-secret-value"
ADDRESS = "0x0000000000000000000000000000000000000001"
NOW = 1_700_000_000.0


@pytest.fixture(autouse=True)
def _block_network(monkeypatch: pytest.MonkeyPatch) -> None:
    def blocked(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("network is disabled")

    monkeypatch.setattr(socket, "socket", blocked)
    monkeypatch.setattr(socket, "create_connection", blocked)
    monkeypatch.setattr(socket, "getaddrinfo", blocked)
    monkeypatch.setattr(httpx, "Client", blocked)


class Recording:
    def __init__(self, responses: list[RawResponse]) -> None:
        self.responses = list(responses)
        self.calls: list[object] = []

    def send(self, call: object) -> RawResponse:
        self.calls.append(call)
        if self.responses:
            return self.responses.pop(0)
        return RawResponse(200, {}, b"{}")


def test_mcm_w9_hdr_goplus_and_chainabuse_send_credential_headers(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    goplus_body = json.dumps({"code": 1, "message": "OK", "result": {ADDRESS: {"is_honeypot": "1"}}}).encode()
    abuse_body = json.dumps({"reports": []}).encode()
    backend = Recording([
        RawResponse(200, {"x-incoming": GP_SECRET, "set-cookie": CA_SECRET}, goplus_body),
        RawResponse(403, {"x-incoming": GP_SECRET}, b""),
        RawResponse(200, {"x-incoming": CA_SECRET}, abuse_body),
    ])
    transport = _transport(backend, gp=GP_SECRET, ca=CA_SECRET)
    client = GoPlusClient(
        transport,
        "goplus",
        load_risk_sources(),
        {"GOPLUS_API_KEY": GP_SECRET},
        clock=lambda: NOW,
        sleep=lambda _seconds: None,
        random=lambda: 0.0,
    )
    assert client.credential_status == "credential_header"
    flagged = client.read(function="token_security", network_id="ethereum-mainnet", target=ADDRESS)
    assert flagged.status == "flagged"
    assert flagged.safe is False
    first = backend.calls[0]
    assert _authorization(first) == f"Bearer {GP_SECRET}"
    sent = pinned_request(first)[1]
    assert sent["authorization"] == f"Bearer {GP_SECRET}"
    assert "x-incoming" not in {key.lower() for key in sent}
    assert "set-cookie" not in {key.lower() for key in sent}
    rejected = client.read(function="token_security", network_id="ethereum-mainnet", target=ADDRESS)
    assert rejected.status == "unknown"
    assert rejected.reason == "credential_rejected"
    assert rejected.safe is False
    second = pinned_request(backend.calls[1])[1]
    assert second["authorization"] == f"Bearer {GP_SECRET}"
    assert "x-incoming" not in {key.lower() for key in second}

    abuse = ChainabuseClient(transport, "chainabuse", {"CHAINABUSE_API_KEY": CA_SECRET})
    read = abuse.lookup(ADDRESS)
    assert read.requested is True
    assert read.safe is False
    assert read.reports is None
    assert read.status == "unknown"
    assert read.reason == "empty_not_clear"
    token = base64.b64encode(f"{CA_SECRET}:".encode("utf-8")).decode("ascii")
    assert _authorization(backend.calls[2]) == f"Basic {token}"
    assert GP_SECRET not in caplog.text
    assert CA_SECRET not in caplog.text
    _assert_masked(transport, backend.calls)


def test_mcm_w9_hdr_rejects_unlisted_and_value_bearing_headers() -> None:
    with pytest.raises(SecurityError) as valued:
        Operation(
            protocol="rest",
            adapter="goplus",
            http_method="GET",
            path="/api/v1/token_security/1",
            headers={"authorization": GP_SECRET},
        )
    assert valued.value.reason == "header_value_not_allowed"
    assert GP_SECRET not in str(valued.value)
    with pytest.raises(SecurityError) as paired:
        Operation(
            protocol="rest",
            adapter="goplus",
            http_method="GET",
            path="/api/v1/token_security/1",
            headers=(("authorization", GP_SECRET),),
        )
    assert paired.value.reason == "header_value_not_allowed"
    assert GP_SECRET not in str(paired.value)
    foreign = Operation(
        protocol="jsonrpc",
        adapter="evm_rpc",
        method="eth_chainId",
        headers=("authorization",),
    )
    with pytest.raises(SecurityError) as evm:
        assert_operation_allowed(foreign)
    assert evm.value.reason == "header_not_allowed"
    named = Operation(
        protocol="rest",
        adapter="goplus",
        http_method="GET",
        path="/api/v1/token_security/1",
        headers=("cookie",),
    )
    with pytest.raises(SecurityError) as cookie:
        assert_operation_allowed(named)
    assert cookie.value.reason == "header_not_allowed"
    host = Operation(
        protocol="rest",
        adapter="chainabuse",
        http_method="GET",
        path="/v0/reports",
        headers=("host",),
    )
    with pytest.raises(SecurityError) as host_error:
        assert_operation_allowed(host)
    assert host_error.value.reason == "header_not_allowed"
    backend = Recording([])
    transport = _transport(backend, gp=GP_SECRET, ca=CA_SECRET)
    with pytest.raises(SecurityError):
        transport.request("goplus", foreign)
    assert backend.calls == []


def test_mcm_w9_hdr_missing_credential_sends_no_request_and_secret_stays_masked(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    backend = Recording([])
    transport = _transport(backend, gp="", ca=None)
    operation = Operation(
        protocol="rest",
        adapter="goplus",
        http_method="GET",
        path="/api/v1/token_security/1",
        headers=("authorization",),
    )
    result = transport.request("goplus", operation)
    assert result.reason == "credential_missing"
    assert result.blocked is True
    assert backend.calls == []
    abuse = ChainabuseClient(transport, "chainabuse", {})
    blocked = abuse.lookup(ADDRESS)
    assert blocked.reason == "credential_missing"
    assert blocked.requested is False
    assert blocked.safe is False
    assert blocked.reports is None
    assert backend.calls == []
    assert GP_SECRET not in caplog.text
    assert CA_SECRET not in caplog.text
    _assert_masked(transport, [])


def _transport(backend: Recording, *, gp: str, ca: str | None) -> ReadOnlyTransport:
    sources = {
        "chainabuse": {
            "adapter": "chainabuse",
            "budget_group": "chainabuse",
            "endpoint_env": "CHAINABUSE_API_URL",
        },
        "goplus": {
            "adapter": "goplus",
            "budget_group": "goplus",
            "credential_env": "GOPLUS_API_KEY",
            "endpoint_env": "GOPLUS_API_URL",
        },
    }
    secrets = {
        "CHAINABUSE_API_URL": "https://chainabuse.example.invalid",
        "GOPLUS_API_URL": "https://goplus.example.invalid",
    }
    if gp:
        secrets["GOPLUS_API_KEY"] = gp
    if ca is not None:
        sources["chainabuse"]["credential_env"] = "CHAINABUSE_API_KEY"
        if ca:
            secrets["CHAINABUSE_API_KEY"] = ca
    endpoints = build_endpoint_registry({"sources": sources}, secrets=secrets)
    book = BudgetBook.from_document(
        {
            "chainabuse": {"max_concurrency": 2, "max_requests_per_day": 20, "max_rps": 100, "retry_attempts": 0},
            "goplus": {"max_concurrency": 2, "max_requests_per_day": 20, "max_rps": 100, "retry_attempts": 0},
        },
        clock=lambda: NOW,
        sleep=lambda _seconds: None,
    )
    return ReadOnlyTransport(endpoints, book, backend, resolve=lambda _host: ["1.1.1.1"])


def _authorization(call: object) -> str:
    headers = getattr(call, "credential_headers", ())
    assert len(headers) == 1
    name, value = headers[0]
    assert name == "authorization"
    revealed = value.reveal()
    assert GP_SECRET not in repr(call)
    assert CA_SECRET not in repr(call)
    assert GP_SECRET not in str(call)
    assert CA_SECRET not in str(call)
    return revealed


def _assert_masked(transport: ReadOnlyTransport, calls: list[object]) -> None:
    rendered = repr(transport.endpoints.sources["goplus"]) + repr(transport.endpoints.sources["chainabuse"])
    assert GP_SECRET not in rendered
    assert CA_SECRET not in rendered
    for call in calls:
        assert GP_SECRET not in repr(call)
        assert CA_SECRET not in repr(call)
        assert GP_SECRET not in str(call)
        assert CA_SECRET not in str(call)
