"""Offline acceptance for MCM-W6. Synthetic fixtures only. No network.

MCM-W6-01 owner-token-account-set
MCM-W6-02 transaction-version
MCM-W6-03 authority-diff
MCM-W6-04 lamports-effects
"""

from __future__ import annotations

import json
import socket
from decimal import Decimal
from pathlib import Path

import pytest

from guardian.amounts import format_units
from guardian.chains.solana.adapter import (
    CLOSED_HISTORY_GAP,
    DEFAULT_COMMITMENT,
    PAGE_CAP_GAP,
    SPL_TOKEN_PROGRAM,
    SYSTEM_PROGRAM,
    TOKEN_2022_PROGRAM,
    UNKNOWN_EXTENSION,
    UNKNOWN_HISTORY_GAP,
    WRAPPED_SOL_MINT,
    SolanaAdapter,
    balance_operation,
    genesis_hash_operation,
    signatures_operation,
    token_accounts_operation,
    transaction_operation,
)
from guardian.chains.solana.authorities import PriorAuthority, diff_authorities, parse_mint_account
from guardian.chains.solana.tokens import TokenAccountView, parse_token_account
from guardian.chains.solana.transactions import parse_transaction
from guardian.errors import SecurityError
from guardian.identities import _b58encode, parse_address
from guardian.network_registry import load_registry
from guardian.security.transport_policy import Operation, assert_operation_allowed
from guardian.sources.budget import BudgetBook
from guardian.sources.capabilities import load_source_capabilities
from guardian.sources.fixtures import FixtureBackend, RawResponse, request_key
from guardian.sources.helius import (
    ENRICHMENT_REASON,
    authoritative_amount,
    coverage_from_enrichment,
    open_helius,
)
from guardian.sources.transport import ReadOnlyTransport, build_endpoint_registry

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "tests" / "fixtures" / "solana"
PUBLIC_IP = "1.1.1.1"
RPC_URL = "https://rpc.example.invalid"
HELIUS_URL = "https://helius.example.invalid"
TENANT = "tenant-synthetic"
WALLET_ID = "11111111-1111-4111-8111-111111111111"
NOW = "2026-10-08T00:00:00Z"
OBSERVED = NOW
PREVIOUS = "cursor-keep-1"
KEY = "heli-test-secret-value"
RAW_HUGE = 2**53 + 1
RENT = 2_039_280
FEE = 5_000


def synthetic_address(number: int) -> str:
    text = _b58encode(bytes([number]) * 32)
    assert parse_address("solana", text).canonical == text
    return text


def synthetic_signature(number: int) -> str:
    return _b58encode(bytes([number]) * 64)


OWNER = synthetic_address(1)
ATA = synthetic_address(2)
MINT = synthetic_address(3)
DELEGATE_OLD = synthetic_address(4)
DELEGATE_NEW = synthetic_address(5)
ATA_2022 = synthetic_address(6)
MINT_2022 = synthetic_address(7)
CLOSE_AUTH = synthetic_address(8)
PAYER = synthetic_address(9)
COUNTERPARTY = synthetic_address(10)
ALT_WRITABLE = synthetic_address(11)
ALT_READONLY = synthetic_address(12)
PERMANENT = synthetic_address(13)
MINT_AUTH = synthetic_address(14)
FREEZE_AUTH = synthetic_address(15)
SIG = synthetic_signature(1)
SIG_OLD = synthetic_signature(2)


@pytest.fixture(autouse=True)
def _offline(monkeypatch):
    def explode(*_args, **_kwargs):
        raise AssertionError("network is forbidden in MCM-W6 tests")

    monkeypatch.setattr(socket, "socket", explode)
    monkeypatch.setattr(socket, "create_connection", explode)
    monkeypatch.setattr(socket, "getaddrinfo", explode)
    import httpx

    monkeypatch.setattr(httpx, "Client", explode)


def _resolve(_host: str) -> list[str]:
    return [PUBLIC_IP]


def registry():
    return load_registry(ROOT / "config" / "chains")


def entry():
    found = registry()["solana-mainnet"]
    assert found["enabled"] is False
    assert found["status"] == "planned_not_live_verified"
    return found


def world(*, daily: int = 1000, retry_attempts: int = 0, rpc: bool = True, helius: bool = True):
    document = {
        "budgets": {
            "helius": {"max_concurrency": 2, "max_requests_per_day": daily, "max_rps": 100, "retry_attempts": retry_attempts},
            "solana-rpc": {"max_concurrency": 4, "max_requests_per_day": daily, "max_rps": 100, "retry_attempts": retry_attempts},
        },
        "sources": {
            "helius": {"adapter": "helius_parsed_events", "budget_group": "helius", "endpoint_env": "HELIUS_PARSED_EVENTS_URL"},
            "solana-rpc": {"adapter": "solana_rpc", "budget_group": "solana-rpc", "endpoint_env": "SOLANA_RPC_URL"},
        },
    }
    secrets = {}
    if rpc:
        secrets["SOLANA_RPC_URL"] = RPC_URL
    if helius:
        secrets["HELIUS_PARSED_EVENTS_URL"] = HELIUS_URL
    endpoints = build_endpoint_registry(document, secrets=secrets)
    book = BudgetBook.from_document(document["budgets"], clock=lambda: 1_700_000_000.0, sleep=lambda _seconds: None)
    backend = FixtureBackend({})
    return ReadOnlyTransport(endpoints, book, backend, resolve=_resolve)


def install(transport, source: str, operation, payload) -> None:
    call = transport.encode(source, operation)
    if isinstance(payload, RawResponse):
        body = payload
    else:
        body = RawResponse(200, {}, json.dumps(payload).encode("utf-8"))
    transport.backend.mapping[request_key(call.method, call.url, call.body)] = body


def install_many(transport, source: str, operation, payloads: list) -> None:
    call = transport.encode(source, operation)
    bodies = []
    for payload in payloads:
        if isinstance(payload, RawResponse):
            bodies.append(payload)
        else:
            bodies.append(RawResponse(200, {}, json.dumps(payload).encode("utf-8")))
    transport.backend.mapping[request_key(call.method, call.url, call.body)] = bodies


def rpc_ok(result):
    return {"id": 1, "jsonrpc": "2.0", "result": result}


def rpc_error():
    return {"error": {"code": -32000, "message": "synthetic failure"}, "id": 1, "jsonrpc": "2.0"}


def methods(transport) -> list[str]:
    return [json.loads(call.body.decode("utf-8"))["method"] for call in transport.backend.calls]


def open_session(transport=None):
    transport = transport or world()
    found = entry()
    install(transport, "solana-rpc", genesis_hash_operation(), rpc_ok(found["identity"]["expected_value"]))
    session = SolanaAdapter(found, transport, rpc_source_id="solana-rpc").open()
    assert session.identity.ok is True
    assert session.identity.genesis == found["identity"]["expected_value"]
    assert session.probe != "live_pass"
    return session


