"""Offline acceptance for MCM-W4. Synthetic fixtures only. No network.

MCM-W4-01 seven-chain-contract
MCM-W4-02 native-vs-logs
MCM-W4-03 pagination-and-limits
MCM-W4-04 token-accounting
"""

from __future__ import annotations

import json
import socket
from pathlib import Path

import pytest

from guardian.amounts import format_units
from guardian.chains.evm.accounting import (
    TRANSFER_BATCH_TOPIC,
    TRANSFER_SINGLE_TOPIC,
    TRANSFER_TOPIC,
    ZERO_ADDRESS,
    account_logs,
    hex_word,
    pad_address,
)
from guardian.chains.evm.adapter import (
    EvmAdapter,
    balance_operation,
    chain_id_operation,
    receipt_operation,
    token_log_operation,
    to_observation,
)
from guardian.chains.evm.receipts import OP_STACK
from guardian.errors import SecurityError
from guardian.events import success_alertable
from guardian.network_registry import load_registry
from guardian.security.redact import redact
from guardian.sources.blockscout import BlockscoutClient
from guardian.sources.budget import BudgetBook
from guardian.sources.capabilities import load_source_capabilities
from guardian.sources.etherscan import (
    ETHERSCAN_RESULT_WINDOW,
    EtherscanClient,
    resolve_credential,
)
from guardian.sources.fixtures import FixtureBackend, RawResponse, request_key
from guardian.sources.nodereal import (
    MAX_BLOCK_SPAN,
    MAX_COUNT,
    CATEGORY_CAPABILITY,
    NodeRealClient,
)
from guardian.sources.transport import ReadOnlyTransport, build_endpoint_registry

ROOT = Path(__file__).resolve().parents[2]
PUBLIC_IP = "1.1.1.1"
CREDENTIAL_ENV = "ETHERSCAN_API_KEY"
SYNTHETIC_KEY = "synthetic-key"
WALLET = "0x0000000000000000000000000000000000000001"
OTHER = "0x0000000000000000000000000000000000000002"
TOKEN = "0x00000000000000000000000000000000000000aa"
TOKEN_6 = "0x00000000000000000000000000000000000000ab"
TOKEN_8 = "0x00000000000000000000000000000000000000ac"
TOKEN_PLAIN = "0x00000000000000000000000000000000000000ad"
TOKEN_721 = "0x00000000000000000000000000000000000000bb"
TOKEN_1155 = "0x00000000000000000000000000000000000000cc"
NOW = "2026-10-08T00:00:00Z"
TENANT = "tenant-synthetic"
WALLET_ID = "11111111-1111-4111-8111-111111111111"
T0 = (
    ("ethereum-mainnet", 1),
    ("bsc-mainnet", 56),
    ("arbitrum-one", 42161),
    ("base-mainnet", 8453),
    ("optimism-mainnet", 10),
    ("polygon-pos", 137),
    ("avalanche-c", 43114),
)
ETHERSCAN_URL = "https://etherscan.example.invalid"
BLOCKSCOUT_URL = "https://blockscout.example.invalid"
RPC_URL = "https://rpc.example.invalid"
NODEREAL_URL = "https://nodereal.example.invalid"

assert ETHERSCAN_RESULT_WINDOW == 10_000
assert MAX_BLOCK_SPAN == 100_000
assert MAX_COUNT == 0x3E8
assert all(len(item) == 42 and item.startswith("0x00000000000000000000000000000000000000") for item in (
    WALLET, OTHER, TOKEN, TOKEN_6, TOKEN_8, TOKEN_PLAIN, TOKEN_721, TOKEN_1155,
))


@pytest.fixture(autouse=True)
def _offline(monkeypatch):
    def explode(*_args, **_kwargs):
        raise AssertionError("network is forbidden in MCM-W4 tests")

    monkeypatch.setattr(socket, "socket", explode)
    monkeypatch.setattr(socket, "create_connection", explode)
    monkeypatch.setattr(socket, "getaddrinfo", explode)
    import httpx

    monkeypatch.setattr(httpx, "Client", explode)


def _clock():
    now = [1_700_000_000.0]
    slept: list[float] = []

    def clock() -> float:
        return now[0]

    def sleep(seconds: float) -> None:
        slept.append(seconds)
        now[0] += seconds

    return slept, clock, sleep


def _resolve(_host: str) -> list[str]:
    return [PUBLIC_IP]


def world(*, retry_attempts: int = 2):
    slept, clock, sleep = _clock()
    sources = {}
    secrets = {}
    budgets = {}
    for source_id, adapter, endpoint in (
        ("etherscan", "etherscan_v2", ETHERSCAN_URL),
        ("blockscout", "blockscout", BLOCKSCOUT_URL),
        ("evm-rpc", "evm_rpc", RPC_URL),
        ("nodereal", "nodereal_transfers", NODEREAL_URL),
    ):
        env_name = source_id.upper().replace("-", "_") + "_ENDPOINT"
        sources[source_id] = {"adapter": adapter, "budget_group": source_id, "endpoint_env": env_name}
        secrets[env_name] = endpoint
        budgets[source_id] = {
            "max_concurrency": 4,
            "max_requests_per_day": 10_000,
            "max_rps": 1000,
            "retry_attempts": retry_attempts,
        }
    document = {"budgets": budgets, "sources": sources}
    endpoints = build_endpoint_registry(document, secrets=secrets)
    book = BudgetBook.from_document(budgets, clock=clock, sleep=sleep)
    backend = FixtureBackend({})
    transport = ReadOnlyTransport(endpoints, book, backend, resolve=_resolve)
    credential = resolve_credential({CREDENTIAL_ENV: SYNTHETIC_KEY}, CREDENTIAL_ENV)
    etherscan = EtherscanClient(
        transport,
        "etherscan",
        credential=credential,
        credential_env=CREDENTIAL_ENV,
        clock=clock,
        sleep=sleep,
        random=lambda: 0.0,
    )
    blockscout = BlockscoutClient(transport, "blockscout", clock=clock, sleep=sleep, random=lambda: 0.0)
    return transport, backend, slept, etherscan, blockscout, clock, sleep


