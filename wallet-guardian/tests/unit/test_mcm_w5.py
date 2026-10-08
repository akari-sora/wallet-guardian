"""Offline acceptance for MCM-W5. Synthetic fixtures only. No network.

MCM-W5-01 approval-families
MCM-W5-02 relayed-permit
MCM-W5-03 permit2-layers
MCM-W5-04 offline-blindspot
"""

from __future__ import annotations

import json
import socket
from pathlib import Path

import pytest
from eth_abi import encode

from guardian.amounts import UINT256_MAX
from guardian.chains.evm.accounting import TRANSFER_TOPIC, ZERO_ADDRESS, hex_word, pad_address
from guardian.chains.evm.permissions import (
    APPROVAL_FOR_ALL_TOPIC,
    ERC1155_INTERFACE,
    ERC20_APPROVAL_TOPIC,
    ERC721_INTERFACE,
    UNLIMITED_FLOOR,
    ApprovalRow,
    ContractEvidence,
    PriorState,
    RiskPolicy,
    StateRead,
    approval_events_from_receipt,
    encode_view_call,
    eth_call_operation,
    parse_approval_logs,
    read_erc20_allowance,
    read_supports_interface,
    read_token_balance,
    reconcile_approvals,
    scan_approvals,
)
from guardian.chains.evm.permits import (
    CANONICAL_PERMIT2,
    PERMIT2_APPROVAL_TOPIC,
    PERMIT2_LOCKDOWN_TOPIC,
    PERMIT2_NONCE_TOPIC,
    PERMIT2_PERMIT_TOPIC,
    PERMIT2_UNORDERED_TOPIC,
    Permit2ConfigError,
    coverage_for_submitted,
    load_permit2_deployments,
    observe_submitted_permits,
    offchain_unused_status,
    parse_permit2_logs,
    read_permit2_allowance,
    read_token_nonce,
    reconcile_permit2,
    unsubmitted_signatures,
    used_nonces,
    validate_permit2_document,
)
from guardian.network_registry import load_registry
from guardian.sources.budget import BudgetBook
from guardian.sources.fixtures import FixtureBackend, RawResponse, request_key
from guardian.sources.transport import ReadOnlyTransport, build_endpoint_registry

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "tests" / "fixtures" / "evm-permissions"
PUBLIC_IP = "1.1.1.1"
RPC_URL = "https://rpc.example.invalid"
OWNER = "0x0000000000000000000000000000000000000001"
RELAYER = "0x0000000000000000000000000000000000000002"
SP_NEW = "0x0000000000000000000000000000000000000003"
SP_UP = "0x0000000000000000000000000000000000000004"
SP_DOWN = "0x0000000000000000000000000000000000000005"
SP_REVOKE = "0x0000000000000000000000000000000000000006"
SP_CONSUMED = "0x0000000000000000000000000000000000000007"
SP_FIRST = "0x0000000000000000000000000000000000000008"
SP_MAX = "0x0000000000000000000000000000000000000009"
SP_HIGH = "0x000000000000000000000000000000000000000a"
SP_FLOOR = "0x000000000000000000000000000000000000000b"
SP_WIDE = "0x000000000000000000000000000000000000000d"
OPERATOR = "0x000000000000000000000000000000000000000c"
TOKEN = "0x00000000000000000000000000000000000000aa"
TOKEN_6 = "0x00000000000000000000000000000000000000ab"
TOKEN_721 = "0x00000000000000000000000000000000000000bb"
TOKEN_1155 = "0x00000000000000000000000000000000000000cc"
TOKEN_UNKNOWN = "0x00000000000000000000000000000000000000dd"
TOKEN_BOTH = "0x00000000000000000000000000000000000000ee"
TENANT = "tenant-synthetic"
WALLET_ID = "11111111-1111-4111-8111-111111111111"
NOW = 1_700_000_000
PERMIT2 = CANONICAL_PERMIT2.lower()

assert all(len(item) == 42 and item.startswith("0x") for item in (
    OWNER, RELAYER, SP_NEW, SP_MAX, OPERATOR, TOKEN, TOKEN_721, TOKEN_1155, TOKEN_UNKNOWN, TOKEN_BOTH,
))
assert UNLIMITED_FLOOR == 2**255
assert UINT256_MAX == 2**256 - 1


@pytest.fixture(autouse=True)
def _offline(monkeypatch):
    def explode(*_args, **_kwargs):
        raise AssertionError("network is forbidden in MCM-W5 tests")

    monkeypatch.setattr(socket, "socket", explode)
    monkeypatch.setattr(socket, "create_connection", explode)
    monkeypatch.setattr(socket, "getaddrinfo", explode)
    import httpx

    monkeypatch.setattr(httpx, "Client", explode)


def _resolve(_host: str) -> list[str]:
    return [PUBLIC_IP]


def world():
    env_name = "EVM_RPC_ENDPOINT"
    document = {
        "budgets": {"evm-rpc": {"max_concurrency": 4, "max_requests_per_day": 1000, "max_rps": 100, "retry_attempts": 0}},
        "sources": {"evm-rpc": {"adapter": "evm_rpc", "budget_group": "evm-rpc", "endpoint_env": env_name}},
    }
    endpoints = build_endpoint_registry(document, secrets={env_name: RPC_URL})
    book = BudgetBook.from_document(document["budgets"], clock=lambda: 1_700_000_000.0, sleep=lambda _seconds: None)
    backend = FixtureBackend({})
    return ReadOnlyTransport(endpoints, book, backend, resolve=_resolve)


