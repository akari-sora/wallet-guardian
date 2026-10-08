"""MCM-W2-01. Writes are rejected before the backend. Reads return a non-zero fixture."""

from __future__ import annotations

import pytest

from guardian.configuration import _ADAPTER_FAMILY
from guardian.errors import SecurityError
from guardian.security.transport_policy import ADAPTER_PROTOCOL, Operation
from guardian.sources.capabilities import load_source_capabilities
from guardian.sources.fixtures import PreparedCall
from guardian.sources.transport import HttpxBackend, TransportError
from mcm_w2_support import SYNTH, harness, install, json_body, source, text_body


def _rpc(group: str = "readers"):
    transport, backend, _clock, _sleep = harness(
        {"rpc": source("evm_rpc", "RPC_URL", group)},
        {group: {"max_concurrency": 2, "max_requests_per_day": 20, "max_rps": 1000, "retry_attempts": 0}},
        {"RPC_URL": "https://rpc.example.invalid/rpc"},
    )
    return transport, backend


def _read(method: str = "eth_getBalance"):
    return Operation("jsonrpc", "evm_rpc", method=method, params=[SYNTH, "latest"])


def test_mcm_w2_01_evm_read_returns_nonzero_and_write_does_not_call_backend():
    transport, backend = _rpc()
    operation = _read()
    install(transport, "rpc", operation, json_body({"id": 1, "jsonrpc": "2.0", "result": "0x1"}))
    result = transport.request("rpc", operation)
    assert int(result.body["result"], 16) == 1
    assert result.body["result"] != "0x0"
    before = len(backend.calls)
    with pytest.raises(SecurityError) as caught:
        transport.request("rpc", Operation("jsonrpc", "evm_rpc", method="eth_sendRawTransaction", params=["0x01"]))
    assert caught.value.reason == "method_denied"
    assert len(backend.calls) == before
    assert transport.budgets.group("readers").requests_today == 1


def test_mcm_w2_01_method_match_rejects_case_space_and_prefix():
    transport, backend = _rpc()
    for method in ("ETH_getBalance", "eth_getBalance ", " eth_getBalance", "eth_getBalanceExtra"):
        with pytest.raises(SecurityError) as caught:
            transport.request("rpc", Operation("jsonrpc", "evm_rpc", method=method, params=[]))
        assert caught.value.reason == "method_denied"
    assert backend.calls == []


def test_mcm_w2_01_jsonrpc_batch_rejects_one_write_and_allows_reads():
    transport, backend = _rpc()
    denied = Operation("jsonrpc", "evm_rpc", batch=(
        {"id": 1, "jsonrpc": "2.0", "method": "eth_getBalance", "params": [SYNTH, "latest"]},
        {"id": 2, "jsonrpc": "2.0", "method": "eth_sendRawTransaction", "params": ["0x01"]},
    ))
    with pytest.raises(SecurityError) as caught:
        transport.request("rpc", denied)
    assert caught.value.reason == "batch_rejected"
    assert backend.calls == []
    for malformed in ((), ({"method": 1},), ("eth_getBalance",)):
        with pytest.raises(SecurityError) as caught:
            transport.request("rpc", Operation("jsonrpc", "evm_rpc", batch=malformed))
        assert caught.value.reason == "batch_malformed"
    assert backend.calls == []
    allowed = Operation("jsonrpc", "evm_rpc", batch=(
        {"id": 1, "jsonrpc": "2.0", "method": "eth_chainId", "params": []},
        {"id": 2, "jsonrpc": "2.0", "method": "eth_getBalance", "params": [SYNTH, "latest"]},
    ))
    install(transport, "rpc", allowed, json_body([
        {"id": 1, "jsonrpc": "2.0", "result": "0x1"},
        {"id": 2, "jsonrpc": "2.0", "result": "0x2"},
    ]))
    result = transport.request("rpc", allowed)
    assert int(result.body[0]["result"], 16) > 0
    assert len(backend.calls) == 1


def test_mcm_w2_01_solana_write_rejected_and_read_returns_nonzero():
    transport, backend, _clock, _sleep = harness(
        {"sol": source("solana_rpc", "SOL_URL", "sol")},
        {"sol": {"max_concurrency": 1, "max_requests_per_day": 10, "max_rps": 1000, "retry_attempts": 0}},
        {"SOL_URL": "https://sol.example.invalid/rpc"},
    )
    for method in ("sendTransaction", "simulateTransaction", "requestAirdrop", "SendTransaction", "getBalance "):
        with pytest.raises(SecurityError):
            transport.request("sol", Operation("jsonrpc", "solana_rpc", method=method, params=[]))
    assert backend.calls == []
    operation = Operation("jsonrpc", "solana_rpc", method="getBalance", params=["synthetic"])
    install(transport, "sol", operation, json_body({"id": 1, "jsonrpc": "2.0", "result": {"value": 1}}))
    result = transport.request("sol", operation)
    assert result.body["result"]["value"] == 1
    assert len(backend.calls) == 1