def install(transport, source_id, operation, payload, status: int = 200, headers: dict | None = None) -> None:
    call = transport.encode(source_id, operation)
    if isinstance(payload, RawResponse):
        response = payload
    else:
        response = RawResponse(status, headers or {}, json.dumps(payload).encode("utf-8"))
    transport.backend.mapping[request_key(call.method, call.url, call.body)] = response


def install_seq(transport, source_id, operation, responses: list[RawResponse]) -> None:
    call = transport.encode(source_id, operation)
    transport.backend.mapping[request_key(call.method, call.url, call.body)] = list(responses)


def rpc_ok(result):
    return {"id": 1, "jsonrpc": "2.0", "result": result}


def registry():
    return load_registry(ROOT / "config" / "chains")


def tx_hash(prefix: str, chain_id: int) -> str:
    return "0x" + prefix * 31 + f"{chain_id % 256:02x}"


def erc20_log(tx: str, amount: int, *, token: str = TOKEN, sender: str = OTHER, receiver: str = WALLET, index: int = 0, source_id: str | None = None):
    log = {
        "address": token,
        "data": hex_word(amount),
        "logIndex": hex(index),
        "topics": [TRANSFER_TOPIC, pad_address(sender), pad_address(receiver)],
        "transactionHash": tx,
    }
    if source_id:
        log["source_id"] = source_id
    return log


def open_session(entry, transport, etherscan, blockscout, nodereal, chain_result: str):
    install(transport, "evm-rpc", chain_id_operation(), rpc_ok(chain_result))
    adapter = EvmAdapter(
        entry,
        transport,
        rpc_source_id="evm-rpc",
        etherscan=etherscan,
        blockscout=blockscout,
        nodereal=nodereal,
    )
    return adapter.open()


def test_mcm_w4_01_parametrized_set_is_every_t0_evm():
    """MCM-W4-01 seven-chain-contract. The parameter set is the registry, not a sample."""
    found = {
        network: entry["evm_chain_id"]
        for network, entry in registry().items()
        if entry["family"] == "evm" and entry["tier"] == 0
    }
    assert found == dict(T0)
    for network, _chain_id in T0:
        entry = registry()[network]
        assert entry["enabled"] is False
        assert entry["status"] == "planned_not_live_verified"
        assert "live_pass" not in json.dumps(entry)


@pytest.mark.parametrize(("network_id", "chain_id"), T0, ids=[item[0] for item in T0])
def test_mcm_w4_01_seven_chain_contract(network_id, chain_id):
    """MCM-W4-01 seven-chain-contract."""
    entry = registry()[network_id]
    assert entry["evm_chain_id"] == chain_id
    assert entry["enabled"] is False
    assert entry["status"] == "planned_not_live_verified"
    transport, backend, _slept, etherscan, blockscout, _clock_fn, _sleep = world()
    session = open_session(entry, transport, etherscan, blockscout, None, hex(chain_id))
    assert session.identity.ok is True
    assert session.identity.chain_id == chain_id
    assert session.probe == "untested"

    install(transport, "evm-rpc", balance_operation(WALLET), rpc_ok(hex(10**18 + chain_id)))
    balance = session.native_balance(WALLET)
    assert balance.refused is False
    assert balance.probe == "untested"
    assert balance.price is None
    assert isinstance(balance.raw_wei, str)
    assert "." not in balance.raw_wei
    assert balance.raw_wei == str(10**18 + chain_id)
    assert int(balance.raw_wei) > 0

    ok_hash = tx_hash("ab", chain_id)
    fail_hash = tx_hash("cd", chain_id)
    l1 = "0x5208" if network_id == "optimism-mainnet" else None
    ok_receipt = {
        "blockNumber": "0x10",
        "effectiveGasPrice": "0x3b9aca00",
        "gasUsed": "0x5208",
        "logs": [erc20_log(ok_hash, 1000 + chain_id)],
        "status": "0x1",
        "transactionHash": ok_hash,
    }
    if l1 is not None:
        ok_receipt["l1Fee"] = l1
    install(transport, "evm-rpc", receipt_operation(ok_hash), rpc_ok(ok_receipt))
    success = session.read_receipt(ok_hash, tenant_id=TENANT, wallet_id=WALLET_ID, wallet=WALLET, observed_at=NOW)
    gas_used = int("0x5208", 16)
    gas_price = int("0x3b9aca00", 16)
    expected_fee = gas_used * gas_price + (int(l1, 16) if l1 else 0)
    assert success.probe == "untested"
    assert success.fee_wei == str(expected_fee)
    assert success.fee_incomplete is (network_id == "base-mainnet")
    assert network_id in OP_STACK or success.fee_incomplete is False
    assets = [event for event in success.events if event.kind in {"asset_in", "asset_out"}]
    assert len(assets) == 1
    assert success_alertable(assets[0]) is True
    assert assets[0].raw_amount == str(1000 + chain_id)
    assert assets[0].finality == "observed"
    fees = [event for event in success.events if event.kind == "fee"]
    assert len(fees) == 1
    assert fees[0].raw_amount == str(expected_fee)

    fail_receipt = {
        "blockNumber": "0x10",
        "effectiveGasPrice": "0x3b9aca00",
        "gasUsed": "0x5208",
        "logs": [erc20_log(fail_hash, 8000 + chain_id)],
        "status": "0x0",
        "transactionHash": fail_hash,
    }
    install(transport, "evm-rpc", receipt_operation(fail_hash), rpc_ok(fail_receipt))
    failed = session.read_receipt(fail_hash, tenant_id=TENANT, wallet_id=WALLET_ID, wallet=WALLET, observed_at=NOW)
    assert failed.probe == "untested"
    assert not any(success_alertable(event) for event in failed.events)
    assert not any(event.kind in {"asset_in", "asset_out"} for event in failed.events)
    assert any(event.kind == "tx_failed" for event in failed.events)
    assert not any(event.raw_amount == str(8000 + chain_id) for event in failed.events)
    assert failed.fee_incomplete is (network_id in OP_STACK)
    blob = json.dumps({"balance": balance.raw_wei, "probe": success.probe, "status": entry["status"]})
    assert "live_pass" not in blob

    mismatch_transport, mismatch_backend, _slept2, mismatch_etherscan, mismatch_blockscout, _c, _s = world()
    mismatch = open_session(entry, mismatch_transport, mismatch_etherscan, mismatch_blockscout, None, "0x539")
    assert mismatch.identity.ok is False
    assert mismatch.identity.reason == "chain_id_mismatch"
    assert mismatch.identity.chain_id == 0x539
    refused_balance = mismatch.native_balance(WALLET)
    refused_receipt = mismatch.read_receipt(ok_hash, tenant_id=TENANT, wallet_id=WALLET_ID, wallet=WALLET, observed_at=NOW)
    assert len(mismatch_backend.calls) == 1
    assert refused_balance.raw_wei is None
    assert refused_balance.observation is None
    assert refused_balance.refused is True
    assert refused_receipt.events == ()
    assert refused_receipt.refused is True
    assert backend.calls