def install(transport, operation, payload) -> None:
    call = transport.encode("evm-rpc", operation)
    body = payload if isinstance(payload, RawResponse) else RawResponse(200, {}, json.dumps(payload).encode("utf-8"))
    transport.backend.mapping[request_key(call.method, call.url, call.body)] = body


def rpc_ok(result):
    return {"id": 1, "jsonrpc": "2.0", "result": result}


def rpc_error():
    return {"error": {"code": -32000, "message": "execution reverted"}, "id": 1, "jsonrpc": "2.0"}


def tx_hash(marker: str) -> str:
    return "0x" + (marker * 32)[:64]


def approval_log(token: str, spender: str, amount: int, tx: str, *, index: int = 0, owner: str = OWNER):
    return {
        "address": token,
        "data": hex_word(amount),
        "from": RELAYER,
        "logIndex": hex(index),
        "topics": [ERC20_APPROVAL_TOPIC, pad_address(owner), pad_address(spender)],
        "transactionHash": tx,
    }


def transfer_log(token: str, tx: str, amount: int, *, index: int = 1):
    return {
        "address": token,
        "data": hex_word(amount),
        "logIndex": hex(index),
        "topics": [TRANSFER_TOPIC, pad_address(OWNER), pad_address(RELAYER)],
        "transactionHash": tx,
    }


def nft_log(token: str, spender: str, token_id: int, tx: str):
    return {
        "address": token,
        "data": "0x",
        "logIndex": "0x0",
        "topics": [ERC20_APPROVAL_TOPIC, pad_address(OWNER), pad_address(spender), hex_word(token_id)],
        "transactionHash": tx,
    }


def operator_log(token: str, operator: str, approved: bool, tx: str):
    return {
        "address": token,
        "data": hex_word(1 if approved else 0),
        "logIndex": "0x0",
        "topics": [APPROVAL_FOR_ALL_TOPIC, pad_address(OWNER), pad_address(operator)],
        "transactionHash": tx,
    }


def erc20_state(amount: int) -> StateRead:
    return StateRead(True, amount=amount)


def scan(logs, states, prior=None, **kwargs):
    return reconcile_approvals(
        logs,
        OWNER,
        states=states,
        prior=prior or {},
        pagination_complete=kwargs.pop("pagination_complete", True),
        gap_detection_complete=kwargs.pop("gap_detection_complete", True),
        **kwargs,
    )


def row_for(scan_result, spender: str) -> ApprovalRow:
    matches = [row for row in scan_result.rows if row.spender == spender]
    assert len(matches) == 1
    return matches[0]


def word(value: int) -> str:
    return "0x" + value.to_bytes(32, "big").hex()


def permit2_data(*values: int) -> str:
    return "0x" + "".join(value.to_bytes(32, "big").hex() for value in values)


def permit2612_call(owner: str, spender: str, value: int) -> bytes:
    """Synthetic already-mined calldata. This is not a transaction to send."""
    body = encode(
        ["address", "address", "uint256", "uint256", "uint8", "bytes32", "bytes32"],
        [owner, spender, value, 1_893_456_000, 27, bytes.fromhex("ab" * 32), bytes.fromhex("cd" * 32)],
    )
    return bytes.fromhex("d505accf") + body


def dai_call(holder: str, spender: str, *, allowed: bool, nonce: int) -> bytes:
    body = encode(
        ["address", "address", "uint256", "uint256", "bool", "uint8", "bytes32", "bytes32"],
        [holder, spender, nonce, 1_893_456_000, allowed, 28, bytes.fromhex("11" * 32), bytes.fromhex("22" * 32)],
    )
    return bytes.fromhex("8fcbaf0c") + body


def multicall_wrap(inner: bytes) -> str:
    body = encode(["bytes[]"], [[inner]])
    return "0x" + bytes.fromhex("ac9650d8").hex() + body.hex()