def token_account_json(address, program, mint, owner, amount, decimals, *, delegate=None, delegated=None, close_authority=None, extensions=None, state="initialized"):
    info = {
        "closeAuthority": close_authority,
        "delegate": delegate,
        "extensions": extensions or [],
        "isNative": mint == WRAPPED_SOL_MINT,
        "mint": mint,
        "owner": owner,
        "state": state,
        "tokenAmount": {"amount": str(amount), "decimals": decimals, "uiAmount": 0.1, "uiAmountString": "0.1"},
    }
    if delegate is None:
        info["delegatedAmount"] = {"amount": "0", "decimals": decimals, "uiAmount": 0.0, "uiAmountString": "0"}
    else:
        info["delegatedAmount"] = {
            "amount": str(0 if delegated is None else delegated),
            "decimals": decimals,
            "uiAmount": 0.1,
            "uiAmountString": "0.1",
        }
    return {
        "account": {
            "data": {"parsed": {"info": info, "type": "account"}, "program": "spl-token", "space": 165},
            "executable": False,
            "lamports": RENT,
            "owner": program,
            "rentEpoch": 0,
        },
        "pubkey": address,
    }


def token_balance(index, mint, owner, amount, decimals, program):
    return {
        "accountIndex": index,
        "mint": mint,
        "owner": owner,
        "programId": program,
        "uiTokenAmount": {"amount": str(amount), "decimals": decimals, "uiAmount": 1.0, "uiAmountString": "1"},
    }


def account_key(pubkey, *, signer=False, writable=False):
    return {"pubkey": pubkey, "signer": signer, "source": "transaction", "writable": writable}


def parsed_ix(program, ix_type, info):
    return {"parsed": {"info": info, "type": ix_type}, "program": "spl-token", "programId": program}


def tx_result(keys, instructions, pre, post, *, signature=SIG, slot=42, fee=FEE, err=None, version="legacy", pre_token=None, post_token=None, inner=None, loaded=None, lookups=None):
    message = {"accountKeys": keys, "instructions": instructions, "recentBlockhash": "synthetic-blockhash"}
    if lookups is not None:
        message["addressTableLookups"] = lookups
    meta = {
        "err": err,
        "fee": fee,
        "innerInstructions": inner or [],
        "postBalances": post,
        "postTokenBalances": post_token or [],
        "preBalances": pre,
        "preTokenBalances": pre_token or [],
    }
    if loaded is not None:
        meta["loadedAddresses"] = loaded
    body = {"meta": meta, "slot": slot, "transaction": {"message": message, "signatures": [signature]}}
    if version is not None:
        body["version"] = version
    return body


def parse_body(body, *, owner=OWNER, owned=None, commitment="confirmed", unknown=()):
    return parse_transaction(
        body,
        entry=entry(),
        commitment=commitment,
        owner=owner,
        owned_accounts=set(owned or ()),
        tenant_id=TENANT,
        wallet_id=WALLET_ID,
        observed_at=OBSERVED,
        signature=body.get("transaction", {}).get("signatures", [""])[0] if isinstance(body, dict) else "",
        unknown_extensions=unknown,
    )


def view_account(address, mint, amount, *, program=SPL_TOKEN_PROGRAM, decimals=6, delegate=None, delegated=None, close_authority=None, owner=OWNER, extensions=None):
    parsed = parse_token_account(
        token_account_json(
            address, program, mint, owner, amount, decimals,
            delegate=delegate, delegated=delegated, close_authority=close_authority, extensions=extensions,
        ),
        program,
    )
    assert parsed is not None
    assert parsed.amount == amount
    assert isinstance(parsed.amount, int)
    return parsed


def test_mcm_w6_01_provenance_matches_the_synthetic_generator():
    document = json.loads((FIXTURES / "provenance.json").read_text(encoding="utf-8"))
    assert document["synthetic"] is True
    assert document["not_a_live_capture"] is True
    assert synthetic_address(1) == document["example_address_1"]
    assert synthetic_signature(1) == document["example_signature_1"]
    assert OWNER not in {
        SYSTEM_PROGRAM, SPL_TOKEN_PROGRAM, TOKEN_2022_PROGRAM, WRAPPED_SOL_MINT,
    }


def test_mcm_w6_01_signature_paging_params_are_explicit():
    operation = signatures_operation(OWNER, limit=2, before=SIG, until=SIG_OLD, commitment="finalized")
    assert operation.method == "getSignaturesForAddress"
    assert operation.adapter == "solana_rpc"
    assert operation.params[1]["before"] == SIG
    assert operation.params[1]["until"] == SIG_OLD
    assert operation.params[1]["limit"] == 2
    assert operation.params[1]["commitment"] == "finalized"
    assert_operation_allowed(operation)
    accounts = token_accounts_operation(OWNER, TOKEN_2022_PROGRAM, "confirmed")
    assert accounts.params[1] == {"programId": TOKEN_2022_PROGRAM}
    assert accounts.params[2]["encoding"] == "jsonParsed"
    assert_operation_allowed(accounts)


def test_mcm_w6_01_incoming_spl_is_captured_when_owner_is_absent_from_account_keys():
    """SPL Token and Token-2022 are both enumerated. The credit is on the ATA."""
    session = open_session()
    transport = session.adapter.transport
    install(transport, "solana-rpc", balance_operation(OWNER), rpc_ok({"context": {"slot": 42}, "value": 1_500_000_000}))
    install(transport, "solana-rpc", token_accounts_operation(OWNER, SPL_TOKEN_PROGRAM), rpc_ok({
        "context": {"slot": 42},
        "value": [token_account_json(ATA, SPL_TOKEN_PROGRAM, MINT, OWNER, 1000001, 6)],
    }))
    install(transport, "solana-rpc", token_accounts_operation(OWNER, TOKEN_2022_PROGRAM), rpc_ok({
        "context": {"slot": 42},
        "value": [token_account_json(ATA_2022, TOKEN_2022_PROGRAM, MINT_2022, OWNER, 7, 0)],
    }))
    empty = rpc_ok([])
    install(transport, "solana-rpc", signatures_operation(OWNER, limit=10), empty)
    install(transport, "solana-rpc", signatures_operation(ATA, limit=10), rpc_ok([
        {"blockTime": 1, "err": None, "memo": None, "signature": SIG, "slot": 42},
    ]))
    install(transport, "solana-rpc", signatures_operation(ATA_2022, limit=10), empty)
    body = tx_result(
        [
            account_key(PAYER, signer=True, writable=True),
            account_key(ATA, writable=True),
            account_key(COUNTERPARTY, writable=True),
            account_key(SPL_TOKEN_PROGRAM),
            account_key(MINT),
        ],
        [parsed_ix(SPL_TOKEN_PROGRAM, "transfer", {
            "amount": "1000001",
            "authority": PAYER,
            "destination": ATA,
            "source": COUNTERPARTY,
        })],
        [2_000_000, RENT, RENT, 1, 1],
        [2_000_000 - FEE, RENT, RENT, 1, 1],
        pre_token=[
            token_balance(2, MINT, PAYER, 5_000_001, 6, SPL_TOKEN_PROGRAM),
            token_balance(1, MINT, OWNER, 0, 6, SPL_TOKEN_PROGRAM),
        ],
        post_token=[
            token_balance(2, MINT, PAYER, 4_000_000, 6, SPL_TOKEN_PROGRAM),
            token_balance(1, MINT, OWNER, 1000001, 6, SPL_TOKEN_PROGRAM),
        ],
    )
    install(transport, "solana-rpc", transaction_operation(SIG), rpc_ok(body))
    scan = session.scan(
        OWNER,
        prior_token_accounts=(),
        previous_cursor=None,
        limit=10,
        page_cap=5,
        tenant_id=TENANT,
        wallet_id=WALLET_ID,
        observed_at=OBSERVED,
    )
    assert [item.program_id for item in scan.accounts] == [SPL_TOKEN_PROGRAM, TOKEN_2022_PROGRAM]
    assert {item.address for item in scan.accounts} == {ATA, ATA_2022}
    assert next(item.amount for item in scan.accounts if item.address == ATA_2022) == 7
    assert next(item.decimals for item in scan.accounts if item.address == ATA_2022) == 0
    assert scan.transfers
    credit = scan.transfers[0]
    assert credit.amount == 1000001
    assert credit.delta == 1000001
    assert credit.account == ATA
    assert credit.owner == OWNER
    assert credit.owner_in_account_keys is False
    assert credit.via == "token_account"
    assert OWNER not in scan.transactions[0].account_keys
    assert scan.pagination_complete is True
    assert scan.gap_detection_complete is True
    assert scan.gaps == ()
    assert scan.cursor == SIG
    assert scan.native_lamports == 1_500_000_000
    assert scan.probe == "untested"
    called = [json.loads(call.body.decode("utf-8")) for call in transport.backend.calls]
    programs = [item["params"][1]["programId"] for item in called if item["method"] == "getTokenAccountsByOwner"]
    assert programs == [SPL_TOKEN_PROGRAM, TOKEN_2022_PROGRAM]
    fetched = [item for item in called if item["method"] == "getTransaction"]
    assert fetched[0]["params"][1]["encoding"] == "jsonParsed"
    assert fetched[0]["params"][1]["maxSupportedTransactionVersion"] == 0
    assert fetched[0]["params"][1]["commitment"] == DEFAULT_COMMITMENT