def test_mcm_w4_02_etherscan_histories_are_separate():
    """MCM-W4-02 native-vs-logs. Etherscan rows stay on their own capabilities."""
    entry = registry()["ethereum-mainnet"]
    transport, backend, _slept, etherscan, blockscout, _clock_fn, _sleep = world()
    session = open_session(entry, transport, etherscan, blockscout, None, "0x1")
    catalog = load_source_capabilities()
    sources = {"etherscan": "etherscan_v2"}
    cases = (
        ("history.native_external", "txlist", None, "11"),
        ("history.native_internal", "txlistinternal", None, "22"),
        ("history.fungible", "tokentx", None, "33"),
        ("history.nft", "tokennfttx", "erc721", "7"),
        ("history.nft", "token1155tx", "erc1155", "8"),
    )
    reads = []
    for capability, action, standard, marker in cases:
        operation = etherscan.operation(
            chain_id=1,
            module="account",
            action=action,
            address=WALLET,
            start_block=1,
            end_block=4,
            page=1,
            offset=1000,
        )
        if action in {"tokennfttx", "token1155tx"}:
            item = {"tokenID": marker, "hash": "0x" + action[:2] * 32}
        else:
            item = {"hash": "0x" + "11" * 32, "value": marker}
        install(transport, "etherscan", operation, {"message": "OK", "result": [item], "status": "1"})
        routing = [{"capability": capability, "network": "ethereum-mainnet", "source_ids": ["etherscan"]}]
        fetched = session.read_etherscan(
            capability,
            WALLET,
            1,
            4,
            standard=standard,
            routing=routing,
            sources=sources,
            catalog=catalog,
        )
        reads.append((action, marker, fetched))
        assert fetched.refused is False
        assert fetched.probe == "untested"
        assert fetched.observation is not None
        assert fetched.observation.cursor == "4"
        assert fetched.items
    external, internal, fungible, nft721, nft1155 = reads
    assert external[2].capability == "history.native_external"
    assert internal[2].capability == "history.native_internal"
    assert fungible[2].capability == "history.fungible"
    assert nft721[2].capability == "history.nft"
    assert nft1155[2].capability == "history.nft"
    assert external[2].items[0]["value"] == "11"
    assert internal[2].items[0]["value"] == "22"
    assert fungible[2].items[0]["value"] == "33"
    assert nft721[2].items[0]["tokenID"] == "7"
    assert nft1155[2].items[0]["tokenID"] == "8"
    assert {item[0] for item in reads} == {"txlist", "txlistinternal", "tokentx", "tokennfttx", "token1155tx"}
    urls = [call.url for call in backend.calls if "action=" in call.url]
    assert any("action=txlist&" in url for url in urls)
    assert any("action=txlistinternal&" in url for url in urls)
    assert any("action=tokentx&" in url for url in urls)
    assert any("action=tokennfttx&" in url for url in urls)
    assert any("action=token1155tx&" in url for url in urls)
    assert all("sort=asc" in url for url in urls)