def test_mcm_w5_01_approval_families_and_unlimited_allowance():
    """MCM-W5-01 approval-families. An active unlimited allowance is found."""
    fixture = json.loads((FIXTURES / "erc20-unlimited.json").read_text(encoding="utf-8"))
    assert "synthetic" in fixture["note"].lower()
    assert fixture["log"]["topics"][0] == ERC20_APPROVAL_TOPIC
    assert ERC20_APPROVAL_TOPIC == "0x8c5be1e5ebec7d5bd14f71427d1e84f3dd0314c0f7b2291e5b200ac8c7c3b925"
    unlimited_tx = fixture["tx"]
    new_tx = tx_hash("11")
    up_tx = tx_hash("22")
    down_tx = tx_hash("33")
    revoke_tx = tx_hash("44")
    consumed_tx = tx_hash("55")
    first_tx = tx_hash("66")
    floor_tx = tx_hash("77")
    high_tx = tx_hash("88")
    small_tx = tx_hash("99")
    logs = [
        fixture["log"],
        approval_log(TOKEN, SP_NEW, 10**18, new_tx),
        approval_log(TOKEN, SP_UP, 10**18 + 5, up_tx),
        approval_log(TOKEN, SP_DOWN, 10**6, down_tx),
        approval_log(TOKEN, SP_REVOKE, 0, revoke_tx),
        approval_log(TOKEN, SP_CONSUMED, 0, consumed_tx),
        transfer_log(TOKEN, consumed_tx, 200),
        approval_log(TOKEN_6, SP_FIRST, 7, first_tx),
        approval_log(TOKEN, SP_FLOOR, UNLIMITED_FLOOR, floor_tx),
        approval_log(TOKEN, SP_HIGH, 5_000, high_tx),
        approval_log(TOKEN_6, SP_WIDE, 2**53 + 9, small_tx),
    ]
    states = {
        ("erc20", TOKEN, SP_MAX): erc20_state(UINT256_MAX),
        ("erc20", TOKEN, SP_NEW): erc20_state(10**18),
        ("erc20", TOKEN, SP_UP): erc20_state(10**18 + 5),
        ("erc20", TOKEN, SP_DOWN): erc20_state(10**6),
        ("erc20", TOKEN, SP_REVOKE): erc20_state(0),
        ("erc20", TOKEN, SP_CONSUMED): erc20_state(0),
        ("erc20", TOKEN_6, SP_FIRST): erc20_state(7),
        ("erc20", TOKEN, SP_FLOOR): erc20_state(UNLIMITED_FLOOR),
        ("erc20", TOKEN, SP_HIGH): erc20_state(5_000),
        ("erc20", TOKEN_6, SP_WIDE): erc20_state(2**53 + 9),
    }
    prior = {
        ("erc20", TOKEN, SP_MAX): PriorState(True, amount=0),
        ("erc20", TOKEN, SP_NEW): PriorState(True, amount=0),
        ("erc20", TOKEN, SP_UP): PriorState(True, amount=10**18),
        ("erc20", TOKEN, SP_DOWN): PriorState(True, amount=10**18),
        ("erc20", TOKEN, SP_REVOKE): PriorState(True, amount=10**6),
        ("erc20", TOKEN, SP_CONSUMED): PriorState(True, amount=200),
        ("erc20", TOKEN, SP_FLOOR): PriorState(True, amount=1),
        ("erc20", TOKEN, SP_HIGH): PriorState(True, amount=1),
        ("erc20", TOKEN_6, SP_WIDE): PriorState(True, amount=1),
    }
    result = scan(
        logs,
        states,
        prior,
        balances={TOKEN: (True, 1_000), TOKEN_6: (True, 10**6)},
        policy=RiskPolicy(raw_high_threshold=1_000, balance_bps=10_000),
        tx_from={unlimited_tx: RELAYER},
        next_cursor="20",
        previous_cursor="10",
    )
    unlimited = row_for(result, SP_MAX)
    assert unlimited.status == "active"
    assert unlimited.change == "new"
    assert unlimited.family == "erc20"
    assert unlimited.capability == "permissions.erc20"
    assert unlimited.raw_amount == str(UINT256_MAX)
    assert isinstance(unlimited.raw_amount, str) and "." not in unlimited.raw_amount
    assert int(unlimited.raw_amount) > 2**53
    assert "unlimited" in unlimited.flags
    assert "unlabeled" in unlimited.flags
    assert "safe" not in unlimited.flags
    assert unlimited.owner == OWNER
    assert unlimited.tx_from == RELAYER
    assert unlimited.owner != unlimited.tx_from
    fresh = row_for(result, SP_NEW)
    assert fresh.change == "new" and fresh.status == "active"
    assert fresh.raw_amount == str(10**18)
    assert int(fresh.raw_amount) > 2**53
    assert row_for(result, SP_UP).change == "increased"
    decreased = row_for(result, SP_DOWN)
    assert decreased.change == "decreased" and decreased.status == "active"
    assert decreased.raw_amount == str(10**6)
    revoked = row_for(result, SP_REVOKE)
    assert revoked.change == "revoked" and revoked.status == "revoked"
    consumed = row_for(result, SP_CONSUMED)
    assert consumed.change == "consumed" and consumed.status == "consumed"
    assert consumed.status != "revoked" and consumed.change != "revoked"
    first = row_for(result, SP_FIRST)
    assert first.change == "first_observed" and first.change != "new"
    assert first.status == "active"
    assert "unlimited" in row_for(result, SP_FLOOR).flags
    below = scan(
        [approval_log(TOKEN, SP_FLOOR, UNLIMITED_FLOOR - 1, floor_tx)],
        {("erc20", TOKEN, SP_FLOOR): erc20_state(UNLIMITED_FLOOR - 1)},
        {("erc20", TOKEN, SP_FLOOR): PriorState(True, amount=0)},
    )
    assert "unlimited" not in row_for(below, SP_FLOOR).flags
    high = row_for(result, SP_HIGH)
    assert "high" in high.flags and "unlimited" not in high.flags
    wide = [row for row in result.rows if row.raw_amount == str(2**53 + 9)]
    assert len(wide) == 1 and int(wide[0].raw_amount) > 2**53
    assert result.cursor == "20"
    assert result.probe == "untested"
    assert result.coverage["permissions.erc20"].category == "ok"