def test_mcm_w6_01_closed_account_is_a_gap_not_complete():
    session = open_session()
    transport = session.adapter.transport
    install(transport, "solana-rpc", balance_operation(OWNER), rpc_ok({"context": {"slot": 3}, "value": 10}))
    install(transport, "solana-rpc", token_accounts_operation(OWNER, SPL_TOKEN_PROGRAM), rpc_ok({"context": {"slot": 3}, "value": []}))
    install(transport, "solana-rpc", token_accounts_operation(OWNER, TOKEN_2022_PROGRAM), rpc_ok({"context": {"slot": 3}, "value": []}))
    install(transport, "solana-rpc", signatures_operation(OWNER, limit=10), rpc_ok([]))
    install(transport, "solana-rpc", signatures_operation(ATA, limit=10), rpc_ok([]))
    scan = session.scan(
        OWNER, prior_token_accounts=(ATA,), previous_cursor=PREVIOUS, limit=10,
        tenant_id=TENANT, wallet_id=WALLET_ID, observed_at=OBSERVED,
    )
    assert CLOSED_HISTORY_GAP in scan.gaps
    assert scan.cursor == PREVIOUS
    fungible = scan.observations["balances.fungible"]
    assert fungible.gaps
    assert fungible.category != "ok" or fungible.gaps
    assert not (
        fungible.category in {"empty", "ok"}
        and fungible.pagination_complete
        and fungible.gap_detection_complete
        and not fungible.gaps
    )


def test_mcm_w6_01_unknown_token_account_history_is_a_gap_not_complete():
    session = open_session()
    transport = session.adapter.transport
    install(transport, "solana-rpc", balance_operation(OWNER), rpc_ok({"context": {"slot": 3}, "value": 10}))
    install(transport, "solana-rpc", token_accounts_operation(OWNER, SPL_TOKEN_PROGRAM), rpc_ok({
        "context": {"slot": 3},
        "value": [token_account_json(ATA, SPL_TOKEN_PROGRAM, MINT, OWNER, 5, 6)],
    }))
    install(transport, "solana-rpc", token_accounts_operation(OWNER, TOKEN_2022_PROGRAM), rpc_ok({"context": {"slot": 3}, "value": []}))
    install(transport, "solana-rpc", signatures_operation(OWNER, limit=10), rpc_ok([]))
    install(transport, "solana-rpc", signatures_operation(ATA, limit=10), rpc_error())
    scan = session.scan(
        OWNER, prior_token_accounts=(), previous_cursor=PREVIOUS, limit=10,
        tenant_id=TENANT, wallet_id=WALLET_ID, observed_at=OBSERVED,
    )
    assert UNKNOWN_HISTORY_GAP in scan.gaps
    assert scan.cursor == PREVIOUS
    assert scan.pagination_complete is False
    assert scan.observations["history.fungible"].category != "ok"
    assert scan.accounts[0].amount == 5


def test_mcm_w6_01_failed_token_read_keeps_cursor_and_is_not_zero():
    session = open_session()
    transport = session.adapter.transport
    install(transport, "solana-rpc", balance_operation(OWNER), rpc_ok({"context": {"slot": 9}, "value": 1_500_000_000}))
    install(transport, "solana-rpc", token_accounts_operation(OWNER, SPL_TOKEN_PROGRAM), rpc_error())
    scan = session.scan(
        OWNER, prior_token_accounts=(), previous_cursor=PREVIOUS, limit=10,
        tenant_id=TENANT, wallet_id=WALLET_ID, observed_at=OBSERVED,
    )
    assert scan.cursor == PREVIOUS
    assert scan.native_lamports == 1_500_000_000
    assert scan.accounts == ()
    fungible = scan.observations["balances.fungible"]
    assert fungible.category == "error"
    assert fungible.cursor == PREVIOUS
    assert scan.authorities is not None
    assert scan.authorities.status == "unknown"
    assert scan.authorities.changes == ()
    assert scan.authorities.cursor == PREVIOUS
    summary = session.adapter.portfolio(
        WALLET_ID,
        scan.observations,
        catalog=load_source_capabilities(ROOT / "config" / "source-capabilities"),
        registry=registry(),
        now=NOW,
    )["summary"]
    assert summary["complete"] is False
    assert "native_only_not_portfolio" in summary["reason_codes"]


def test_mcm_w6_01_signature_page_cap_is_partial_and_keeps_the_cursor():
    session = open_session()
    transport = session.adapter.transport
    install(transport, "solana-rpc", balance_operation(OWNER), rpc_ok({"context": {"slot": 4}, "value": 20}))
    install(transport, "solana-rpc", token_accounts_operation(OWNER, SPL_TOKEN_PROGRAM), rpc_ok({"context": {"slot": 4}, "value": []}))
    install(transport, "solana-rpc", token_accounts_operation(OWNER, TOKEN_2022_PROGRAM), rpc_ok({"context": {"slot": 4}, "value": []}))
    install(transport, "solana-rpc", signatures_operation(OWNER, limit=1), rpc_ok([
        {"blockTime": 1, "err": None, "memo": None, "signature": SIG, "slot": 4},
    ]))
    scan = session.scan(
        OWNER, prior_token_accounts=(), previous_cursor=PREVIOUS, limit=1, page_cap=1,
        tenant_id=TENANT, wallet_id=WALLET_ID, observed_at=OBSERVED,
    )
    assert PAGE_CAP_GAP in scan.gaps
    assert scan.pagination_complete is False
    assert scan.cursor == PREVIOUS
    assert scan.signatures[0].signature == SIG
    assert "getTransaction" not in methods(transport)


def test_mcm_w6_01_budget_cap_keeps_cursor_and_does_not_invent_a_balance():
    transport = world(daily=1)
    found = entry()
    install(transport, "solana-rpc", genesis_hash_operation(), rpc_ok(found["identity"]["expected_value"]))
    session = SolanaAdapter(found, transport, rpc_source_id="solana-rpc").open()
    scan = session.scan(
        OWNER, previous_cursor=PREVIOUS, tenant_id=TENANT, wallet_id=WALLET_ID, observed_at=OBSERVED,
    )
    assert scan.reason == "budget"
    assert "budget" in scan.gaps
    assert scan.cursor == PREVIOUS
    assert scan.native_lamports is None
    assert scan.accounts == ()
    assert methods(transport) == ["getGenesisHash"]