def test_mcm_w4_02_blockscout_histories_are_separate():
    """MCM-W4-02 native-vs-logs. Blockscout address paths stay separate."""
    entry = registry()["ethereum-mainnet"]
    transport, _backend, _slept, etherscan, blockscout, _clock_fn, _sleep = world()
    session = open_session(entry, transport, etherscan, blockscout, None, "0x1")
    tx_path = f"/api/v2/addresses/{WALLET}/transactions"
    install(transport, "blockscout", blockscout.operation(tx_path), {
        "items": [{"hash": "0x" + "ab" * 32, "value": "5"}],
        "next_page_params": {"block_number": 4, "index": 1},
    })
    install(transport, "blockscout", blockscout.operation(tx_path, blockscout.page_query({"block_number": 4, "index": 1})), {
        "items": [{"hash": "0x" + "cd" * 32, "value": "7"}],
        "next_page_params": None,
    })
    internal = f"/api/v2/addresses/{WALLET}/internal-transactions"
    install(transport, "blockscout", blockscout.operation(internal), {
        "items": [{"hash": "0x" + "ef" * 32, "value": "3"}],
        "next_page_params": None,
    })
    tokens = f"/api/v2/addresses/{WALLET}/token-transfers"
    install(transport, "blockscout", blockscout.operation(tokens), {
        "items": [{"hash": "0x" + "12" * 32, "value": "9"}],
        "next_page_params": None,
    })
    external = session.read_blockscout("history.native_external", WALLET, previous_cursor="0")
    native_internal = session.read_blockscout("history.native_internal", WALLET, previous_cursor="0")
    fungible = session.read_blockscout("history.fungible", WALLET, previous_cursor="0")
    assert [item["value"] for item in external.items] == ["5", "7"]
    assert native_internal.items[0]["value"] == "3"
    assert fungible.items[0]["value"] == "9"
    assert external.capability == "history.native_external"
    assert native_internal.capability == "history.native_internal"
    assert fungible.capability == "history.fungible"
    assert external.observation is not None and external.observation.cursor == "exhausted"
    assert int(external.items[0]["value"]) > 0


def test_mcm_w4_02_rpc_fallback_gaps_native_history_and_reads_token_logs():
    """MCM-W4-02 native-vs-logs. Logs are not a native address history."""
    entry = registry()["ethereum-mainnet"]
    transport, backend, _slept, etherscan, blockscout, _clock_fn, _sleep = world()
    session = open_session(entry, transport, etherscan, blockscout, None, "0x1")
    install(transport, "evm-rpc", balance_operation(WALLET), rpc_ok(hex(10**18 + 1)))
    balance = session.native_balance(WALLET)
    before = len(backend.calls)
    external = session.rpc_native_history("history.native_external", previous_cursor="4")
    internal = session.rpc_native_history("history.native_internal", previous_cursor="4")
    assert len(backend.calls) == before
    assert external.reason == "rpc_has_no_address_history"
    assert internal.reason == "rpc_has_no_address_history"
    assert external.observation is not None
    assert external.observation.cursor == "4"
    assert external.observation.category == "error"
    assert "rpc_has_no_address_history" in external.observation.gaps
    wide = session.token_history_from_logs(WALLET, 0, LOG_WIDE := 5_000, previous_cursor="4")
    assert len(backend.calls) == before
    assert wide.observation is not None
    assert wide.observation.cursor == "4"
    assert "log_range_exceeds_bound" in wide.observation.gaps
    assert wide.items == ()
    amount = 10**18 + 5
    log = erc20_log("0x" + "ab" * 32, amount)
    install(transport, "evm-rpc", token_log_operation(WALLET, 10, 12), rpc_ok([log]))
    logs = session.token_history_from_logs(WALLET, 10, 12, previous_cursor="4")
    assert logs.ledger is not None
    assert len(logs.ledger.movements) == 1
    assert logs.ledger.movements[0].raw_amount == str(amount)
    assert int(logs.ledger.movements[0].raw_amount) > 0
    report = session.adapter.portfolio(
        WALLET_ID,
        {
            "balances.native": balance.observation,
            "history.native_external": external.observation,
            "history.native_internal": internal.observation,
            "history.fungible": logs.observation,
        },
        catalog=load_source_capabilities(),
        registry=registry(),
        now=NOW,
    )
    summary = report["summary"]
    assert summary["complete"] is False
    assert "native_only_not_portfolio" in summary["reason_codes"]
    rows = {row["capability"]: row for row in report["rows"]}
    assert rows["balances.native"]["quality"] == "complete_in_declared_scope"
    assert rows["balances.native"]["price_quality"] == "unknown"
    assert rows["history.native_external"]["quality"] != "complete_in_declared_scope"
    assert "rpc_has_no_address_history" in rows["history.native_external"]["reason_codes"]
    assert rows["history.fungible"]["quality"] != "complete_in_declared_scope"
    assert "live_pass" not in json.dumps(rows["balances.native"])


def test_mcm_w4_03_last_full_page_requests_the_empty_follower():
    """MCM-W4-03 pagination-and-limits. A full page is not the end until the next page is short."""
    transport, backend, _slept, etherscan, _blockscout, _clock_fn, _sleep = world()
    page1 = etherscan.operation(chain_id=1, module="account", action="txlist", address=WALLET, start_block=1, end_block=9, page=1, offset=2)
    page2 = etherscan.operation(chain_id=1, module="account", action="txlist", address=WALLET, start_block=1, end_block=9, page=2, offset=2)
    install(transport, "etherscan", page1, {
        "message": "OK",
        "result": [{"hash": "0x" + "ab" * 32, "value": "9"}, {"hash": "0x" + "cd" * 32, "value": "8"}],
        "status": "1",
    })
    install(transport, "etherscan", page2, {"message": "No transactions found", "result": "No transactions found", "status": "0"})
    read = etherscan.account_history(chain_id=1, action="txlist", address=WALLET, start_block=1, end_block=9, offset=2, previous_cursor="1")
    assert [item["value"] for item in read.items] == ["9", "8"]
    assert read.pagination_complete is True
    assert read.category == "ok"
    assert read.cursor == "9"
    assert any("page=2" in call.url and "sort=asc" in call.url for call in backend.calls)
    observation = to_observation(read, source="etherscan", scope_from="1", scope_to="9")
    assert observation.cursor == "9"
    assert observation.pagination_complete is True