def test_mcm_w5_01_nft_and_operator_families_stay_separate():
    """MCM-W5-01 approval-families. 721 single-token, 721 operator, and 1155 operator differ."""
    approve_tx = tx_hash("a1")
    clear_tx = tx_hash("a2")
    op721_tx = tx_hash("a3")
    op1155_tx = tx_hash("a4")
    unknown_tx = tx_hash("a5")
    both_tx = tx_hash("a6")
    logs = [
        nft_log(TOKEN_721, OPERATOR, 7, approve_tx),
        nft_log(TOKEN_721, ZERO_ADDRESS, 8, clear_tx),
        operator_log(TOKEN_721, OPERATOR, True, op721_tx),
        operator_log(TOKEN_1155, OPERATOR, True, op1155_tx),
        operator_log(TOKEN_UNKNOWN, OPERATOR, True, unknown_tx),
        operator_log(TOKEN_BOTH, OPERATOR, True, both_tx),
    ]
    evidence = {
        TOKEN_721: ContractEvidence(erc165_erc721=True, erc165_erc1155=False),
        TOKEN_1155: ContractEvidence(transfer_kind="erc1155"),
        TOKEN_BOTH: ContractEvidence(erc165_erc721=True, erc165_erc1155=True),
    }
    states = {
        ("erc721", TOKEN_721, "7"): StateRead(True, amount=1, approved=True, approved_for=OPERATOR),
        ("erc721", TOKEN_721, "8"): StateRead(True, amount=0, approved=False, approved_for=ZERO_ADDRESS),
        ("operator", "erc721", TOKEN_721, OPERATOR): StateRead(True, approved=True, amount=1),
        ("operator", "erc1155", TOKEN_1155, OPERATOR): StateRead(True, approved=True, amount=1),
        ("operator", "unknown", TOKEN_UNKNOWN, OPERATOR): StateRead(True, approved=True, amount=1),
        ("operator", "unknown", TOKEN_BOTH, OPERATOR): StateRead(True, approved=True, amount=1),
    }
    prior = {
        ("erc721", TOKEN_721, "7"): PriorState(True, amount=0, approved=False, spender=ZERO_ADDRESS),
        ("erc721", TOKEN_721, "8"): PriorState(True, amount=1, approved=True, spender=OPERATOR),
        ("operator", "erc721", TOKEN_721, OPERATOR): PriorState(True, approved=False),
        ("operator", "erc1155", TOKEN_1155, OPERATOR): PriorState(True, approved=False),
    }
    transport = world()
    install(
        transport,
        eth_call_operation(TOKEN_721, encode_view_call("supportsInterface(bytes4)", [ERC721_INTERFACE])),
        rpc_ok(word(1)),
    )
    install(
        transport,
        eth_call_operation(TOKEN_721, encode_view_call("supportsInterface(bytes4)", [ERC1155_INTERFACE])),
        rpc_ok(word(0)),
    )
    erc721_answer = read_supports_interface(transport, "evm-rpc", TOKEN_721, ERC721_INTERFACE)
    erc1155_answer = read_supports_interface(transport, "evm-rpc", TOKEN_721, ERC1155_INTERFACE)
    assert erc721_answer is True and erc1155_answer is False
    evidence[TOKEN_721] = ContractEvidence(erc165_erc721=erc721_answer, erc165_erc1155=erc1155_answer)
    result = scan(logs, states, prior, evidence=evidence)
    single = [row for row in result.rows if row.token_id == "7"]
    assert len(single) == 1
    assert single[0].family == "erc721_token"
    assert single[0].capability == "permissions.erc721_token"
    assert single[0].change == "new" and single[0].status == "active"
    assert single[0].spender == OPERATOR
    cleared = [row for row in result.rows if row.token_id == "8"]
    assert cleared[0].family == "erc721_token"
    assert cleared[0].change == "revoked" and cleared[0].status == "revoked"
    assert cleared[0].capability != "permissions.erc721_operator"
    families = {row.family for row in result.rows}
    assert "erc721_operator" in families
    assert "erc1155_operator" in families
    assert "operator_unknown_standard" in families
    erc721_ops = [row for row in result.rows if row.family == "erc721_operator"]
    erc1155_ops = [row for row in result.rows if row.family == "erc1155_operator"]
    unknown_ops = [row for row in result.rows if row.family == "operator_unknown_standard"]
    assert len(erc721_ops) == 1 and erc721_ops[0].token == TOKEN_721
    assert erc721_ops[0].capability == "permissions.erc721_operator"
    assert erc721_ops[0].status == "active" and "operator_for_all" in erc721_ops[0].flags
    assert len(erc1155_ops) == 1 and erc1155_ops[0].token == TOKEN_1155
    assert erc1155_ops[0].capability == "permissions.erc1155_operator"
    assert erc1155_ops[0].status == "active"
    assert {row.token for row in unknown_ops} == {TOKEN_UNKNOWN, TOKEN_BOTH}
    assert all(row.capability == "operator_unknown_standard" for row in unknown_ops)
    assert all(row.capability not in {"permissions.erc721_operator", "permissions.erc1155_operator"} for row in unknown_ops)
    assert "operator_standard_unknown" in result.coverage["permissions.erc721_operator"].reason_codes
    assert "operator_standard_unknown" in result.coverage["permissions.erc1155_operator"].reason_codes
    assert result.coverage["permissions.erc721_operator"].category == "partial"