def test_mcm_w6_01_missing_rpc_endpoint_is_blocked_not_empty():
    transport = world(rpc=False)
    session = SolanaAdapter(entry(), transport, rpc_source_id="solana-rpc").open()
    scan = session.scan(
        OWNER, previous_cursor=PREVIOUS, tenant_id=TENANT, wallet_id=WALLET_ID, observed_at=OBSERVED,
    )
    assert scan.blocked is True
    assert scan.reason == "endpoint_missing"
    assert scan.probe == "blocked"
    assert scan.native_lamports is None
    assert scan.cursor == PREVIOUS
    assert scan.accounts == ()
    assert scan.observations["balances.fungible"].category == "error"
    assert transport.backend.calls == []


def test_mcm_w6_01_genesis_mismatch_records_nothing():
    transport = world()
    install(transport, "solana-rpc", genesis_hash_operation(), rpc_ok(synthetic_address(9)))
    install(transport, "solana-rpc", balance_operation(OWNER), rpc_ok({"context": {"slot": 1}, "value": 0}))
    session = SolanaAdapter(entry(), transport, rpc_source_id="solana-rpc").open()
    assert session.identity.ok is False
    assert session.identity.reason == "genesis_mismatch"
    balance = session.native_balance(OWNER)
    assert balance.raw_lamports is None
    assert balance.refused is True
    assert methods(transport) == ["getGenesisHash"]


def test_mcm_w6_02_legacy_transfer_and_v0_alt_inner_are_parsed():
    legacy = tx_result(
        [account_key(OWNER, signer=True, writable=True), account_key(COUNTERPARTY, writable=True), account_key(SYSTEM_PROGRAM)],
        [parsed_ix(SYSTEM_PROGRAM, "transfer", {"destination": COUNTERPARTY, "lamports": 100_000, "source": OWNER})],
        [1_000_000_000, 0, 1],
        [1_000_000_000 - FEE - 100_000, 100_000, 1],
        version="legacy",
    )
    parsed = parse_body(legacy, owned={OWNER})
    assert parsed.version == "legacy"
    assert parsed.status == "success"
    assert parsed.finality == "confirmed"
    assert any(effect.kind == "native_transfer" and effect.raw_amount == 100_000 and effect.direction == "out" for effect in parsed.effects)
    v0 = tx_result(
        [account_key(PAYER, signer=True, writable=True), account_key(SPL_TOKEN_PROGRAM)],
        [],
        [2_000_000, 1, RENT, RENT],
        [2_000_000 - FEE, 1, RENT, RENT],
        version=0,
        loaded={"readonly": [ALT_READONLY], "writable": [ALT_WRITABLE, COUNTERPARTY]},
        lookups=[{"accountKey": ALT_READONLY, "readonlyIndexes": [0], "writableIndexes": [0, 1]}],
        pre_token=[
            token_balance(3, MINT, PAYER, 50, 6, SPL_TOKEN_PROGRAM),
            token_balance(2, MINT, OWNER, 0, 6, SPL_TOKEN_PROGRAM),
        ],
        post_token=[
            token_balance(3, MINT, PAYER, 15, 6, SPL_TOKEN_PROGRAM),
            token_balance(2, MINT, OWNER, 35, 6, SPL_TOKEN_PROGRAM),
        ],
        inner=[{"index": 0, "instructions": [
            parsed_ix(SPL_TOKEN_PROGRAM, "transfer", {
                "amount": "35",
                "authority": PAYER,
                "destination": ALT_WRITABLE,
                "source": COUNTERPARTY,
            }),
            parsed_ix(TOKEN_2022_PROGRAM, "approve", {
                "amount": "35",
                "delegate": DELEGATE_NEW,
                "owner": OWNER,
                "source": ALT_WRITABLE,
            }),
        ]}],
    )
    parsed_v0 = parse_body(v0, owned={ALT_WRITABLE})
    assert parsed_v0.version == "0"
    assert parsed_v0.account_keys == (PAYER, SPL_TOKEN_PROGRAM, ALT_WRITABLE, COUNTERPARTY, ALT_READONLY)
    assert ALT_WRITABLE in parsed_v0.loaded_writable
    assert ALT_READONLY in parsed_v0.loaded_readonly
    assert ALT_WRITABLE not in parsed_v0.static_keys
    assert parsed_v0.inner_instruction_count == 2
    assert any(item.source == "inner" and item.ix_type == "transfer" and item.amount == 35 for item in parsed_v0.instructions)
    assert any(item.source == "inner" and item.ix_type == "approve" and item.amount == 35 for item in parsed_v0.instructions)
    assert any(delta.account == ALT_WRITABLE and delta.delta == 35 for delta in parsed_v0.token_deltas)


def test_mcm_w6_02_failed_meta_null_commitment_and_unsupported_version():
    failed = tx_result(
        [account_key(OWNER, signer=True, writable=True), account_key(ATA, writable=True), account_key(SPL_TOKEN_PROGRAM)],
        [parsed_ix(SPL_TOKEN_PROGRAM, "approve", {"amount": "999", "delegate": DELEGATE_NEW, "owner": OWNER, "source": ATA})],
        [1_000_000, RENT, 1],
        [1_000_000 - FEE, RENT, 1],
        err={"InstructionError": [0, {"Custom": 1}]},
        pre_token=[token_balance(1, MINT, OWNER, 10, 6, SPL_TOKEN_PROGRAM)],
        post_token=[token_balance(1, MINT, OWNER, 10, 6, SPL_TOKEN_PROGRAM)],
    )
    parsed = parse_body(failed, owned={ATA})
    assert parsed.status == "failed"
    assert parsed.token_deltas == ()
    assert [effect.kind for effect in parsed.effects] == ["fee"]
    assert parsed.effects[0].raw_amount == FEE
    kinds = [event.kind for event in parsed.events]
    assert kinds.count("fee") == 1
    assert "tx_failed" in kinds
    assert "asset_in" not in kinds
    assert "asset_out" not in kinds
    assert "delegate_set" not in kinds
    assert parsed.counted_as_final is False

    unavailable = {"slot": 42, "transaction": {"message": {"accountKeys": [], "instructions": []}, "signatures": [SIG]}, "meta": None}
    unknown = parse_body(unavailable)
    assert unknown.status == "unknown"
    assert unknown.status != "success"
    assert unknown.status != "failed"
    assert unknown.effects == ()
    assert unknown.events == ()
    assert unknown.counted_as_final is False

    dropped = parse_body(None)
    assert dropped.status == "unknown"
    assert dropped.finality == "unknown"
    assert dropped.counted_as_final is False

    unsupported = tx_result(
        [account_key(OWNER, signer=True, writable=True)],
        [parsed_ix(SPL_TOKEN_PROGRAM, "approve", {"amount": "5", "delegate": DELEGATE_NEW, "owner": OWNER, "source": ATA})],
        [1_000_000],
        [995_000],
        version=1,
    )
    refused = parse_body(unsupported)
    assert refused.version == "unsupported"
    assert refused.status == "unknown"
    assert "unsupported_transaction_version" in refused.reasons
    assert refused.instructions == ()
    assert refused.effects == ()
    assert refused.fee_lamports is None

    confirmed = parse_body(failed, commitment="confirmed")
    finalized = parse_body(failed, commitment="finalized")
    processed = parse_body(tx_result(
        [account_key(OWNER, signer=True, writable=True), account_key(COUNTERPARTY, writable=True)],
        [],
        [1_000_000, 1],
        [995_000, 1],
    ), commitment="processed")
    assert confirmed.finality == "confirmed"
    assert finalized.finality == "finalized"
    assert confirmed.finality != finalized.finality
    assert processed.finality == "observed"
    assert processed.finality != "finalized"
    assert processed.counted_as_final is False

    session = open_session()
    transport = session.adapter.transport
    success = tx_result(
        [account_key(OWNER, signer=True, writable=True)],
        [],
        [1_000_000],
        [995_000],
    )
    install(transport, "solana-rpc", transaction_operation(SIG, "confirmed"), rpc_ok(success))
    install(transport, "solana-rpc", transaction_operation(SIG, "finalized"), rpc_ok(success))
    install(transport, "solana-rpc", transaction_operation(SIG_OLD, "finalized"), rpc_ok(None))
    left = session.read_transaction(SIG, commitment="confirmed", owner=OWNER, tenant_id=TENANT, wallet_id=WALLET_ID, observed_at=OBSERVED)
    right = session.read_transaction(SIG, commitment="finalized", owner=OWNER, tenant_id=TENANT, wallet_id=WALLET_ID, observed_at=OBSERVED)
    missing = session.read_transaction(SIG_OLD, commitment="finalized", owner=OWNER, tenant_id=TENANT, wallet_id=WALLET_ID, observed_at=OBSERVED)
    assert left.finality == "confirmed"
    assert right.finality == "finalized"
    assert left.fee_lamports == FEE
    assert missing.status == "unknown"
    assert missing.counted_as_final is False
    assert missing.finality == "unknown"


