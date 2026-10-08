"""MCM-W2-03. Failover stays inside the declared capability and does not invent a quorum."""

from __future__ import annotations

import pytest

from guardian.errors import SecurityError
from guardian.security.transport_policy import Operation
from guardian.sources.budget import BudgetExhausted
from guardian.sources.capabilities import load_source_capabilities
from guardian.sources.fixtures import RawResponse
from guardian.sources.router import SourceRouter, independent_source_count, require_independent_quorum
from mcm_w2_support import SYNTH, budget, harness, install, json_body, source


def _balance(value: str = "0x2") -> RawResponse:
    return json_body({"id": 1, "jsonrpc": "2.0", "result": value})


def _down() -> RawResponse:
    return RawResponse(500, {}, b"down")


def _pair():
    transport, backend, _clock, _sleep = harness(
        {
            "primary": source("evm_rpc", "PRIMARY_URL", "rpc"),
            "scan": source("etherscan_v2", "SCAN_URL", "scan"),
            "secondary": source("evm_rpc", "SECONDARY_URL", "rpc"),
        },
        {"rpc": budget(retry_attempts=0), "scan": budget(retry_attempts=0)},
        {
            "PRIMARY_URL": "https://primary.example.invalid/rpc",
            "SCAN_URL": "https://scan.example.invalid",
            "SECONDARY_URL": "https://secondary.example.invalid/rpc",
        },
    )
    catalog = load_source_capabilities()
    adapters = {"primary": "evm_rpc", "scan": "etherscan_v2", "secondary": "evm_rpc"}
    routing = [
        {
            "capability": "balances.native",
            "network": "ethereum-mainnet",
            "source_ids": ["primary", "secondary"],
        },
        {
            "capability": "history.native_internal",
            "network": "ethereum-mainnet",
            "source_ids": ["scan", "primary"],
        },
    ]

    def operation_for(source_id: str) -> Operation:
        adapter = adapters[source_id]
        if adapter == "etherscan_v2":
            return Operation(
                "rest",
                "etherscan_v2",
                http_method="GET",
                path="/v2/api",
                query={"action": "txlistinternal", "address": SYNTH, "module": "account"},
            )
        return Operation("jsonrpc", "evm_rpc", method="eth_getBalance", params=[SYNTH, "latest"])

    router = SourceRouter(routing, adapters, catalog, lambda source_id, operation: transport.request(source_id, operation))
    return transport, backend, router, operation_for


def test_mcm_w2_03_primary_failure_uses_secondary_for_shared_capability():
    transport, backend, router, operation_for = _pair()
    balance = operation_for("primary")
    install(transport, "primary", balance, _down())
    install(transport, "secondary", operation_for("secondary"), _balance("0x2"))
    result = router.fetch("ethereum-mainnet", "balances.native", operation_for)
    assert result.degraded is True
    assert result.source_id == "secondary"
    assert result.quality == "partial"
    assert result.reason_codes == ("http_5xx",)
    assert result.source_ids_tried == ("primary", "secondary")
    assert int(result.body["result"], 16) == 2
    assert set(result.as_dict()) == {
        "capability",
        "degraded",
        "quality",
        "reason_codes",
        "source_id",
        "source_ids_tried",
    }
    assert len(backend.calls) == 2


def test_mcm_w2_03_fallback_without_capability_is_not_complete():
    transport, backend, router, operation_for = _pair()
    install(transport, "scan", operation_for("scan"), _down())
    result = router.fetch("ethereum-mainnet", "history.native_internal", operation_for)
    assert result.quality == "unknown"
    assert result.quality != "complete_in_declared_scope"
    assert "fallback_lacks_capability" in result.reason_codes
    assert result.body is None
    assert result.body != []
    assert result.body != 0
    assert len(backend.calls) == 1


def test_mcm_w2_03_all_sources_fail_returns_error_not_empty():
    transport, backend, router, operation_for = _pair()
    install(transport, "primary", operation_for("primary"), _down())
    install(transport, "secondary", operation_for("secondary"), _down())
    previous = 5
    result = router.fetch("ethereum-mainnet", "balances.native", operation_for)
    assert result.quality == "error"
    assert result.body is None
    assert result.body != []
    assert result.body != 0
    assert result.degraded is True
    assert previous == 5
    assert len(backend.calls) == 2
    with pytest.raises(SecurityError) as caught:
        router.fetch(
            "ethereum-mainnet",
            "balances.native",
            lambda source_id: Operation("jsonrpc", "evm_rpc", method="eth_sendRawTransaction", params=["0x01"]),
        )
    assert caught.value.reason == "method_denied"
    assert len(backend.calls) == 2

    def exhausted(_source_id: str, _operation: Operation):
        raise BudgetExhausted("daily_cap")

    blocked = SourceRouter(
        [{"capability": "balances.native", "network": "ethereum-mainnet", "source_ids": ["primary", "secondary"]}],
        {"primary": "evm_rpc", "secondary": "evm_rpc"},
        load_source_capabilities(),
        exhausted,
    )
    with pytest.raises(BudgetExhausted):
        blocked.fetch("ethereum-mainnet", "balances.native", operation_for)


def test_mcm_w2_03_same_backend_is_not_an_independent_quorum():
    transport, _backend, _clock, _sleep = harness(
        {
            "scan": source("etherscan_v2", "SCAN_URL", "scan"),
            "v2": source("toncenter_v2", "TON_V2_URL", "ton"),
            "v3": source("toncenter_v3", "TON_V3_URL", "ton"),
        },
        {"scan": budget(retry_attempts=0), "ton": budget(retry_attempts=0)},
        {
            "SCAN_URL": "https://scan.example.invalid",
            "TON_V2_URL": "https://v2.example.invalid",
            "TON_V3_URL": "https://v3.example.invalid",
        },
    )
    assert transport.endpoints.get("v2").endpoint != transport.endpoints.get("v3").endpoint
    catalog = load_source_capabilities()
    backends = {
        "scan": catalog.backend("etherscan_v2"),
        "v2": catalog.backend("toncenter_v2"),
        "v3": catalog.backend("toncenter_v3"),
    }
    assert backends["v2"] == backends["v3"] == "toncenter"
    assert independent_source_count(["v2", "v3"], backends) == 1
    with pytest.raises(SecurityError) as caught:
        require_independent_quorum(["v2", "v3"], backends, minimum=2)
    assert caught.value.reason == "quorum_not_independent"
    assert require_independent_quorum(["v2", "scan"], backends, minimum=2) == 2


def test_mcm_w2_03_missing_route_is_unknown_not_complete():
    from guardian.sources.router import SourceRouter

    calls: list = []
    router = SourceRouter([], {}, None, lambda source_id, op: calls.append(source_id))
    result = router.fetch("ethereum-mainnet", "history.native_internal", lambda _sid: None)
    assert result.quality == "unknown"
    assert result.reason_codes == ("no_route",)
    assert result.source_id is None and calls == []