def test_mcm_w5_01_failed_receipt_has_no_approval_and_views_are_read_only():
    """MCM-W5-01 approval-families. A successful receipt records the allowance; a revert does not."""
    tx = tx_hash("fa")
    log = approval_log(TOKEN, SP_MAX, UINT256_MAX, tx)
    common = {
        "chain_uid": "eip155:1",
        "network": "ethereum-mainnet",
        "observed_at": "2026-10-08T00:00:00Z",
        "owner": OWNER,
        "tenant_id": TENANT,
        "wallet_id": WALLET_ID,
    }
    success = approval_events_from_receipt(
        {"logs": [log], "status": "0x1", "transactionHash": tx},
        **common,
    )
    failure = approval_events_from_receipt(
        {"logs": [log], "status": "0x0", "transactionHash": tx},
        **common,
    )
    approvals = [event for event in success if event.kind == "approval_set"]
    assert len(approvals) == 1
    assert approvals[0].tx_status == "success"
    assert approvals[0].raw_amount == str(UINT256_MAX)
    assert int(approvals[0].raw_amount) > 2**53
    assert [event.kind for event in failure] == ["tx_failed"]
    assert all(event.kind not in {"approval_set", "approval_revoked", "operator_set"} for event in failure)
    allowance = encode_view_call("allowance(address,address)", [OWNER, SP_MAX])
    assert allowance.startswith("0xdd62ed3e")
    assert encode_view_call("supportsInterface(bytes4)", [ERC721_INTERFACE]).startswith("0x01ffc9a7")
    assert ERC721_INTERFACE.hex() == "80ac58cd"
    assert ERC1155_INTERFACE.hex() == "d9b67a26"
    for signature in (
        "approve(address,uint256)",
        "increaseAllowance(address,uint256)",
        "permit(address,address,uint256,uint256,uint8,bytes32,bytes32)",
        "setApprovalForAll(address,bool)",
    ):
        with pytest.raises(ValueError, match="not_a_view_call"):
            encode_view_call(signature, [OWNER, 1])


def test_mcm_w5_01_current_state_wins_when_the_log_disagrees():
    """MCM-W5-01 approval-families. The eth_call allowance replaces a conflicting log."""
    tx = tx_hash("mm")
    key = ("erc20", TOKEN, SP_NEW)
    result = scan(
        [approval_log(TOKEN, SP_NEW, 500, tx)],
        {key: erc20_state(80)},
        {key: PriorState(True, amount=100)},
    )
    found = row_for(result, SP_NEW)
    assert found.raw_amount == "80"
    assert found.change == "decreased"
    assert "event_state_mismatch" in found.reason_codes
    assert found.status == "active"


def test_mcm_w5_02_relayed_and_multicall_permits_use_the_owner():
    """MCM-W5-02 relayed-permit. A relayer and a multicall still attribute the owner."""
    direct_tx = tx_hash("d1")
    multi_tx = tx_hash("d2")
    dai_tx = tx_hash("d3")
    stuck_tx = tx_hash("d4")
    decoy_tx = tx_hash("d5")
    direct = "0x" + permit2612_call(OWNER, SP_NEW, 1000).hex()
    wrapped = multicall_wrap(permit2612_call(OWNER, SP_UP, 2500))
    dai = "0x" + dai_call(OWNER, SP_DOWN, allowed=True, nonce=7).hex()
    stuck = "0x" + dai_call(OWNER, SP_REVOKE, allowed=True, nonce=1).hex()
    decoy_input = "0x" + permit2612_call(RELAYER, SP_NEW, 1000).hex()
    transport = world()
    install(
        transport,
        eth_call_operation(TOKEN, encode_view_call("nonces(address)", [OWNER])),
        rpc_ok(word(5)),
    )
    after = read_token_nonce(transport, "evm-rpc", TOKEN, OWNER)
    assert after == 5

    def observed(tx, calldata, spender, amount, before, after_nonce):
        return observe_submitted_permits(
            [{"from": RELAYER, "hash": tx, "input": calldata, "to": TOKEN}],
            [approval_log(TOKEN, spender, amount, tx)],
            OWNER,
            nonces={TOKEN: (before, after_nonce)},
        )

    direct_permit = observed(direct_tx, direct, SP_NEW, 1000, 4, after)[0]
    assert direct_permit.capability == "permits.erc2612_observed"
    assert direct_permit.status == "observed"
    assert direct_permit.owner == OWNER
    assert direct_permit.tx_from == RELAYER
    assert direct_permit.owner != direct_permit.tx_from
    assert direct_permit.via == "direct"
    assert direct_permit.nonce_incremented is True
    assert direct_permit.nonce_before == "4" and direct_permit.nonce_after == "5"
    assert direct_permit.raw_amount == "1000"
    permit_coverage = coverage_for_submitted(
        [direct_permit],
        nonce_known=True,
        pagination_complete=True,
        gap_detection_complete=True,
        previous_cursor="1",
        next_cursor="2",
    )
    assert permit_coverage["permits.erc2612_observed"].category == "ok"
    assert permit_coverage["permits.offchain_unused"].reason_codes == ("offchain_signature_not_observable",)
    multi_permit = observed(multi_tx, wrapped, SP_UP, 2500, 8, 9)[0]
    assert multi_permit.status == "observed"
    assert multi_permit.via == "multicall"
    assert multi_permit.owner == OWNER and multi_permit.tx_from == RELAYER
    assert multi_permit.capability == "permits.erc2612_observed"
    assert multi_permit.raw_amount == "2500"
    dai_permit = observed(dai_tx, dai, SP_DOWN, UINT256_MAX, 1, 2)[0]
    assert dai_permit.capability == "permits.dai_variant"
    assert dai_permit.capability != "permits.erc2612_observed"
    assert dai_permit.allowed is True
    assert dai_permit.status == "observed"
    assert dai_permit.owner == OWNER and dai_permit.tx_from == RELAYER
    assert dai_permit.calldata_nonce == "7"
    stuck_permit = observed(stuck_tx, stuck, SP_REVOKE, UINT256_MAX, 2, 2)[0]
    assert stuck_permit.status == "unknown"
    assert stuck_permit.nonce_incremented is False
    decoy = observe_submitted_permits(
        [{"from": OWNER, "hash": decoy_tx, "input": decoy_input, "to": TOKEN}],
        [approval_log(TOKEN, SP_NEW, 1000, decoy_tx, owner=RELAYER)],
        OWNER,
        nonces={TOKEN: (4, 5)},
    )
    assert decoy == ()