def test_mcm_w6_03_approve_checked_revoke_and_delegate_replacement():
    approve = parse_body(tx_result(
        [account_key(OWNER, signer=True, writable=True), account_key(ATA)],
        [parsed_ix(SPL_TOKEN_PROGRAM, "approve", {"amount": "1000001", "delegate": DELEGATE_NEW, "owner": OWNER, "source": ATA})],
        [1_000_000, 1],
        [995_000, 1],
    ))
    checked = parse_body(tx_result(
        [account_key(OWNER, signer=True, writable=True), account_key(ATA)],
        [parsed_ix(TOKEN_2022_PROGRAM, "approveChecked", {
            "delegate": DELEGATE_OLD,
            "mint": MINT,
            "owner": OWNER,
            "source": ATA,
            "tokenAmount": {"amount": "1000001", "decimals": 6, "uiAmount": 0.1, "uiAmountString": "0.1"},
        })],
        [1_000_000, 1],
        [995_000, 1],
    ))
    revoke = parse_body(tx_result(
        [account_key(OWNER, signer=True, writable=True), account_key(ATA)],
        [parsed_ix(SPL_TOKEN_PROGRAM, "revoke", {"owner": OWNER, "source": ATA})],
        [1_000_000, 1],
        [995_000, 1],
    ), commitment="finalized")
    assert approve.instructions[0].ix_type == "approve"
    assert approve.instructions[0].amount == 1000001
    assert checked.instructions[0].ix_type == "approveChecked"
    assert checked.instructions[0].amount == 1000001
    assert checked.instructions[0].program_id == TOKEN_2022_PROGRAM
    assert revoke.instructions[0].ix_type == "revoke"
    assert revoke.finality == "finalized"

    current = view_account(ATA, MINT, 80, delegate=DELEGATE_NEW, delegated=50)
    replaced = diff_authorities(
        [current],
        prior={ATA: PriorAuthority(True, delegate=DELEGATE_OLD, amount=50, owner=OWNER)},
        instructions=approve.instructions,
    )
    change = next(item for item in replaced.changes if item.field == "delegate")
    assert change.change == "replaced"
    assert change.previous == DELEGATE_OLD
    assert change.current == DELEGATE_NEW
    assert change.same_amount is True
    assert change.previous_amount == 50
    assert change.current_amount == 50
    assert "approve" in change.instruction_kinds

    increased = diff_authorities(
        [view_account(ATA, MINT, 80, delegate=DELEGATE_NEW, delegated=25)],
        prior={ATA: PriorAuthority(True, delegate=DELEGATE_NEW, amount=10, owner=OWNER)},
    )
    assert next(item.change for item in increased.changes if item.field == "delegate") == "increased"
    decreased = diff_authorities(
        [view_account(ATA, MINT, 80, delegate=DELEGATE_NEW, delegated=4)],
        prior={ATA: PriorAuthority(True, delegate=DELEGATE_NEW, amount=10, owner=OWNER)},
    )
    assert next(item.change for item in decreased.changes if item.field == "delegate") == "decreased"
    granted = diff_authorities(
        [view_account(ATA, MINT, 80, delegate=DELEGATE_NEW, delegated=4)],
        prior={ATA: PriorAuthority(True, delegate=None, amount=None, owner=OWNER)},
    )
    assert next(item.change for item in granted.changes if item.field == "delegate") == "new"
    revoked = diff_authorities(
        [view_account(ATA, MINT, 80)],
        prior={ATA: PriorAuthority(True, delegate=DELEGATE_OLD, amount=4, owner=OWNER)},
        instructions=revoke.instructions,
    )
    revoked_row = next(item for item in revoked.changes if item.field == "delegate")
    assert revoked_row.change == "revoked"
    assert revoked_row.current is None
    assert "revoke" in revoked_row.instruction_kinds


def test_mcm_w6_03_prior_unknown_is_first_observed_and_failed_read_is_not_revoked():
    current = view_account(ATA, MINT, 80, delegate=DELEGATE_NEW, delegated=50)
    first = diff_authorities([current], prior=None)
    row = next(item for item in first.changes if item.field == "delegate")
    assert row.change == "first_observed"
    assert row.change != "new"
    assert "grant" not in row.change
    assert row.previous is None
    assert row.label == "unlabeled"
    assert row.label != "safe"
    assert "unlabeled_delegate" in row.structural_flags
    assert "external_delegate" in row.structural_flags

    labeled = diff_authorities([current], prior=None, labels={DELEGATE_NEW: "phishing"})
    labeled_row = next(item for item in labeled.changes if item.field == "delegate")
    assert labeled_row.label == "phishing"
    assert "external_delegate" in labeled_row.structural_flags
    assert "unlabeled_delegate" not in labeled_row.structural_flags

    mismatch = diff_authorities(
        [current],
        prior={ATA: PriorAuthority(True, delegate=DELEGATE_NEW, amount=50, owner=OWNER)},
        instructions=parse_body(tx_result(
            [account_key(OWNER, signer=True, writable=True), account_key(ATA)],
            [parsed_ix(SPL_TOKEN_PROGRAM, "revoke", {"owner": OWNER, "source": ATA})],
            [1_000_000, 1],
            [995_000, 1],
        )).instructions,
    )
    mismatch_row = next(item for item in mismatch.changes if item.field == "delegate")
    assert "event_state_mismatch" in mismatch_row.reasons
    assert mismatch_row.change != "revoked"
    assert mismatch_row.status == "active"
    assert mismatch_row.current == DELEGATE_NEW

    failed = diff_authorities(
        [view_account(ATA, MINT, 80)],
        prior={ATA: PriorAuthority(True, delegate=DELEGATE_OLD, amount=9, owner=OWNER)},
        current_read_ok=False,
        previous_cursor=PREVIOUS,
        read_reason="read_error",
    )
    assert failed.status == "unknown"
    assert failed.cursor == PREVIOUS
    assert failed.changes == ()
    assert failed.complete is False
    assert failed.observation.category == "error"
    assert all(change.change != "revoked" for change in failed.changes)