def test_mcm_w4_03_window_limit_splits_by_block_range():
    """MCM-W4-03 pagination-and-limits. The 10000 window is re-read in halves, not kept as a full history."""
    transport, backend, _slept, etherscan, _blockscout, _clock_fn, _sleep = world()
    offset = ETHERSCAN_RESULT_WINDOW

    def operation(start, end, page):
        return etherscan.operation(
            chain_id=1, module="account", action="txlist", address=WALLET,
            start_block=start, end_block=end, page=page, offset=offset,
        )

    install(transport, "etherscan", operation(0, 3, 1), {
        "message": "OK",
        "result": [{"blockNumber": "0", "value": "1"} for _ in range(offset)],
        "status": "1",
    })
    install(transport, "etherscan", operation(0, 1, 1), {"message": "OK", "result": [{"blockNumber": "1", "value": "4"}], "status": "1"})
    install(transport, "etherscan", operation(2, 3, 1), {"message": "OK", "result": [{"blockNumber": "3", "value": "6"}], "status": "1"})
    read = etherscan.account_history(
        chain_id=1, action="txlist", address=WALLET, start_block=0, end_block=3,
        offset=offset, previous_cursor="1", item_cap=200_000,
    )
    assert [item["value"] for item in read.items] == ["4", "6"]
    assert read.pagination_complete is True
    assert read.cursor == "3"
    assert not any("page=2" in call.url and "startblock=0" in call.url and "endblock=3" in call.url for call in backend.calls)
    assert int(read.items[0]["value"]) > 0


def test_mcm_w4_03_single_block_over_limit_is_a_gap():
    """MCM-W4-03 pagination-and-limits. One block that still overflows is a gap, not a complete wallet."""
    transport, backend, _slept, etherscan, _blockscout, _clock_fn, _sleep = world()
    offset = ETHERSCAN_RESULT_WINDOW
    operation = etherscan.operation(
        chain_id=1, module="account", action="txlist", address=WALLET,
        start_block=8, end_block=8, page=1, offset=offset,
    )
    install(transport, "etherscan", operation, {
        "message": "OK",
        "result": [{"value": "1"} for _ in range(offset)],
        "status": "1",
    })
    read = etherscan.account_history(
        chain_id=1, action="txlist", address=WALLET, start_block=8, end_block=8,
        offset=offset, previous_cursor="3", item_cap=200_000,
    )
    assert read.items == ()
    assert read.pagination_complete is False
    assert read.category == "partial"
    assert read.cursor == "3"
    assert read.gaps == ("single_block_over_limit:8",)
    assert read.category != "empty"
    assert not any("page=2" in call.url and "startblock=8" in call.url for call in backend.calls)


def test_mcm_w4_03_http_200_partial_and_rate_limit():
    """MCM-W4-03 pagination-and-limits. A 200 NOTOK partial is not success; a rate limit is retried."""
    transport, backend, slept, etherscan, _blockscout, _clock_fn, _sleep = world()
    partial = etherscan.operation(chain_id=1, module="account", action="txlist", address=WALLET, start_block=20, end_block=21, page=1, offset=10)
    install(transport, "etherscan", partial, {"message": "NOTOK", "result": [{"value": "55"}], "status": "0"})
    partial_read = etherscan.account_history(
        chain_id=1, action="txlist", address=WALLET, start_block=20, end_block=21, offset=10, previous_cursor="6",
    )
    assert partial_read.category == "partial"
    assert partial_read.pagination_complete is False
    assert partial_read.cursor == "6"
    assert partial_read.items == ()
    assert partial_read.category != "empty"

    limited = etherscan.operation(chain_id=1, module="account", action="txlist", address=WALLET, start_block=3, end_block=4, page=1, offset=10)
    first = RawResponse(200, {"Retry-After": "1"}, json.dumps({"message": "NOTOK", "result": "Max rate limit reached", "status": "0"}).encode("utf-8"))
    second = RawResponse(200, {}, json.dumps({"message": "OK", "result": [{"value": "77"}], "status": "1"}).encode("utf-8"))
    install_seq(transport, "etherscan", limited, [first, second])
    before = len(backend.calls)
    retried = etherscan.account_history(
        chain_id=1, action="txlist", address=WALLET, start_block=3, end_block=4, offset=10, previous_cursor="2",
    )
    assert retried.items[0]["value"] == "77"
    assert int(retried.items[0]["value"]) > 0
    assert retried.pagination_complete is True
    assert retried.cursor == "4"
    assert len(backend.calls) == before + 2
    assert 1.0 in slept