def test_mcm_w5_03_permit2_layers_expiration_and_signature_use():
    """MCM-W5-03 permit2-layers. Token approval, spender allowance, and a used nonce stay apart."""
    deployments = load_permit2_deployments(ROOT / "config" / "deployments" / "permit2")
    registry = load_registry(ROOT / "config" / "chains")
    t0 = {network for network, entry in registry.items() if entry["family"] == "evm" and entry["tier"] == 0}
    assert set(deployments) == t0
    assert len(deployments) == 7
    for network, deployment in deployments.items():
        text = (ROOT / "config" / "deployments" / "permit2" / f"{network}.json").read_text(encoding="utf-8")
        assert deployment.address == CANONICAL_PERMIT2
        assert deployment.status == "documented"
        assert deployment.source_url == "https://github.com/Uniswap/permit2"
        assert "not verified on-chain by this package" in deployment.notes
        assert "live_pass" not in text
        assert "enabled" not in json.loads(text)
    with pytest.raises(Permit2ConfigError):
        validate_permit2_document({
            "address": CANONICAL_PERMIT2,
            "network": "ethereum-mainnet",
            "notes": "not verified on-chain by this package",
            "retrieved": "2026-10-08",
            "source_url": "https://github.com/Uniswap/permit2",
            "status": "live_pass",
        })
    token_tx = tx_hash("p1")
    allowance_tx = tx_hash("p2")
    permit_tx = tx_hash("p3")
    lock_tx = tx_hash("p4")
    nonce_tx = tx_hash("p5")
    sig_tx = tx_hash("p6")
    expired_tx = tx_hash("p7")
    future = NOW + 10_000
    past = NOW - 50
    token_log = approval_log(TOKEN, PERMIT2, UINT256_MAX, token_tx)
    allowance_log = {
        "address": CANONICAL_PERMIT2,
        "data": permit2_data(12_345, future),
        "logIndex": "0x1",
        "topics": [PERMIT2_APPROVAL_TOPIC, pad_address(OWNER), pad_address(TOKEN), pad_address(SP_NEW)],
        "transactionHash": allowance_tx,
    }
    permit_log = {
        "address": CANONICAL_PERMIT2,
        "data": permit2_data(12_345, future, 9),
        "logIndex": "0x2",
        "topics": [PERMIT2_PERMIT_TOPIC, pad_address(OWNER), pad_address(TOKEN), pad_address(SP_UP)],
        "transactionHash": permit_tx,
    }
    lockdown_log = {
        "address": CANONICAL_PERMIT2,
        "data": "0x" + encode(["address", "address"], [TOKEN, SP_REVOKE]).hex(),
        "logIndex": "0x3",
        "topics": [PERMIT2_LOCKDOWN_TOPIC, pad_address(OWNER)],
        "transactionHash": lock_tx,
    }
    nonce_log = {
        "address": CANONICAL_PERMIT2,
        "data": permit2_data(6, 2),
        "logIndex": "0x4",
        "topics": [PERMIT2_NONCE_TOPIC, pad_address(OWNER), pad_address(TOKEN), pad_address(SP_HIGH)],
        "transactionHash": nonce_tx,
    }
    signature_log = {
        "address": CANONICAL_PERMIT2,
        "data": permit2_data(0, 1 << 1),
        "logIndex": "0x5",
        "topics": [PERMIT2_UNORDERED_TOPIC, pad_address(OWNER)],
        "transactionHash": sig_tx,
    }
    expired_log = {
        "address": CANONICAL_PERMIT2,
        "data": permit2_data(80, past),
        "logIndex": "0x6",
        "topics": [PERMIT2_APPROVAL_TOPIC, pad_address(OWNER), pad_address(TOKEN), pad_address(SP_DOWN)],
        "transactionHash": expired_tx,
    }
    ignored, _reasons = parse_approval_logs([allowance_log], OWNER)
    assert ignored == ()
    erc20 = scan(
        [token_log],
        {("erc20", TOKEN, PERMIT2): erc20_state(UINT256_MAX)},
        {("erc20", TOKEN, PERMIT2): PriorState(True, amount=0)},
        permit2_address=CANONICAL_PERMIT2,
    )
    transport = world()
    allowance_data = encode_view_call("allowance(address,address,address)", [OWNER, TOKEN, SP_NEW])
    assert allowance_data.startswith("0x927da105")
    install(
        transport,
        eth_call_operation(CANONICAL_PERMIT2, allowance_data),
        rpc_ok(permit2_data(12_345, future, 4)),
    )
    current = read_permit2_allowance(transport, "evm-rpc", CANONICAL_PERMIT2, OWNER, TOKEN, SP_NEW)
    assert current.ok is True
    assert current.amount == 12_345 and current.expiration == future and current.nonce == 4
    report = reconcile_permit2(
        [allowance_log, permit_log, lockdown_log, nonce_log, signature_log, expired_log],
        OWNER,
        CANONICAL_PERMIT2,
        states={
            (TOKEN, SP_NEW): current,
            (TOKEN, SP_UP): StateRead(True, amount=12_345, expiration=future, nonce=9),
            (TOKEN, SP_REVOKE): StateRead(True, amount=0, expiration=0, nonce=3),
            (TOKEN, SP_HIGH): StateRead(True, amount=12_345, expiration=future, nonce=6),
            (TOKEN, SP_DOWN): StateRead(True, amount=80, expiration=past, nonce=1),
        },
        prior={
            (TOKEN, SP_NEW): PriorState(True, amount=12_345, expiration=NOW - 10, nonce=4),
            (TOKEN, SP_UP): PriorState(True, amount=1, expiration=NOW - 10, nonce=8),
            (TOKEN, SP_REVOKE): PriorState(True, amount=12_345, expiration=future, nonce=3),
            (TOKEN, SP_HIGH): PriorState(True, amount=12_345, expiration=future, nonce=2),
            (TOKEN, SP_DOWN): PriorState(True, amount=80, expiration=past, nonce=1),
        },
        pagination_complete=True,
        gap_detection_complete=True,
        now=NOW,
        previous_cursor="3",
        next_cursor="30",
        erc20_rows=erc20.rows,
    )
    token_layer = report.erc20_to_permit2
    assert len(token_layer) == 1
    assert token_layer[0].layer == "erc20_to_permit2"
    assert token_layer[0].capability == "permissions.erc20"
    assert token_layer[0].spender == PERMIT2
    assert token_layer[0].status == "active"
    assert "unlimited" in token_layer[0].flags
    assert int(token_layer[0].raw_amount) == UINT256_MAX
    by_spender = {item.spender: item for item in report.allowances}
    active = by_spender[SP_NEW]
    assert active.layer == "permit2_to_spender"
    assert active.capability == "permit2.allowance"
    assert active.capability != token_layer[0].capability
    assert active.spender != token_layer[0].spender
    assert active.status == "active"
    assert active.change == "extended"
    assert active.persistent_allowance is True
    assert active.raw_amount == "12345"
    assert active.expiration == str(future)
    assert active.nonce == "4"
    assert by_spender[SP_UP].status == "active"
    assert by_spender[SP_UP].capability == "permit2.allowance"
    assert by_spender[SP_UP].nonce == "9"
    assert by_spender[SP_DOWN].status == "expired"
    assert by_spender[SP_DOWN].status != "active"
    assert by_spender[SP_REVOKE].change == "revoked" and by_spender[SP_REVOKE].status == "revoked"
    nonce_row = by_spender[SP_HIGH]
    assert nonce_row.status == "active"
    assert nonce_row.change != "revoked"
    assert "nonce_invalidated" in nonce_row.reason_codes
    assert nonce_row.nonce == "6"
    assert len(report.signature_uses) == 1
    use = report.signature_uses[0]
    assert use.capability == "permit2.signature_usage"
    assert use.persistent_allowance is False
    assert use.nonce == "1"
    assert use.source == "unordered_nonce_invalidation"
    assert all(item.capability != "permit2.signature_usage" for item in report.allowances)
    assert used_nonces(0, 0b101) == (0, 2)
    assert 1 not in used_nonces(0, 0b101)
    bitmap = unsubmitted_signatures(0b101, "none")
    assert bitmap.support == "not_observable"
    assert bitmap.quality == "unknown"
    assert bitmap.reason == "offchain_signature_not_observable"
    assert report.offchain.quality == "unknown"
    assert report.cursor == "30"
    assert report.probe == "untested"
    _events, uses, reasons = parse_permit2_logs([signature_log], OWNER, CANONICAL_PERMIT2)
    assert reasons == ()
    assert [item.nonce for item in uses] == ["1"]