def test_mcm_w2_01_nodereal_transfer_read_returns_nonzero():
    transport, backend, _clock, _sleep = harness(
        {"nr": source("nodereal_transfers", "NR_URL", "nr")},
        {"nr": {"max_concurrency": 1, "max_requests_per_day": 10, "max_rps": 1000, "retry_attempts": 0}},
        {"NR_URL": "https://nr.example.invalid/rpc"},
    )
    operation = Operation("jsonrpc", "nodereal_transfers", method="nr_getAssetTransfers", params=[{"category": ["external"]}])
    install(transport, "nr", operation, json_body({"id": 1, "jsonrpc": "2.0", "result": {"transfers": [{"value": "5"}]}}))
    result = transport.request("nr", operation)
    assert result.body["result"]["transfers"][0]["value"] == "5"
    assert len(backend.calls) == 1
    with pytest.raises(SecurityError):
        transport.request("nr", Operation("jsonrpc", "nodereal_transfers", method="nr_getAssetTransfersExtra", params=[]))
    assert len(backend.calls) == 1


def test_mcm_w2_01_etherscan_proxy_send_rejected_and_balance_nonzero():
    transport, backend, _clock, _sleep = harness(
        {"scan": source("etherscan_v2", "SCAN_URL", "scan")},
        {"scan": {"max_concurrency": 1, "max_requests_per_day": 10, "max_rps": 1000, "retry_attempts": 0}},
        {"SCAN_URL": "https://api.example.invalid"},
    )
    denied = Operation(
        "rest",
        "etherscan_v2",
        http_method="GET",
        path="/v2/api",
        query={"action": "eth_sendRawTransaction", "module": "proxy"},
    )
    with pytest.raises(SecurityError) as caught:
        transport.request("scan", denied)
    assert caught.value.reason == "rest_denied"
    lowered = Operation(
        "rest",
        "etherscan_v2",
        http_method="get",
        path="/v2/api",
        query={"action": "balance", "address": SYNTH, "module": "account"},
    )
    with pytest.raises(SecurityError) as caught:
        transport.request("scan", lowered)
    assert caught.value.reason == "rest_denied"
    assert backend.calls == []
    operation = Operation(
        "rest",
        "etherscan_v2",
        http_method="GET",
        path="/v2/api",
        query={"action": "balance", "address": SYNTH, "module": "account"},
    )
    install(transport, "scan", operation, json_body({"message": "OK", "result": "1", "status": "1"}))
    result = transport.request("scan", operation)
    assert int(result.body["result"]) == 1
    assert len(backend.calls) == 1


def test_mcm_w2_01_esplora_broadcast_rejected_and_height_nonzero():
    transport, backend, _clock, _sleep = harness(
        {"btc": source("esplora", "BTC_URL", "btc")},
        {"btc": {"max_concurrency": 1, "max_requests_per_day": 10, "max_rps": 1000, "retry_attempts": 0}},
        {"BTC_URL": "https://btc.example.invalid"},
    )
    with pytest.raises(SecurityError):
        transport.request("btc", Operation("rest", "esplora", http_method="POST", path="/tx", body="00"))
    assert backend.calls == []
    operation = Operation("rest", "esplora", http_method="GET", path="/blocks/tip/height")
    install(transport, "btc", operation, text_body("840000"))
    result = transport.request("btc", operation)
    assert int(result.body) == 840000
    assert len(backend.calls) == 1


def test_mcm_w2_01_tron_broadcast_rejected_and_account_nonzero():
    transport, backend, _clock, _sleep = harness(
        {"tron": source("tron_grid", "TRON_URL", "tron")},
        {"tron": {"max_concurrency": 1, "max_requests_per_day": 10, "max_rps": 1000, "retry_attempts": 0}},
        {"TRON_URL": "https://tron.example.invalid"},
    )
    for path in (
        "/wallet/broadcasttransaction",
        "/wallet/broadcasthex",
        "/wallet/createtransaction",
        "/wallet/triggersmartcontract",
        "/wallet/easytransferbyprivate",
        "/wallet/gettransactionsign",
    ):
        with pytest.raises(SecurityError):
            transport.request("tron", Operation("rest", "tron_grid", http_method="POST", path=path, body={}))
    assert backend.calls == []
    operation = Operation("rest", "tron_grid", http_method="POST", path="/wallet/getaccount", body={"address": "synthetic"})
    install(transport, "tron", operation, json_body({"balance": 1}))
    result = transport.request("tron", operation)
    assert result.body["balance"] == 1
    assert len(backend.calls) == 1
    smuggled = Operation(
        "rest",
        "tron_grid",
        http_method="POST",
        path="/wallet/getaccount",
        body={"address": "synthetic", "raw_data_hex": "aa", "signature": ["bb"]},
    )
    with pytest.raises(SecurityError) as caught:
        transport.request("tron", smuggled)
    assert caught.value.reason == "signed_payload"
    assert len(backend.calls) == 1