def test_mcm_w4_03_free_plan_is_blocked_not_empty():
    """MCM-W4-03 pagination-and-limits. A free-plan refusal is BLOCKED, not an empty history."""
    transport, _backend, _slept, etherscan, _blockscout, _clock_fn, _sleep = world()
    messages = (
        "Free API access is not supported for this chain",
        "this plan does not support this chain",
    )
    for index, message in enumerate(messages):
        operation = etherscan.operation(
            chain_id=1, module="account", action="txlist", address=WALLET,
            start_block=index, end_block=index, page=1, offset=10,
        )
        install(transport, "etherscan", operation, {"message": "NOTOK", "result": message, "status": "0"})
        read = etherscan.account_history(
            chain_id=1, action="txlist", address=WALLET, start_block=index, end_block=index,
            offset=10, previous_cursor="5",
        )
        assert read.blocked is True
        assert read.reason == "plan_not_supported"
        assert read.category == "error"
        assert read.category != "empty"
        assert read.items == ()
        assert read.cursor == "5"
        assert read.pagination_complete is False
        assert SYNTHETIC_KEY not in read.detail


def test_mcm_w4_03_nodereal_splits_span_and_walks_page_key():
    """MCM-W4-03 pagination-and-limits. NodeReal span and pageKey stay inside the documented cap."""
    entry = registry()["ethereum-mainnet"]
    transport, backend, _slept, etherscan, blockscout, clock, sleep = world()
    ethereum = NodeRealClient(transport, "nodereal", network="ethereum-mainnet", clock=clock, sleep=sleep, random=lambda: 0.0)
    bsc = NodeRealClient(transport, "nodereal", network="bsc-mainnet", clock=clock, sleep=sleep, random=lambda: 0.0)
    polygon = NodeRealClient(transport, "nodereal", network="polygon-pos", clock=clock, sleep=sleep, random=lambda: 0.0)
    session = open_session(entry, transport, etherscan, blockscout, ethereum, "0x1")

    def operation(**kwargs):
        return ethereum.operation(address=WALLET, side="to", **kwargs)

    install(transport, "nodereal", operation(category="external", start=0, end=100_000, page_key=None), rpc_ok({
        "pageKey": "k2",
        "transfers": [{"category": "external", "hash": "0x" + "11" * 32, "value": "15"}],
    }))
    install(transport, "nodereal", operation(category="external", start=0, end=100_000, page_key="k2"), rpc_ok({
        "transfers": [{"hash": "0x" + "22" * 32, "value": "16"}],
    }))
    install(transport, "nodereal", operation(category="external", start=100_001, end=100_001, page_key=None), rpc_ok({
        "transfers": [{"hash": "0x" + "33" * 32, "value": "17"}],
    }))
    external = session.read_nodereal("external", WALLET, 0, 100_001, previous_cursor="1")
    assert external.capability == "history.native_external"
    assert [item["value"] for item in external.items] == ["15", "16", "17"]
    assert external.observation is not None
    assert external.observation.cursor == "100001"
    assert external.observation.pagination_complete is True
    assert int(external.items[1]["value"]) > 0

    install(transport, "nodereal", operation(category="20", start=10, end=11, page_key=None), rpc_ok({
        "transfers": [{"hash": "0x" + "44" * 32, "value": "20"}],
    }))
    fungible = session.read_nodereal("20", WALLET, 10, 11)
    install(transport, "nodereal", operation(category="721", start=12, end=13, page_key=None), rpc_ok({
        "transfers": [{"hash": "0x" + "55" * 32, "tokenId": "4", "value": "1"}],
    }))
    nft = session.read_nodereal("721", WALLET, 12, 13)
    assert fungible.capability == CATEGORY_CAPABILITY["20"] == "history.fungible"
    assert nft.capability == "history.nft"
    assert fungible.items[0]["category"] == "20"
    assert nft.items[0]["category"] == "721"
    assert nft.items[0]["tokenId"] == "4"
    assert "20" not in {item["value"] for item in external.items}

    install(transport, "nodereal", bsc.operation(category="external", address=WALLET, start=1, end=2, page_key=None, side="to"), rpc_ok({
        "transfers": [{"hash": "0x" + "66" * 32, "value": "42"}],
    }))
    bsc_read = bsc.transfers(category="external", address=WALLET, start_block=1, end_block=2)
    assert bsc_read.items[0]["value"] == "42"
    assert bsc_read.capability == "history.native_external"
    assert int(bsc_read.items[0]["value"]) > 0

    before = len(backend.calls)
    refused = polygon.transfers(category="external", address=WALLET, start_block=0, end_block=1, previous_cursor="9")
    assert len(backend.calls) == before
    assert refused.blocked is True
    assert refused.reason == "nodereal_chain_not_supported"
    assert refused.category != "empty"
    assert refused.cursor == "9"

    install(transport, "nodereal", operation(category="external", start=7, end=8, page_key=None), rpc_ok({"pageKey": None}))
    missing = session.read_nodereal("external", WALLET, 7, 8, previous_cursor="1")
    assert missing.items == ()
    assert missing.reason == "missing_transfers"
    assert missing.observation is not None
    assert missing.observation.cursor == "1"
    assert missing.observation.category == "error"

    for call in backend.calls:
        if call.source_id != "nodereal":
            continue
        params = json.loads(call.body)["params"][0]
        assert int(params["maxCount"], 16) <= 0x3E8
        assert int(params["toBlock"], 16) - int(params["fromBlock"], 16) <= MAX_BLOCK_SPAN