def test_mcm_w5_04_zero_balance_exposure_is_recorded():
    """MCM-W5-04 offline-blindspot. A live allowance with a zero balance is still recorded."""
    tx = tx_hash("z1")
    from guardian.chains.evm.permissions import approval_log_operation

    transport = world()
    install(transport, approval_log_operation(OWNER, 1, 10), rpc_ok([approval_log(TOKEN, SP_NEW, 1000, tx)]))
    install(
        transport,
        eth_call_operation(TOKEN, encode_view_call("allowance(address,address)", [OWNER, SP_NEW])),
        rpc_ok(word(1000)),
    )
    install(
        transport,
        eth_call_operation(TOKEN, encode_view_call("balanceOf(address)", [OWNER])),
        rpc_ok(word(0)),
    )
    result = scan_approvals(
        transport,
        "evm-rpc",
        OWNER,
        1,
        10,
        previous_cursor="4",
        pagination_complete=True,
        gap_detection_complete=True,
    )
    assert len(result.rows) == 1
    found = result.rows[0]
    assert found.status == "active"
    assert found.raw_amount == "1000"
    assert int(found.raw_amount) == 1000
    assert "zero_balance_exposure" in found.flags
    assert "unlabeled" in found.flags
    assert "safe" not in found.flags
    assert found.owner == OWNER and found.spender == SP_NEW
    assert result.cursor == "10"
    balance_ok, balance = read_token_balance(transport, "evm-rpc", TOKEN, OWNER)
    assert balance_ok is True and balance == 0
    allowance = read_erc20_allowance(transport, "evm-rpc", TOKEN, OWNER, SP_NEW)
    assert allowance.ok is True and allowance.amount == 1000