def test_mcm_w2_01_ton_submit_rejected_and_read_nonzero():
    transport, backend, _clock, _sleep = harness(
        {"ton": source("toncenter_v2", "TON_URL", "ton")},
        {"ton": {"max_concurrency": 1, "max_requests_per_day": 10, "max_rps": 1000, "retry_attempts": 0}},
        {"TON_URL": "https://ton.example.invalid"},
    )
    for path in ("/api/v2/sendBoc", "/api/v2/sendBocReturnHash", "/api/v2/sendQuery", "/api/v2/estimateFee"):
        with pytest.raises(SecurityError):
            transport.request("ton", Operation("rest", "toncenter_v2", http_method="POST", path=path, body={}))
    assert backend.calls == []
    operation = Operation("rest", "toncenter_v2", http_method="GET", path="/api/v2/getAddressInformation")
    install(transport, "ton", operation, json_body({"balance": "1"}))
    result = transport.request("ton", operation)
    assert int(result.body["balance"]) == 1
    assert len(backend.calls) == 1


def test_mcm_w2_01_graphql_mutation_rejected_and_query_nonzero():
    transport, backend, _clock, _sleep = harness(
        {"sui": source("sui_graphql", "SUI_URL", "sui")},
        {"sui": {"max_concurrency": 1, "max_requests_per_day": 10, "max_rps": 1000, "retry_attempts": 0}},
        {"SUI_URL": "https://sui.example.invalid/graphql"},
    )
    documents = (
        "mutation { send }",
        "query Read { amount } mutation Write { send }",
        "subscription { ticks }",
    )
    for document in documents:
        with pytest.raises(SecurityError) as caught:
            transport.request("sui", Operation("graphql", "sui_graphql", document=document, operation_name="Read"))
        assert caught.value.reason in {"graphql_mutation", "graphql_subscription"}
    with pytest.raises(SecurityError) as caught:
        transport.request("sui", Operation("graphql", "sui_graphql", documents=("query { amount }", "mutation { send }")))
    assert caught.value.reason == "graphql_mutation"
    with pytest.raises(SecurityError) as caught:
        transport.request("sui", Operation("graphql", "sui_graphql", document="query { amount }", body={"signatures": ["aa"], "tx_bytes": "bb"}))
    assert caught.value.reason == "signed_payload"
    assert backend.calls == []
    operation = Operation("graphql", "sui_graphql", document="query { amount }")
    install(transport, "sui", operation, json_body({"data": {"amount": "1"}}))
    result = transport.request("sui", operation)
    assert int(result.body["data"]["amount"]) == 1
    assert len(backend.calls) == 1


def test_mcm_w2_01_graphql_comment_and_string_do_not_count_as_mutation():
    transport, backend, _clock, _sleep = harness(
        {"sui": source("sui_graphql", "SUI_URL", "sui")},
        {"sui": {"max_concurrency": 1, "max_requests_per_day": 10, "max_rps": 1000, "retry_attempts": 0}},
        {"SUI_URL": "https://sui.example.invalid/graphql"},
    )
    document = '# mutation\nquery { mutation note(text: "mutation") block: """\nmutation\n""" }\n'
    operation = Operation("graphql", "sui_graphql", document=document)
    install(transport, "sui", operation, json_body({"data": {"amount": "4"}}))
    result = transport.request("sui", operation)
    assert int(result.body["data"]["amount"]) == 4
    assert len(backend.calls) == 1


def test_mcm_w2_01_aptos_anonymous_query_returns_nonzero():
    transport, backend, _clock, _sleep = harness(
        {"idx": source("aptos_indexer", "APT_URL", "apt")},
        {"apt": {"max_concurrency": 1, "max_requests_per_day": 10, "max_rps": 1000, "retry_attempts": 0}},
        {"APT_URL": "https://apt.example.invalid/graphql"},
    )
    operation = Operation("graphql", "aptos_indexer", document="{ amount }")
    install(transport, "idx", operation, json_body({"data": {"amount": "2"}}))
    result = transport.request("idx", operation)
    assert int(result.body["data"]["amount"]) == 2
    assert len(backend.calls) == 1