def test_mcm_w4_03_disallowed_etherscan_action_does_not_call_backend():
    """MCM-W4-03 pagination-and-limits. Writes are rejected before the backend; a real txlist still returns value."""
    transport, backend, _slept, etherscan, _blockscout, _clock_fn, _sleep = world()
    with pytest.raises(SecurityError) as caught:
        etherscan.account_history(
            chain_id=1,
            module="proxy",
            action="eth_sendRawTransaction",
            address=WALLET,
            start_block=0,
            end_block=1,
        )
    assert len(backend.calls) == 0
    assert SYNTHETIC_KEY not in str(caught.value)
    assert SYNTHETIC_KEY not in repr(etherscan)
    operation = etherscan.operation(chain_id=1, module="account", action="txlist", address=WALLET, start_block=0, end_block=1, page=1, offset=1000)
    install(transport, "etherscan", operation, {"message": "OK", "result": [{"value": "12"}], "status": "1"})
    read = etherscan.account_history(chain_id=1, action="txlist", address=WALLET, start_block=0, end_block=1)
    assert len(backend.calls) == 1
    assert read.items[0]["value"] == "12"
    assert int(read.items[0]["value"]) > 0
    assert SYNTHETIC_KEY not in redact(backend.calls[0].url, [SYNTHETIC_KEY])


def test_mcm_w4_03_failed_page_keeps_the_previous_cursor():
    """MCM-W4-03 pagination-and-limits. A failed page does not advance the cursor."""
    entry = registry()["ethereum-mainnet"]
    transport, backend, _slept, etherscan, blockscout, _clock_fn, _sleep = world(retry_attempts=0)
    session = open_session(entry, transport, etherscan, blockscout, None, "0x1")
    page1 = etherscan.operation(chain_id=1, module="account", action="txlist", address=WALLET, start_block=1, end_block=9, page=1, offset=2)
    page2 = etherscan.operation(chain_id=1, module="account", action="txlist", address=WALLET, start_block=1, end_block=9, page=2, offset=2)
    install(transport, "etherscan", page1, {"message": "OK", "result": [{"value": "50"}, {"value": "51"}], "status": "1"})
    install(transport, "etherscan", page2, RawResponse(500, {}, b"unavailable"))
    read = etherscan.account_history(
        chain_id=1, action="txlist", address=WALLET, start_block=1, end_block=9, offset=2, previous_cursor="4",
    )
    assert read.cursor == "4"
    assert read.pagination_complete is False
    assert read.category == "error"
    assert read.items == ()
    assert SYNTHETIC_KEY not in read.detail
    assert "apikey=***" in read.detail
    fetched = session.read_etherscan("history.native_external", WALLET, 1, 9, offset=2, previous_cursor="4")
    assert fetched.observation is not None
    assert fetched.observation.cursor == "4"
    assert fetched.observation.pagination_complete is False
    assert any("page=2" in call.url for call in backend.calls)


def _batch_data(ids: list[int], values: list[int]) -> str:
    def word(number: int) -> bytes:
        return number.to_bytes(32, "big")

    offset_ids = 64
    offset_values = 64 + 32 + 32 * len(ids)
    blob = word(offset_ids) + word(offset_values)
    blob += word(len(ids)) + b"".join(word(item) for item in ids)
    blob += word(len(values)) + b"".join(word(item) for item in values)
    return "0x" + blob.hex()


def test_mcm_w4_04_erc20_erc721_erc1155_mint_burn_and_batch():
    """MCM-W4-04 token-accounting. Standards, mint, burn, and batch rows stay distinct."""
    tx = "0x" + "ab" * 32
    logs = [
        erc20_log(tx, 100, index=1),
        erc20_log(tx, 10, sender=ZERO_ADDRESS, index=2),
        erc20_log(tx, 4, sender=WALLET, receiver=ZERO_ADDRESS, index=3),
        {
            "address": TOKEN_721,
            "data": "0x",
            "logIndex": "0x4",
            "topics": [TRANSFER_TOPIC, pad_address(OTHER), pad_address(WALLET), hex_word(9)],
            "transactionHash": tx,
        },
        {
            "address": TOKEN_1155,
            "data": "0x" + (3).to_bytes(32, "big").hex() + (6).to_bytes(32, "big").hex(),
            "logIndex": "0x5",
            "topics": [TRANSFER_SINGLE_TOPIC, pad_address(OTHER), pad_address(OTHER), pad_address(WALLET)],
            "transactionHash": tx,
        },
        {
            "address": TOKEN_1155,
            "data": _batch_data([1, 2], [5, 9]),
            "logIndex": "0x6",
            "topics": [TRANSFER_BATCH_TOPIC, pad_address(OTHER), pad_address(OTHER), pad_address(WALLET)],
            "transactionHash": tx,
        },
    ]
    ledger = account_logs(logs, WALLET)
    kinds = {(item.kind, item.direction, item.token_id, item.raw_amount) for item in ledger.movements}
    assert ("erc20", "in", None, "100") in kinds
    assert ("erc20", "mint", None, "10") in kinds
    assert ("erc20", "burn", None, "4") in kinds
    assert ("erc721", "in", "9", "1") in kinds
    assert ("erc1155", "in", "3", "6") in kinds
    assert ("erc1155", "in", "1", "5") in kinds
    assert ("erc1155", "in", "2", "9") in kinds
    batch = [item for item in ledger.movements if item.kind == "erc1155" and item.log_index == 6]
    assert len(batch) == 2
    assert ledger.net(TOKEN) == 106
    assert ledger.inbound(TOKEN) == 110
    assert ledger.outbound(TOKEN) == 4
    assert ledger.net(TOKEN_721, "9") == 1
    assert ledger.net(TOKEN_1155, "1") == 5
    assert ledger.net(TOKEN_1155, "2") == 9
    assert int(ledger.movements[0].raw_amount) > 0