def test_mcm_w5_04_incomplete_history_is_not_an_empty_approval_set():
    """MCM-W5-04 offline-blindspot. Missing history is not 'no approvals'."""
    result = reconcile_approvals(
        [],
        OWNER,
        states={},
        pagination_complete=False,
        gap_detection_complete=False,
        gaps=("11-12",),
        previous_cursor="8",
        next_cursor="40",
    )
    assert result.rows == ()
    assert result.cursor == "8"
    assert result.summary == "history_incomplete_permissions_unknown"
    assert "no approvals" not in result.summary
    assert result.summary != "safe"
    observation = result.coverage["permissions.erc20"]
    assert observation.category != "ok"
    assert "history_incomplete_permissions_unknown" in observation.reason_codes
    assert observation.cursor == "8"
    gapped = scan(
        [approval_log(TOKEN, SP_NEW, 5, tx_hash("gg"))],
        {("erc20", TOKEN, SP_NEW): erc20_state(5)},
        gaps=("1-2",),
        previous_cursor="2",
        next_cursor="9",
    )
    assert gapped.cursor == "2"
    assert gapped.summary == "history_incomplete_permissions_unknown"
    assert gapped.rows[0].status == "active"


def test_mcm_w5_04_failed_allowance_read_stays_unknown_and_keeps_cursor():
    """MCM-W5-04 offline-blindspot. A failed current read is not a revoke."""
    tx = tx_hash("rf")
    from guardian.chains.evm.permissions import approval_log_operation

    transport = world()
    install(transport, approval_log_operation(OWNER, 20, 30), rpc_ok([approval_log(TOKEN, SP_REVOKE, 0, tx)]))
    install(
        transport,
        eth_call_operation(TOKEN, encode_view_call("allowance(address,address)", [OWNER, SP_REVOKE])),
        rpc_error(),
    )
    result = scan_approvals(
        transport,
        "evm-rpc",
        OWNER,
        20,
        30,
        previous_cursor="15",
        prior={("erc20", TOKEN, SP_REVOKE): PriorState(True, amount=200)},
        pagination_complete=True,
        gap_detection_complete=True,
        read_balances=False,
    )
    assert len(result.rows) == 1
    found = result.rows[0]
    assert found.status == "unknown"
    assert found.change != "revoked"
    assert found.status != "revoked"
    assert found.raw_amount is None
    assert "current_state_unreadable" in found.reason_codes
    assert result.cursor == "15"
    assert result.summary == "history_incomplete_permissions_unknown"
    assert result.coverage["permissions.erc20"].cursor == "15"
    blind = scan_approvals(
        transport,
        "evm-rpc",
        OWNER,
        40,
        50,
        previous_cursor="15",
        read_balances=False,
    )
    assert blind.rows == ()
    assert blind.cursor == "15"
    assert blind.summary == "history_incomplete_permissions_unknown"


def test_mcm_w5_04_offchain_signatures_stay_not_observable():
    """MCM-W5-04 offline-blindspot. An unsubmitted signature is never safe or none."""
    for claim in ("safe", "none", "no approvals", None):
        blind = offchain_unused_status(claim)
        assert blind.capability == "permits.offchain_unused"
        assert blind.support == "not_observable"
        assert blind.quality == "unknown"
        assert blind.reason == "offchain_signature_not_observable"
        assert blind.observation.reason_codes == ("offchain_signature_not_observable",)
    same = unsubmitted_signatures(0, "safe")
    assert same.support == "not_observable" and same.quality == "unknown"