def test_mcm_w6_03_set_authority_mint_freeze_permanent_delegate_and_unknown_extension():
    body = tx_result(
        [account_key(OWNER, signer=True, writable=True), account_key(ATA, writable=True)],
        [
            parsed_ix(SPL_TOKEN_PROGRAM, "setAuthority", {
                "account": ATA,
                "authority": OWNER,
                "authorityType": "closeAccount",
                "newAuthority": CLOSE_AUTH,
            }),
            parsed_ix(SPL_TOKEN_PROGRAM, "setAuthority", {
                "account": ATA,
                "authority": OWNER,
                "authorityType": "accountOwner",
                "newAuthority": DELEGATE_NEW,
            }),
        ],
        [1_000_000, RENT],
        [995_000, RENT],
    )
    parsed = parse_body(body)
    account = view_account(ATA, MINT, 12, owner=DELEGATE_NEW, close_authority=CLOSE_AUTH)
    report = diff_authorities(
        [account],
        prior={ATA: PriorAuthority(True, owner=OWNER, close_authority=OWNER)},
        instructions=parsed.instructions,
    )
    close = next(item for item in report.changes if item.field == "close_authority")
    owner = next(item for item in report.changes if item.field == "owner")
    assert close.change == "replaced"
    assert close.previous == OWNER
    assert close.current == CLOSE_AUTH
    assert "external_close_authority" in close.structural_flags
    assert owner.change == "replaced"
    assert owner.previous == OWNER
    assert owner.current == DELEGATE_NEW
    assert any(item.authority_type == "closeAccount" for item in parsed.instructions)
    assert any(item.authority_type == "accountOwner" for item in parsed.instructions)

    mint_value = {
        "data": {"parsed": {"info": {
            "decimals": 6,
            "extensions": [
                {"extension": "permanentDelegate", "state": {"delegate": PERMANENT}},
                {"extension": "transferFeeConfig", "state": {}},
            ],
            "freezeAuthority": FREEZE_AUTH,
            "isInitialized": True,
            "mintAuthority": MINT_AUTH,
            "supply": str(RAW_HUGE),
        }, "type": "mint"}},
        "lamports": RENT,
        "owner": TOKEN_2022_PROGRAM,
    }
    mint = parse_mint_account(MINT_2022, mint_value)
    assert mint is not None
    assert mint.supply == RAW_HUGE
    assert isinstance(mint.supply, int)
    minted = diff_authorities([], mints=(mint,), prior={})
    freeze = next(item for item in minted.changes if item.field == "freeze_authority")
    authority = next(item for item in minted.changes if item.field == "mint_authority")
    permanent = next(item for item in minted.changes if item.field == "permanent_delegate")
    assert freeze.current == FREEZE_AUTH
    assert "freeze_authority_present" in freeze.structural_flags
    assert authority.current == MINT_AUTH
    assert permanent.current == PERMANENT
    assert "permanent_delegate" in permanent.structural_flags
    assert permanent.label == "unlabeled"
    assert minted.complete is True

    unknown = parse_mint_account(MINT_2022, {
        "data": {"parsed": {"info": {
            "decimals": 9,
            "extensions": [
                {"extension": "permanentDelegate", "state": {"delegate": PERMANENT}},
                {"extension": "syntheticHook", "state": {"programId": DELEGATE_OLD}},
            ],
            "freezeAuthority": None,
            "mintAuthority": None,
            "supply": "1",
        }, "type": "mint"}},
    })
    assert unknown is not None
    assert "syntheticHook" in unknown.unknown_extensions
    unknown_report = diff_authorities([], mints=(unknown,), prior={})
    assert unknown_report.complete is False
    assert UNKNOWN_EXTENSION in unknown_report.reasons
    assert unknown_report.observation.category != "ok" or UNKNOWN_EXTENSION in unknown_report.observation.gaps or UNKNOWN_EXTENSION in unknown_report.observation.reason_codes
    assert not (
        unknown_report.observation.category in {"empty", "ok"}
        and unknown_report.observation.pagination_complete
        and unknown_report.observation.gap_detection_complete
        and not unknown_report.observation.gaps
    )

    closed = diff_authorities(
        [],
        prior={},
        instructions=parse_body(tx_result(
            [account_key(OWNER, signer=True, writable=True), account_key(ATA)],
            [parsed_ix(SPL_TOKEN_PROGRAM, "closeAccount", {"account": ATA, "destination": OWNER, "owner": OWNER})],
            [1_000_000, RENT],
            [1_000_000 - FEE + RENT, 0],
        )).instructions,
    )
    row = next(item for item in closed.changes if item.status == "closed")
    assert row.change == "closed"
    assert row.status == "closed"
    assert "rent_reclaim_possible" in row.reasons
    assert "stolen" not in row.reasons
    assert row.status != "stolen"


def test_mcm_w6_03_mint_read_uses_get_multiple_accounts():
    session = open_session()
    transport = session.adapter.transport
    from guardian.chains.solana.adapter import multiple_accounts_operation

    value = {
        "data": {"parsed": {"info": {
            "decimals": 6,
            "extensions": [{"extension": "permanentDelegate", "state": {"delegate": PERMANENT}}],
            "freezeAuthority": FREEZE_AUTH,
            "mintAuthority": MINT_AUTH,
            "supply": "4",
        }, "type": "mint"}},
        "lamports": RENT,
        "owner": TOKEN_2022_PROGRAM,
    }
    install(
        transport,
        "solana-rpc",
        multiple_accounts_operation([MINT_2022]),
        rpc_ok({"context": {"slot": 8}, "value": [value]}),
    )
    read = session.read_mints([MINT_2022])
    assert read.complete is True
    assert read.mints[0].permanent_delegate == PERMANENT
    assert read.mints[0].freeze_authority == FREEZE_AUTH
    assert read.mints[0].supply == 4
    called = json.loads(transport.backend.calls[-1].body.decode("utf-8"))
    assert called["method"] == "getMultipleAccounts"
    assert called["params"][1]["encoding"] == "jsonParsed"