def test_mcm_w4_04_self_transfer_nets_to_zero():
    """MCM-W4-04 token-accounting. A self-transfer is not an inflow plus an outflow."""
    log = erc20_log("0x" + "ab" * 32, 500, sender=WALLET, receiver=WALLET)
    ledger = account_logs([log], WALLET)
    assert len(ledger.movements) == 1
    assert ledger.movements[0].direction == "self"
    assert ledger.movements[0].raw_amount == "500"
    assert ledger.net(TOKEN) == 0
    assert ledger.inbound(TOKEN) == 0
    assert ledger.outbound(TOKEN) == 0


def test_mcm_w4_04_duplicate_log_is_counted_once():
    """MCM-W4-04 token-accounting. The same tx and log index from two sources counts once."""
    tx = "0x" + "ab" * 32
    first = erc20_log(tx, 50, index=4, source_id="etherscan")
    second = erc20_log(tx, 50, index=4, source_id="evm-rpc")
    batch = {
        "address": TOKEN_1155,
        "data": _batch_data([1, 2], [5, 9]),
        "logIndex": "0x8",
        "source_id": "etherscan",
        "topics": [TRANSFER_BATCH_TOPIC, pad_address(OTHER), pad_address(OTHER), pad_address(WALLET)],
        "transactionHash": tx,
    }
    again = dict(batch, source_id="evm-rpc")
    ledger = account_logs([first, second, batch, again], WALLET)
    erc20 = [item for item in ledger.movements if item.kind == "erc20"]
    assert len(erc20) == 1
    assert erc20[0].raw_amount == "50"
    assert erc20[0].source_ids == ("etherscan", "evm-rpc")
    assert ledger.net(TOKEN) == 50
    batched = [item for item in ledger.movements if item.kind == "erc1155"]
    assert len(batched) == 2
    assert ledger.net(TOKEN_1155, "1") == 5
    assert {source for item in batched for source in item.source_ids} == {"etherscan", "evm-rpc"}


def test_mcm_w4_04_fee_or_rebase_mismatch_is_not_rewritten():
    """MCM-W4-04 token-accounting. A balance delta that disagrees with the log is flagged, not rewritten."""
    log = erc20_log("0x" + "ab" * 32, 100)
    ledger = account_logs([log], WALLET, balance_deltas={TOKEN: 99})
    assert ledger.movements[0].raw_amount == "100"
    assert ledger.net(TOKEN) == 100
    assert "amount_mismatch" in ledger.flags
    assert all(item.raw_amount != "99" for item in ledger.movements)


def test_mcm_w4_04_amount_above_2_53_and_decimals_18_6_8():
    """MCM-W4-04 token-accounting. Raw integers survive 2^53, and 18/6/8 decimals use decimal arithmetic."""
    assert TRANSFER_TOPIC == "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
    assert TRANSFER_SINGLE_TOPIC == "0xc3d58168c5ae7397731d063d5bbf3d657854427343f4c083240f7aacaa2d0f62"
    assert TRANSFER_BATCH_TOPIC == "0x4a39dc06d4c0dbc64b70af90fd698a233a518aa5d07e595d983b8c0526c8f7fb"
    huge = 2**53 + 3
    logs = [
        erc20_log("0x" + "ab" * 32, huge, token=TOKEN, index=1),
        erc20_log("0x" + "cd" * 32, 1_500_000, token=TOKEN_6, index=2),
        erc20_log("0x" + "ef" * 32, 150_000_000, token=TOKEN_8, index=3),
        erc20_log("0x" + "12" * 32, 10, token=TOKEN_PLAIN, index=4),
    ]
    ledger = account_logs(logs, WALLET, decimals={TOKEN: 18, TOKEN_6: 6, TOKEN_8: 8})
    by_token = {item.token: item for item in ledger.movements}
    assert by_token[TOKEN].raw_amount == str(huge)
    assert int(by_token[TOKEN].raw_amount) == huge
    assert int(by_token[TOKEN].raw_amount) != int(float(by_token[TOKEN].raw_amount))
    assert by_token[TOKEN].display_amount == format_units(huge, 18)
    assert "e" not in by_token[TOKEN].display_amount.lower()
    assert by_token[TOKEN_6].display_amount == format_units(1_500_000, 6) == "1.5"
    assert by_token[TOKEN_8].display_amount == format_units(150_000_000, 8) == "1.5"
    assert by_token[TOKEN_PLAIN].raw_amount == "10"
    assert by_token[TOKEN_PLAIN].display_amount is None


def test_mcm_w4_04_unknown_topic_layout_is_unknown():
    """MCM-W4-04 token-accounting. An unfamiliar topic is unknown and is not given a guessed amount."""
    tx = "0x" + "ab" * 32
    weird = {
        "address": TOKEN,
        "data": hex_word(12345),
        "logIndex": "0x1",
        "topics": ["0x" + "ee" * 32],
        "transactionHash": tx,
    }
    short = {
        "address": TOKEN,
        "data": hex_word(12345),
        "logIndex": "0x2",
        "topics": [TRANSFER_TOPIC, pad_address(OTHER)],
        "transactionHash": tx,
    }
    ledger = account_logs([weird, short], WALLET)
    assert len(ledger.movements) == 2
    assert {item.kind for item in ledger.movements} == {"unknown"}
    assert all(item.raw_amount is None for item in ledger.movements)
    assert all(item.raw_amount != "0" for item in ledger.movements)
    assert ledger.net(TOKEN) == 0
    assert ledger.inbound(TOKEN) == 0