def test_mcm_w2_01_grpc_execute_rejected_and_read_returns_nonzero():
    transport, backend, _clock, _sleep = harness(
        {"sui": source("sui_grpc", "SUI_URL", "sui")},
        {"sui": {"max_concurrency": 1, "max_requests_per_day": 10, "max_rps": 1000, "retry_attempts": 0}},
        {"SUI_URL": "https://sui.example.invalid"},
    )
    for method in (
        "sui.rpc.v2.TransactionExecutionService/ExecuteTransaction",
        "sui.rpc.v2.TransactionExecutionService/SimulateTransaction",
        "sui.rpc.v2.LedgerService/GetObjectExtra",
    ):
        with pytest.raises(SecurityError) as caught:
            transport.request("sui", Operation("grpc", "sui_grpc", method=method))
        assert caught.value.reason == "grpc_denied"
    assert backend.calls == []
    operation = Operation("grpc", "sui_grpc", method="sui.rpc.v2.StateService/GetBalance")
    install(transport, "sui", operation, json_body({"balance": "1"}))
    result = transport.request("sui", operation)
    assert int(result.body["balance"]) == 1
    assert len(backend.calls) == 1
    with pytest.raises(TransportError) as caught:
        HttpxBackend().send(PreparedCall("sui", "GRPC", "https://sui.example.invalid", b"{}", "grpc"))
    assert caught.value.reason == "grpc_client_not_enabled"


def test_mcm_w2_01_capability_operations_match_policy_and_are_not_live():
    catalog = load_source_capabilities()
    assert set(catalog.adapters) == set(ADAPTER_PROTOCOL) == set(_ADAPTER_FAMILY)
    assert catalog.backend("toncenter_v2") == catalog.backend("toncenter_v3") == "toncenter"
    etherscan = catalog.get("etherscan_v2")
    assert "opbnb-mainnet" not in etherscan.networks
    assert "2026-10-12" in etherscan.notes
    assert etherscan.limits[0]["max_rps"] == 3
    assert etherscan.limits[0]["max_requests_per_day"] == 100000
    assert catalog.get("helius_parsed_events").limits[0]["parsed_events_credit_cost"] == 10
    for adapter, entry in catalog.adapters.items():
        assert entry.probe in {"documented", "untested"}
        assert "JSON-RPC" in entry.notes or adapter not in {"sui_graphql", "sui_grpc"}
        for name, status in entry.capabilities.items():
            assert status.probe in {"documented", "untested"}
            assert status.support != "implemented"
            if name == "permits.offchain_unused":
                assert status.support == "not_observable"
    assert "JSON-RPC" in catalog.get("sui_graphql").notes
    assert catalog.declares("evm_rpc", "ethereum-mainnet", "balances.native") is True
    assert catalog.declares("evm_rpc", "ethereum-mainnet", "history.native_internal") is False


def test_mcm_w2_01_graphql_variable_default_query_allowed_but_mutation_with_default_denied():
    from guardian.security.transport_policy import assert_operation_allowed

    query = 'query Read($owner: SuiAddress = "0x1", $first: Int = 5) { address(address: $owner) { balance { totalBalance } } }'
    assert_operation_allowed(Operation("graphql", "sui_graphql", document=query))
    mutation = 'mutation Run($tx: String = "AA==") { executeTransactionBlock(txBytes: $tx) { digest } }'
    with pytest.raises(SecurityError) as caught:
        assert_operation_allowed(Operation("graphql", "sui_graphql", document=mutation))
    assert caught.value.reason == "graphql_mutation"
    with pytest.raises(SecurityError) as caught:
        assert_operation_allowed(Operation("graphql", "sui_graphql", document="= { a }"))
    assert caught.value.reason == "graphql_malformed"


def test_mcm_w2_01_get_only_adapter_unknown_path_fails_closed_and_read_path_reaches_backend():
    transport, backend, _clock, _sleep = harness(
        {"scout": source("blockscout", "SCOUT_URL", "scout")},
        {"scout": {"max_concurrency": 2, "max_requests_per_day": 20, "max_rps": 1000, "retry_attempts": 0}},
        {"SCOUT_URL": "https://scout.example.invalid"},
    )
    read = Operation("rest", "blockscout", http_method="GET", path=f"/api/v2/addresses/{SYNTH}")
    install(transport, "scout", read, json_body({"coin_balance": "42"}))
    result = transport.request("scout", read)
    assert int(result.body["coin_balance"]) == 42
    before = len(backend.calls)
    for path in ("/api/v2/smart-contracts/verify", "/api/v2/anything", "/admin", "/api/v2/addresses/"):
        with pytest.raises(SecurityError) as caught:
            transport.request("scout", Operation("rest", "blockscout", http_method="GET", path=path))
        assert caught.value.reason == "rest_denied"
    assert len(backend.calls) == before