def test_mcm_w6_04_fee_is_counted_once_and_rent_is_not_income():
    paid = parse_body(tx_result(
        [account_key(OWNER, signer=True, writable=True), account_key(COUNTERPARTY, writable=True), account_key(SYSTEM_PROGRAM)],
        [parsed_ix(SYSTEM_PROGRAM, "transfer", {"destination": COUNTERPARTY, "lamports": 100_000, "source": OWNER})],
        [1_000_000_000, 0, 1],
        [1_000_000_000 - FEE - 100_000, 100_000, 1],
    ), owned={OWNER})
    assert [effect.raw_amount for effect in paid.effects if effect.kind == "fee"] == [FEE]
    assert not any(effect.kind != "fee" and effect.raw_amount == FEE for effect in paid.effects)
    assert any(effect.kind == "native_transfer" and effect.raw_amount == 100_000 and effect.direction == "out" for effect in paid.effects)
    assert [event.kind for event in paid.events].count("fee") == 1

    created = parse_body(tx_result(
        [account_key(OWNER, signer=True, writable=True), account_key(ATA, writable=True), account_key(SYSTEM_PROGRAM), account_key(SPL_TOKEN_PROGRAM)],
        [parsed_ix(SYSTEM_PROGRAM, "createAccount", {
            "lamports": RENT,
            "newAccount": ATA,
            "owner": SPL_TOKEN_PROGRAM,
            "source": OWNER,
            "space": 165,
        })],
        [5_000_000_000, 0, 1, 1],
        [5_000_000_000 - FEE - RENT, RENT, 1, 1],
    ), owned={OWNER, ATA})
    assert any(effect.kind == "rent_deposit" and effect.raw_amount == RENT for effect in created.effects)
    assert not any(effect.kind == "native_transfer" and effect.raw_amount == RENT for effect in created.effects)

    closed = parse_body(tx_result(
        [account_key(OWNER, signer=True, writable=True), account_key(ATA, writable=True), account_key(SPL_TOKEN_PROGRAM)],
        [parsed_ix(SPL_TOKEN_PROGRAM, "closeAccount", {"account": ATA, "destination": OWNER, "owner": OWNER})],
        [10_000_000, RENT, 1],
        [10_000_000 + RENT - FEE, 0, 1],
    ), owned={OWNER, ATA})
    assert any(effect.kind == "rent_refund" and effect.raw_amount == RENT for effect in closed.effects)
    assert not any(effect.kind == "native_transfer" and effect.raw_amount == RENT for effect in closed.effects)
    assert not any(effect.direction == "in" and effect.raw_amount == RENT for effect in closed.effects)


def test_mcm_w6_04_wsol_wrap_and_unwrap_are_not_double_counted():
    wrap_amount = 1_000_000_000
    wrapped = parse_body(tx_result(
        [account_key(OWNER, signer=True, writable=True), account_key(ATA, writable=True), account_key(SYSTEM_PROGRAM), account_key(SPL_TOKEN_PROGRAM)],
        [
            parsed_ix(SYSTEM_PROGRAM, "transfer", {"destination": ATA, "lamports": wrap_amount, "source": OWNER}),
            parsed_ix(SPL_TOKEN_PROGRAM, "syncNative", {"account": ATA}),
        ],
        [5_000_000_000, RENT, 1, 1],
        [5_000_000_000 - FEE - wrap_amount, RENT + wrap_amount, 1, 1],
        pre_token=[token_balance(1, WRAPPED_SOL_MINT, OWNER, 0, 9, SPL_TOKEN_PROGRAM)],
        post_token=[token_balance(1, WRAPPED_SOL_MINT, OWNER, wrap_amount, 9, SPL_TOKEN_PROGRAM)],
    ), owned={OWNER, ATA})
    assert [effect.kind for effect in wrapped.effects if effect.raw_amount == wrap_amount] == ["wrap"]
    assert not any(effect.kind == "native_transfer" and effect.raw_amount == wrap_amount for effect in wrapped.effects)
    assert not any(effect.kind == "token_transfer" for effect in wrapped.effects)

    unwrapped = parse_body(tx_result(
        [account_key(OWNER, signer=True, writable=True), account_key(ATA, writable=True), account_key(SPL_TOKEN_PROGRAM)],
        [parsed_ix(SPL_TOKEN_PROGRAM, "closeAccount", {"account": ATA, "destination": OWNER, "owner": OWNER})],
        [10_000_000, RENT + wrap_amount, 1],
        [10_000_000 + RENT + wrap_amount - FEE, 0, 1],
        pre_token=[token_balance(1, WRAPPED_SOL_MINT, OWNER, wrap_amount, 9, SPL_TOKEN_PROGRAM)],
        post_token=[],
    ), owned={OWNER, ATA})
    assert [effect.kind for effect in unwrapped.effects if effect.raw_amount == wrap_amount] == ["unwrap"]
    assert any(effect.kind == "rent_refund" and effect.raw_amount == RENT for effect in unwrapped.effects)
    assert not any(effect.kind == "token_transfer" and effect.raw_amount == wrap_amount for effect in unwrapped.effects)
    assert not any(effect.kind == "native_transfer" and effect.raw_amount == wrap_amount for effect in unwrapped.effects)


def test_mcm_w6_04_raw_amounts_above_2_53_and_transfer_fee_stay_exact():
    moved = parse_body(tx_result(
        [account_key(OWNER, signer=True, writable=True), account_key(ATA, writable=True), account_key(COUNTERPARTY, writable=True), account_key(SPL_TOKEN_PROGRAM)],
        [parsed_ix(SPL_TOKEN_PROGRAM, "transferChecked", {
            "authority": OWNER,
            "destination": COUNTERPARTY,
            "mint": MINT,
            "source": ATA,
            "tokenAmount": {"amount": str(RAW_HUGE), "decimals": 9, "uiAmount": 1.0, "uiAmountString": "1"},
        })],
        [2_000_000, RENT, RENT, 1],
        [2_000_000 - FEE, RENT, RENT, 1],
        pre_token=[token_balance(1, MINT, OWNER, RAW_HUGE, 9, SPL_TOKEN_PROGRAM), token_balance(2, MINT, PAYER, 0, 9, SPL_TOKEN_PROGRAM)],
        post_token=[token_balance(1, MINT, OWNER, 0, 9, SPL_TOKEN_PROGRAM), token_balance(2, MINT, PAYER, RAW_HUGE, 9, SPL_TOKEN_PROGRAM)],
    ), owned={ATA})
    effect = next(item for item in moved.effects if item.kind == "token_transfer")
    assert effect.raw_amount == RAW_HUGE
    assert isinstance(effect.raw_amount, int)
    assert float(effect.raw_amount) != effect.raw_amount
    assert Decimal(effect.raw_amount) == Decimal(RAW_HUGE)
    assert effect.decimals == 9
    assert format_units(effect.raw_amount, 9) != format_units(int(float(effect.raw_amount)), 9)

    six = view_account(ATA, MINT, 1_500_000, decimals=6)
    zero = view_account(ATA_2022, MINT_2022, 4, program=TOKEN_2022_PROGRAM, decimals=0)
    assert six.decimals == 6 and six.amount == 1_500_000
    assert zero.decimals == 0 and zero.amount == 4
    assert format_units(six.amount, six.decimals) == "1.5"
    assert format_units(zero.amount, zero.decimals) == "4"
    assert isinstance(six, TokenAccountView)

    sent = 1_000_000
    received = 990_000
    token_fee = 10_000
    taxed = parse_body(tx_result(
        [
            account_key(OWNER, signer=True, writable=True),
            account_key(ATA, writable=True),
            account_key(COUNTERPARTY, writable=True),
            account_key(ALT_WRITABLE, writable=True),
            account_key(TOKEN_2022_PROGRAM),
        ],
        [parsed_ix(TOKEN_2022_PROGRAM, "transferChecked", {
            "authority": OWNER,
            "destination": COUNTERPARTY,
            "mint": MINT_2022,
            "source": ATA,
            "tokenAmount": {"amount": str(sent), "decimals": 6, "uiAmount": 1.0, "uiAmountString": "1"},
        })],
        [2_000_000, RENT, RENT, RENT, 1],
        [2_000_000 - FEE, RENT, RENT, RENT, 1],
        pre_token=[
            token_balance(1, MINT_2022, OWNER, 5_000_000, 6, TOKEN_2022_PROGRAM),
            token_balance(2, MINT_2022, PAYER, 0, 6, TOKEN_2022_PROGRAM),
            token_balance(3, MINT_2022, PAYER, 0, 6, TOKEN_2022_PROGRAM),
        ],
        post_token=[
            token_balance(1, MINT_2022, OWNER, 5_000_000 - sent, 6, TOKEN_2022_PROGRAM),
            token_balance(2, MINT_2022, PAYER, received, 6, TOKEN_2022_PROGRAM),
            token_balance(3, MINT_2022, PAYER, token_fee, 6, TOKEN_2022_PROGRAM),
        ],
    ), owned={ATA}, unknown=("syntheticHook",))
    transfer = next(item for item in taxed.effects if item.kind == "token_transfer")
    withheld = next(item for item in taxed.effects if item.kind == "token_transfer_fee")
    assert transfer.sent == sent
    assert transfer.received == received
    assert transfer.transfer_fee == token_fee
    assert withheld.raw_amount == token_fee
    assert transfer.received + withheld.raw_amount == transfer.sent
    assert taxed.complete is False
    assert UNKNOWN_EXTENSION in taxed.reasons
    assert transfer.raw_amount == sent

    internal = parse_body(tx_result(
        [account_key(OWNER, signer=True, writable=True), account_key(ATA, writable=True), account_key(ATA_2022, writable=True), account_key(SPL_TOKEN_PROGRAM)],
        [parsed_ix(SPL_TOKEN_PROGRAM, "transfer", {"amount": "100", "authority": OWNER, "destination": ATA_2022, "source": ATA})],
        [2_000_000, RENT, RENT, 1],
        [2_000_000 - FEE, RENT, RENT, 1],
        pre_token=[token_balance(1, MINT, OWNER, 100, 6, SPL_TOKEN_PROGRAM), token_balance(2, MINT, OWNER, 0, 6, SPL_TOKEN_PROGRAM)],
        post_token=[token_balance(1, MINT, OWNER, 0, 6, SPL_TOKEN_PROGRAM), token_balance(2, MINT, OWNER, 100, 6, SPL_TOKEN_PROGRAM)],
    ), owned={ATA, ATA_2022})
    assert internal.net == 0
    assert internal.gross == 100
    assert any(effect.direction == "internal" and effect.raw_amount == 100 for effect in internal.effects)
    assert not any(effect.direction == "out" for effect in internal.effects)


def test_mcm_w6_04_helius_missing_key_is_blocked_and_does_not_override_rpc():
    transport = world()
    client, blocked = open_helius(transport, "helius", {}, previous_cursor=PREVIOUS)
    assert client is None
    assert blocked is not None
    assert blocked.probe == "blocked"
    assert blocked.completes_rpc_coverage is False
    assert blocked.history.blocked is True
    assert blocked.history.reason == "credential_missing"
    assert blocked.history.category == "error"
    assert blocked.history.category != "empty"
    assert blocked.history.cursor == PREVIOUS
    assert blocked.history.items == ()
    assert "safe" not in blocked.history.reason
    assert "no assets" not in blocked.history.reason
    assert transport.backend.calls == []
    blank, blank_read = open_helius(transport, "helius", {"HELIUS_API_KEY": ""}, previous_cursor=PREVIOUS)
    assert blank is None
    assert blank_read is not None and blank_read.history.reason == "credential_missing"

    kept, reasons = authoritative_amount(1000001, 1)
    assert kept == 1000001
    assert reasons == ("helius_amount_not_authoritative",)
    missing, missing_reasons = authoritative_amount(None, 1)
    assert missing is None
    assert missing != 1
    assert "rpc_amount_missing" in missing_reasons

    ready, absent = open_helius(
        transport, "helius", {"HELIUS_API_KEY": KEY}, clock=lambda: 0.0, sleep=lambda _seconds: None, random=lambda: 0.0,
    )
    assert absent is None and ready is not None
    assert KEY not in repr(ready)
    operation = ready.history_operation(address=OWNER, limit=2, sort_order="desc", commitment="confirmed", pagination_token=None)
    prepared = transport.encode("helius", operation)
    assert "api-key=" in prepared.url
    assert KEY in prepared.url
    assert KEY not in repr(prepared)
    assert KEY not in ready.redact_error(f"failed {prepared.url} {KEY}")
    rejected = ready.transaction_history(address=OWNER, commitment="processed", previous_cursor=PREVIOUS)
    assert rejected.history.reason == "commitment_rejected"
    assert transport.backend.calls == []

    install(transport, "helius", ready.history_operation(address=OWNER, limit=2, sort_order="desc", commitment="confirmed", pagination_token=None), RawResponse(401, {}, b"{}"))
    denied = ready.transaction_history(address=OWNER, limit=2, previous_cursor=PREVIOUS)
    assert denied.probe == "blocked"
    assert denied.history.blocked is True
    assert denied.history.reason == "credential_rejected"
    assert denied.history.category == "error"
    assert denied.history.cursor == PREVIOUS
    assert KEY not in denied.history.detail
    coverage = coverage_from_enrichment(denied.history)
    assert coverage.pagination_complete is False
    assert coverage.gap_detection_complete is False
    assert ENRICHMENT_REASON in coverage.reason_codes
    assert coverage.category != "ok"


def test_mcm_w6_04_helius_retries_429_then_stays_enrichment():
    transport = world(retry_attempts=1)
    client, _blocked = open_helius(
        transport, "helius", {"HELIUS_API_KEY": KEY}, clock=lambda: 1_700_000_000.0, sleep=lambda _seconds: None, random=lambda: 0.0,
    )
    assert client is not None
    operation = client.history_operation(address=OWNER, limit=2, sort_order="desc", commitment="finalized", pagination_token=None)
    install_many(transport, "helius", operation, [
        RawResponse(429, {"retry-after": "0"}, b"{}"),
        RawResponse(200, {}, json.dumps({"transactions": [{"amount": 1, "signature": SIG}]}).encode("utf-8")),
    ])
    read = client.transaction_history(address=OWNER, limit=2, commitment="finalized", previous_cursor=PREVIOUS)
    assert read.history.category == "ok"
    assert read.history.pagination_complete is True
    assert read.history.items[0]["signature"] == SIG
    assert read.completes_rpc_coverage is False
    assert len(transport.backend.calls) == 2
    observed = coverage_from_enrichment(read.history)
    assert observed.category == "partial"
    assert observed.pagination_complete is False
    assert ENRICHMENT_REASON in observed.gaps
    amount, notes = authoritative_amount(RAW_HUGE, read.history.items[0]["amount"])
    assert amount == RAW_HUGE
    assert notes == ("helius_amount_not_authoritative",)

    transport.backend.mapping.clear()
    transport.backend.calls.clear()
    install(transport, "helius", operation, RawResponse(200, {}, b'{"unexpected": true}'))
    malformed = client.transaction_history(address=OWNER, limit=2, commitment="finalized", previous_cursor=PREVIOUS)
    assert malformed.history.category == "error"
    assert malformed.history.reason == "malformed"
    assert malformed.history.cursor == PREVIOUS
    assert KEY not in malformed.history.detail


def test_mcm_w6_04_send_and_simulate_are_denied_before_the_backend():
    transport = world()
    for method in ("sendTransaction", "simulateTransaction"):
        operation = Operation(protocol="jsonrpc", adapter="solana_rpc", method=method, params=["synthetic"])
        with pytest.raises(SecurityError) as caught:
            transport.request("solana-rpc", operation)
        assert caught.value.reason == "method_denied"
    assert transport.backend.calls == []
    allowed = transaction_operation(SIG, "confirmed")
    assert_operation_allowed(allowed)
    assert allowed.method == "getTransaction"
