"""MCM-W8 protocol positions, claim evidence, and exposure news. Offline fixtures only."""

from __future__ import annotations

import json
import socket
import sys
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from types import MappingProxyType

import pytest
from eth_abi import encode

from guardian.chains.evm.adapter import chain_id_operation
from guardian.chains.solana.adapter import account_info_operation, genesis_hash_operation
from guardian.coverage import build_coverage
from guardian.events import ChainEvent, FlowStatus
from guardian.identities import parse_address
from guardian.news import (
    IncidentBook,
    WalletExposure,
    canonicalize_url,
    link_exposure,
    load_news_sources,
    parse_feed,
    parse_structured,
)
from guardian.pricing.quote import PriceProvenance, PriceQuote, value_usd
from guardian.protocols import (
    PLUGIN_REGISTRY,
    FungibleBalance,
    ManifestError,
    PositionLeg,
    PositionObservation,
    aggregate_portfolio,
    assess_bridge,
    build_protocol_coverage,
    load_manifest,
    load_protocol_dir,
    open_plugin_context,
    run_plugin,
)
from guardian.protocols.aave import AaveV3
from guardian.protocols.erc4626 import Erc4626
from guardian.protocols.lista import ListaSlisBnb
from guardian.protocols.network import expected_genesis, native_identity
from guardian.protocols.pins import AAVE_POOLS, LISTA_PROXY, SLISBNB, STAKE_PROGRAM, STAKE_WITHDRAWER_OFFSET
from guardian.protocols.stake import program_accounts_operation
from guardian.sources.budget import BudgetBook
from guardian.sources.fixtures import FixtureBackend, RawResponse, request_key
from guardian.sources.transport import ReadOnlyTransport, build_endpoint_registry

ROOT = Path(__file__).resolve().parents[2]
NEWS_FIXTURES = ROOT / "tests" / "fixtures" / "news"
PUBLIC_IP = "1.1.1.1"
OWNER = "0x0000000000000000000000000000000000000001"
UNDERLYING = "0x0000000000000000000000000000000000000011"
A_TOKEN = "0x0000000000000000000000000000000000000012"
DEBT_TOKEN = "0x0000000000000000000000000000000000000013"
VAULT = "0x0000000000000000000000000000000000000014"
ASSET = "0x0000000000000000000000000000000000000015"
TENANT = "tenant-synthetic"
WALLET_ID = "11111111-1111-4111-8111-111111111111"
WALLET_B = "22222222-2222-4222-8222-222222222222"
WALLET_C = "33333333-3333-4333-8333-333333333333"
NOW = 1_700_000_000
COLLATERAL_BASE = 2**53 + 8
DEBT_BASE = 2**53 + 4
HEALTH = 10**18
ATOKEN_RAW = 2**53 + 17
DEBT_RAW = 2**53 + 3
SHARES = 10**18 + 2**53
ASSETS = 2**53 + 8
SLIS_RAW = 10**18
SLIS_QUOTE = 10**18 + 12345
WALLET_BNB = 10**16
LAMPORTS = 2**53 + 9
DELEGATED = 2**53
WITHDRAW_BNB = 2**53 + 11
SOURCES = {
    "bsc-mainnet": "evm-bsc",
    "ethereum-mainnet": "evm-eth",
    "solana-mainnet": "solana-rpc",
}
_B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


@pytest.fixture(autouse=True)
def _offline(monkeypatch):
    def explode(*_args, **_kwargs):
        raise AssertionError("network is forbidden in MCM-W8 tests")

    monkeypatch.setattr(socket, "socket", explode)
    monkeypatch.setattr(socket, "create_connection", explode)
    monkeypatch.setattr(socket, "getaddrinfo", explode)
    import httpx

    monkeypatch.setattr(httpx, "Client", explode)


def _resolve(_host: str) -> list[str]:
    return [PUBLIC_IP]


def _b58encode(raw: bytes) -> str:
    value = int.from_bytes(raw, "big")
    chars: list[str] = []
    while value:
        value, rem = divmod(value, 58)
        chars.append(_B58[rem])
    zeros = 0
    for byte in raw:
        if byte != 0:
            break
        zeros += 1
    return ("1" * zeros) + "".join(reversed(chars))


def solana_address(marker: int) -> str:
    return parse_address("solana", _b58encode(bytes([marker]) * 32)).canonical


def world():
    document = {
        "budgets": {
            "evm": {"max_concurrency": 4, "max_requests_per_day": 1000, "max_rps": 100, "retry_attempts": 0},
            "sol": {"max_concurrency": 4, "max_requests_per_day": 1000, "max_rps": 100, "retry_attempts": 0},
        },
        "sources": {
            "evm-bsc": {"adapter": "evm_rpc", "budget_group": "evm", "endpoint_env": "BSC_RPC_ENDPOINT"},
            "evm-eth": {"adapter": "evm_rpc", "budget_group": "evm", "endpoint_env": "ETH_RPC_ENDPOINT"},
            "solana-rpc": {"adapter": "solana_rpc", "budget_group": "sol", "endpoint_env": "SOLANA_RPC_ENDPOINT"},
        },
    }
    secrets = {
        "BSC_RPC_ENDPOINT": "https://bsc.example.invalid",
        "ETH_RPC_ENDPOINT": "https://eth.example.invalid",
        "SOLANA_RPC_ENDPOINT": "https://solana.example.invalid",
    }
    endpoints = build_endpoint_registry(document, secrets=secrets)
    book = BudgetBook.from_document(document["budgets"], clock=lambda: 1_700_000_000.0, sleep=lambda _seconds: None)
    return ReadOnlyTransport(endpoints, book, FixtureBackend({}), resolve=_resolve)


def install(transport, source_id: str, operation, payload) -> None:
    call = transport.encode(source_id, operation)
    body = payload if isinstance(payload, RawResponse) else RawResponse(200, {}, json.dumps(payload).encode("utf-8"))
    transport.backend.mapping[request_key(call.method, call.url, call.body)] = body


def rpc_ok(result):
    return {"id": 1, "jsonrpc": "2.0", "result": result}


def abi_hex(types: list[str], values: list) -> str:
    return "0x" + encode(types, values).hex()


def install_chain_ids(transport) -> None:
    install(transport, "evm-eth", chain_id_operation(), rpc_ok(hex(1)))
    install(transport, "evm-bsc", chain_id_operation(), rpc_ok(hex(56)))
    install(transport, "solana-rpc", genesis_hash_operation(), rpc_ok(expected_genesis("solana-mainnet")))


def quote_for(network: str, identity: str, raw: int, decimals: int, price: Decimal) -> PriceQuote:
    amount = value_usd(raw, decimals, price)
    return PriceQuote(
        network_id=network,
        identity=identity,
        symbol="FIXTURE",
        price_usd=price,
        price_quality="available",
        valuation="valued",
        reason="",
        value_usd=amount,
        threshold_decision="below" if amount < Decimal("1000") else "above",
        age_seconds=0,
        display_only=False,
        provenance=PriceProvenance(source="fixture", fetched_at=NOW, last_updated_at=NOW, path="/fixture"),
    )


def chain_event(network: str, tx_id: str, *, kind: str, tx_status: str, finality: str) -> ChainEvent:
    return ChainEvent(
        tenant_id=TENANT,
        wallet_id=WALLET_ID,
        network=network,
        chain_uid="eip155:1",
        tx_id=tx_id,
        sub_index="0",
        kind=kind,
        asset="evm-native:1",
        raw_amount="1",
        block_number="1",
        block_hash="0x" + "ab" * 32,
        tx_status=tx_status,
        finality=finality,
        source_ids=("evm-eth",),
        observed_at="2026-10-09T00:00:00Z",
    )


def stake_account_value(pubkey_owner: str, lamports, delegated) -> dict:
    return {
        "data": {
            "parsed": {
                "info": {
                    "meta": {"authorized": {"staker": pubkey_owner, "withdrawer": pubkey_owner}},
                    "stake": {
                        "delegation": {
                            "activationEpoch": "1",
                            "deactivationEpoch": "18446744073709551615",
                            "stake": delegated,
                            "voter": pubkey_owner,
                            "warmupCooldownRate": 0.25,
                        }
                    },
                },
                "type": "delegated",
            },
            "program": "stake",
            "space": 200,
        },
        "executable": False,
        "lamports": lamports,
        "owner": STAKE_PROGRAM,
    }


def run(plugin_id: str, transport, network: str, wallet: str = OWNER, **kwargs):
    return run_plugin(
        plugin_id,
        transport=transport,
        sources=SOURCES,
        wallet_id=WALLET_ID,
        wallet=wallet,
        network=network,
        clock=kwargs.pop("clock", NOW),
        **kwargs,
    )


def test_mcm_w8_01_plugin_readonly_rejects_code_loading_and_reads_views():
    assert isinstance(PLUGIN_REGISTRY, MappingProxyType)
    assert set(PLUGIN_REGISTRY) == {
        "aave_v3",
        "erc4626",
        "kinza",
        "lista_slisbnb",
        "solana_stake",
        "uxlink_holdings",
    }

    with pytest.raises(ManifestError) as unknown:
        load_manifest({"id": "guardian.protocols.not_a_plugin"})
    assert unknown.value.reason == "unknown_plugin"
    for key in ("module", "class", "entrypoint", "import", "exec"):
        with pytest.raises(ManifestError) as forbidden:
            load_manifest({"id": "lista_slisbnb", key: "guardian.protocols.not_a_plugin"})
        assert forbidden.value.reason == f"forbidden_key:{key}"

    document = json.loads((ROOT / "config" / "protocols" / "lista-slisbnb.json").read_text(encoding="utf-8"))
    widened = dict(document)
    widened["view_methods"] = [*document["view_methods"], "claimWithdraw(uint256)"]
    with pytest.raises(ManifestError) as view:
        load_manifest(widened)
    assert view.value.reason == "view_not_allowlisted"
    assert "mcm_w8_no_such_module" not in sys.modules
    noted = dict(document)
    noted["notes"] = "import mcm_w8_no_such_module"
    loaded_note = load_manifest(noted)
    assert loaded_note.plugin_id == "lista_slisbnb"
    parsed = parse_structured(
        [{
            "title": "import mcm_w8_no_such_module",
            "url": "https://news.example.invalid/meta",
            "body": "__import__('mcm_w8_no_such_module')",
            "published_at": "2026-10-01T00:00:00Z",
        }],
        source_id="synthetic",
    )
    assert parsed.ok is True
    assert parsed.items[0].title == "import mcm_w8_no_such_module"
    assert "mcm_w8_no_such_module" not in sys.modules

    manifests = load_protocol_dir()
    assert set(manifests) == {
        "aave_v3",
        "erc4626",
        "kinza",
        "lista_slisbnb",
        "solana_stake",
        "uxlink_holdings",
    }
    legacy = json.loads((ROOT / "config" / "protocols" / "lista-bsc.json").read_text(encoding="utf-8"))
    assert "id" not in legacy
    assert legacy["protocol_id"] == "lista-slisbnb"
    for network, address in AAVE_POOLS.items():
        declared = manifests["aave_v3"].contracts[network]["pool"]["address"]
        parsed_address = parse_address("evm", declared)
        assert parsed_address.canonical == address
        assert parsed_address.display == address
        assert declared == address
    assert manifests["lista_slisbnb"].contracts["bsc-mainnet"]["proxy"]["address"] == LISTA_PROXY
    assert manifests["solana_stake"].contracts["solana-mainnet"]["stake_program"]["address"] == STAKE_PROGRAM
    for manifest in manifests.values():
        assert "enabled" not in json.dumps(manifest.contracts)
    for path in (ROOT / "config" / "protocols").glob("*.json"):
        text = path.read_text(encoding="utf-8").lower()
        if path.name == "lista-bsc.json":
            continue
        assert "live_pass" not in text

    transport = world()
    install_chain_ids(transport)
    install(
        transport,
        "evm-bsc",
        ListaSlisBnb.operation(LISTA_PROXY, "paused()", []),
        rpc_ok(abi_hex(["bool"], [False])),
    )
    before = len(transport.backend.calls)
    with open_plugin_context(
        "lista_slisbnb",
        ListaSlisBnb.specs,
        transport=transport,
        sources=SOURCES,
        clock=NOW,
    ) as ctx:
        public = [name for name in dir(ctx) if not name.startswith("_")]
        assert "transport" not in public
        assert "sign" not in public
        assert "broadcast" not in public
        assert "send" not in public
        assert "request" not in public
        assert "write" not in public
        with pytest.raises(ValueError, match="not_a_view_call"):
            ctx.eth_call("bsc-mainnet", LISTA_PROXY, "claimWithdraw(uint256)", [1])
        assert len(transport.backend.calls) == before
        paused = ctx.eth_call("bsc-mainnet", LISTA_PROXY, "paused()", [])
    assert paused.ok is True
    assert paused.decoded == (False,)
    with pytest.raises(ValueError, match="not_a_view_call"):
        ListaSlisBnb.operation(LISTA_PROXY, "decimals()", [])

    unknown_obs = run("not_a_plugin", transport, "bsc-mainnet")
    assert unknown_obs.status == "unknown"
    assert unknown_obs.legs == ()
    assert unknown_obs.reason_codes == ("unknown_plugin",)
    quiet = PositionObservation(
        plugin_id="lista_slisbnb",
        wallet_id=WALLET_ID,
        network="bsc-mainnet",
        status="none_observed",
        cursor="clock:1",
    )
    coverage = build_protocol_coverage(
        wallet_id=WALLET_ID,
        network="bsc-mainnet",
        positions=(quiet,),
        requested_plugins=("lista_slisbnb", "not_a_plugin"),
        prior_success_at="2026-10-08T00:00:00Z",
    )
    assert coverage["positions"].category == "error"
    assert "unknown_plugin:not_a_plugin" in coverage["positions"].reason_codes
    assert "unknown_plugin:lista_slisbnb" not in coverage["positions"].reason_codes
    assert "plugin_without_data" in coverage["bridges"].reason_codes
    assert "news_not_parsed" in coverage["exposure_news"].reason_codes

    class EmptyCatalog:
        adapters: dict = {}

    from guardian.protocols.network import chain_registry

    rows = build_coverage(
        [(WALLET_ID, "bsc-mainnet")],
        {(WALLET_ID, "bsc-mainnet", "positions"): coverage["positions"]},
        EmptyCatalog(),
        chain_registry(),
        "2026-10-09T00:00:00Z",
    )
    position_row = next(row for row in rows if row["capability"] == "positions")
    assert position_row["quality"] == "error"
    assert "unknown_plugin:not_a_plugin" in position_row["reason_codes"]
    assert "read_error" in position_row["reason_codes"]
    assert position_row["last_success_at"] == "2026-10-08T00:00:00Z"
    assert all(row["quality"] != "complete_in_declared_scope" for row in rows)


def test_mcm_w8_02_positions_not_double_counted():
    stake_account = solana_address(9)
    withdrawer = solana_address(8)
    transport = world()
    install_chain_ids(transport)
    pool = AAVE_POOLS["ethereum-mainnet"]
    install(
        transport,
        "evm-eth",
        AaveV3.operation(pool, "getUserAccountData(address)", [OWNER]),
        rpc_ok(abi_hex(["uint256"] * 6, [COLLATERAL_BASE, DEBT_BASE, 0, 8000, 7000, HEALTH])),
    )
    install(
        transport,
        "evm-eth",
        AaveV3.operation(A_TOKEN, "balanceOf(address)", [OWNER]),
        rpc_ok(abi_hex(["uint256"], [ATOKEN_RAW])),
    )
    install(
        transport,
        "evm-eth",
        AaveV3.operation(DEBT_TOKEN, "balanceOf(address)", [OWNER]),
        rpc_ok(abi_hex(["uint256"], [DEBT_RAW])),
    )
    install(
        transport,
        "evm-bsc",
        ListaSlisBnb.operation(LISTA_PROXY, "paused()", []),
        rpc_ok(abi_hex(["bool"], [False])),
    )
    install(
        transport,
        "evm-bsc",
        ListaSlisBnb.operation(LISTA_PROXY, "getUserWithdrawalRequests(address)", [OWNER]),
        rpc_ok(abi_hex(["(uint256,uint256,uint256)[]"], [[]])),
    )
    install(
        transport,
        "evm-bsc",
        ListaSlisBnb.operation(SLISBNB, "balanceOf(address)", [OWNER]),
        rpc_ok(abi_hex(["uint256"], [SLIS_RAW])),
    )
    install(
        transport,
        "evm-bsc",
        ListaSlisBnb.operation(LISTA_PROXY, "convertSnBnbToBnb(uint256)", [SLIS_RAW]),
        rpc_ok(abi_hex(["uint256"], [SLIS_QUOTE])),
    )
    install(
        transport,
        "evm-eth",
        Erc4626.operation(VAULT, "asset()", []),
        rpc_ok(abi_hex(["address"], [ASSET])),
    )
    install(
        transport,
        "evm-eth",
        Erc4626.operation(VAULT, "balanceOf(address)", [OWNER]),
        rpc_ok(abi_hex(["uint256"], [SHARES])),
    )
    install(
        transport,
        "evm-eth",
        Erc4626.operation(VAULT, "convertToAssets(uint256)", [SHARES]),
        rpc_ok(abi_hex(["uint256"], [ASSETS])),
    )
    account = stake_account_value(withdrawer, LAMPORTS, str(DELEGATED))
    install(
        transport,
        "solana-rpc",
        account_info_operation(stake_account),
        rpc_ok({"context": {"slot": 9}, "value": account}),
    )
    install(
        transport,
        "solana-rpc",
        program_accounts_operation(withdrawer, STAKE_WITHDRAWER_OFFSET),
        rpc_ok([{"account": account, "pubkey": stake_account}]),
    )

    aave = run(
        "aave_v3",
        transport,
        "ethereum-mainnet",
        private={"reserves": [{
            "a_token": A_TOKEN,
            "debt_decimals": 6,
            "decimals": 18,
            "network": "ethereum-mainnet",
            "underlying": UNDERLYING,
            "variable_debt_token": DEBT_TOKEN,
        }]},
    )
    base_only = run("aave_v3", transport, "ethereum-mainnet")
    lista = run("lista_slisbnb", transport, "bsc-mainnet")
    vault = run(
        "erc4626",
        transport,
        "ethereum-mainnet",
        private={"vaults": [{
            "asset_decimals": 8,
            "network": "ethereum-mainnet",
            "share_decimals": 18,
            "vault": VAULT,
        }]},
    )
    stake = run("solana_stake", transport, "solana-mainnet", private={"stake_accounts": [stake_account]})
    discovered = run(
        "solana_stake",
        transport,
        "solana-mainnet",
        private={"stake_authority": {"address": withdrawer, "role": "withdrawer"}},
    )

    assert aave.status == "observed"
    assert type(aave.legs[0].raw_amount) is int
    assert {leg.identity for leg in aave.legs} == {A_TOKEN, DEBT_TOKEN}
    assert "aave-v3:collateral-base" not in {leg.identity for leg in aave.legs}
    assert "aave-v3:debt-base" not in {leg.identity for leg in aave.legs}
    metrics = dict(aave.metrics)
    assert metrics["health_factor"] == str(HEALTH)
    assert metrics["collateral_base"] == str(COLLATERAL_BASE)
    assert "." not in metrics["health_factor"]
    collateral = next(leg for leg in aave.legs if leg.kind == "receipt")
    debt = next(leg for leg in aave.legs if leg.kind == "debt")
    assert collateral.raw_amount == ATOKEN_RAW
    assert collateral.decimals == 18
    assert debt.raw_amount == DEBT_RAW
    assert debt.decimals == 6
    assert base_only.status == "observed"
    base_collateral = next(leg for leg in base_only.legs if leg.identity == "aave-v3:collateral-base")
    base_debt = next(leg for leg in base_only.legs if leg.identity == "aave-v3:debt-base")
    assert base_collateral.raw_amount == COLLATERAL_BASE
    assert base_collateral.decimals == 8
    assert base_debt.raw_amount == DEBT_BASE
    assert base_debt.decimals == 8
    assert base_only.reason_codes == ("reserves_not_supplied",)

    assert lista.status == "observed"
    receipt = next(leg for leg in lista.legs if leg.kind == "receipt")
    quote = next(leg for leg in lista.legs if leg.kind == "underlying")
    assert receipt.identity == SLISBNB.lower()
    assert receipt.raw_amount == SLIS_RAW
    assert type(receipt.raw_amount) is int
    assert quote.identity == native_identity("bsc-mainnet")
    assert quote.raw_amount == SLIS_QUOTE
    assert quote.duplicates == SLISBNB.lower()

    assert vault.status == "observed"
    shares = next(leg for leg in vault.legs if leg.kind == "receipt")
    assets = next(leg for leg in vault.legs if leg.kind == "underlying")
    assert shares.identity == VAULT
    assert shares.raw_amount == SHARES
    assert shares.decimals == 18
    assert type(shares.raw_amount) is int
    assert assets.identity == ASSET
    assert assets.raw_amount == ASSETS
    assert assets.decimals == 8
    assert assets.duplicates == VAULT

    assert stake.status == "observed"
    assert stake.cursor == "slot:9"
    assert dict(stake.metrics)["commitment"] == "confirmed"
    stake_receipt = next(leg for leg in stake.legs if leg.kind == "receipt")
    assert stake_receipt.identity == stake_account
    assert stake_receipt.raw_amount == LAMPORTS
    assert type(stake_receipt.raw_amount) is int
    assert stake_receipt.decimals == 9
    assert dict(stake_receipt.detail)["delegated_stake"] == str(DELEGATED)
    assert discovered.status == "observed"
    assert next(leg.raw_amount for leg in discovered.legs if leg.kind == "receipt") == LAMPORTS
    program_calls = [
        json.loads(call.body.decode("utf-8"))
        for call in transport.backend.calls
        if json.loads(call.body.decode("utf-8"))["method"] == "getProgramAccounts"
    ]
    assert program_calls
    filters = program_calls[-1]["params"][1]["filters"]
    assert program_calls[-1]["params"][0] == STAKE_PROGRAM
    assert program_calls[-1]["params"][1]["commitment"] == "confirmed"
    assert filters[0]["memcmp"]["offset"] == 44
    assert filters[0]["memcmp"]["bytes"] == withdrawer
    methods = {json.loads(call.body.decode("utf-8"))["method"] for call in transport.backend.calls}
    assert methods <= {"eth_call", "eth_chainId", "getAccountInfo", "getGenesisHash", "getProgramAccounts"}
    assert "getAccountInfo" in methods

    bnb = native_identity("bsc-mainnet")
    assert bnb == "evm-native:56"
    portfolio = aggregate_portfolio(
        [aave, lista, vault, stake],
        fungibles=(
            FungibleBalance("ethereum-mainnet", A_TOKEN, ATOKEN_RAW, 18),
            FungibleBalance("ethereum-mainnet", DEBT_TOKEN, DEBT_RAW, 6),
            FungibleBalance("ethereum-mainnet", VAULT, SHARES, 18),
            FungibleBalance("bsc-mainnet", SLISBNB.lower(), SLIS_RAW, 18),
            FungibleBalance("bsc-mainnet", bnb, WALLET_BNB, 18),
        ),
        quotes={("ethereum-mainnet", VAULT): quote_for("ethereum-mainnet", VAULT, SHARES, 18, Decimal("2"))},
    )
    assert portfolio.amount("ethereum-mainnet", A_TOKEN) == ATOKEN_RAW
    assert type(portfolio.amount("ethereum-mainnet", A_TOKEN)) is int
    assert portfolio.amount("ethereum-mainnet", DEBT_TOKEN) == -DEBT_RAW
    assert portfolio.amount("ethereum-mainnet", A_TOKEN) + portfolio.amount("ethereum-mainnet", DEBT_TOKEN) == ATOKEN_RAW - DEBT_RAW
    debt_lines = [line for line in portfolio.counted if line.identity == DEBT_TOKEN]
    assert len(debt_lines) == 1
    assert debt_lines[0].raw_amount == -DEBT_RAW
    assert portfolio.amount("ethereum-mainnet", VAULT) == SHARES
    assert portfolio.amount("ethereum-mainnet", ASSET) == 0
    assert portfolio.amount("bsc-mainnet", SLISBNB.lower()) == SLIS_RAW
    assert portfolio.amount("bsc-mainnet", bnb) == WALLET_BNB
    assert portfolio.amount("solana-mainnet", stake_account) == LAMPORTS
    assert all(line.raw_amount != DELEGATED for line in portfolio.counted)
    quote_lines = [line for line in portfolio.lines if line.reason == "underlying_of_counted_receipt"]
    assert quote_lines
    assert all(line.counted is False for line in quote_lines)
    assert any(line.identity == bnb and line.raw_amount == SLIS_QUOTE for line in quote_lines)
    suppressed = [line for line in portfolio.lines if line.reason == "w4_same_as_receipt"]
    assert {line.identity for line in suppressed} >= {A_TOKEN, VAULT, SLISBNB.lower()}
    assert any(line.reason == "w4_same_as_debt" and line.identity == DEBT_TOKEN for line in portfolio.lines)
    expected = value_usd(SHARES, 18, Decimal("2"))
    priced = [line for line in portfolio.counted if line.priced is not None]
    assert len(priced) == 1
    assert priced[0].identity == VAULT
    assert priced[0].priced == expected
    assert type(priced[0].priced) is Decimal
    assert portfolio.value_usd is None
    assert portfolio.value_usd != Decimal(0)
    assert portfolio.valuation == "valuation_unknown"
    assert portfolio.unknown_plugins == ()

    broken = world()
    install_chain_ids(broken)
    install(
        broken,
        "solana-rpc",
        account_info_operation(stake_account),
        rpc_ok({"context": {"slot": 1}, "value": stake_account_value(withdrawer, 1.5, "1")}),
    )
    failed = run(
        "solana_stake",
        broken,
        "solana-mainnet",
        prior={"cursor": "slot:1"},
        private={"stake_accounts": [stake_account]},
    )
    assert failed.status == "unknown"
    assert failed.status != "none_observed"
    assert failed.legs == ()
    assert failed.prior_kept is True
    assert failed.cursor == "slot:1"
    failed_book = aggregate_portfolio([failed])
    assert failed_book.unknown_plugins == ("solana_stake",)
    assert failed_book.lines == ()
    assert failed_book.value_usd is None


def test_mcm_w8_03_claim_and_bridge_evidence():
    empty = world()
    install_chain_ids(empty)
    _install_lista(empty, rows=[], balance=0, quoted=0)
    none = run("lista_slisbnb", empty, "bsc-mainnet")
    assert none.status == "none_observed"
    assert none.legs == ()
    assert none.withdrawals == ()
    assert none.prior_kept is False

    clock = NOW
    start = clock - 8 * 86400
    opened = world()
    install_chain_ids(opened)
    _install_lista(
        opened,
        rows=[(7, 1, start)],
        statuses={0: (False, WITHDRAW_BNB)},
        balance=0,
        quoted=0,
    )
    pending = run("lista_slisbnb", opened, "bsc-mainnet", clock=clock)
    assert pending.status == "observed"
    assert len(pending.withdrawals) == 1
    assert pending.withdrawals[0].request_id == "7"
    assert pending.withdrawals[0].index == 0
    assert pending.withdrawals[0].is_claimable is False
    assert pending.withdrawals[0].start_time == start
    withdrawal = next(leg for leg in pending.legs if leg.identity == "lista-slisbnb:withdrawal:7")
    assert withdrawal.raw_amount == WITHDRAW_BNB
    assert type(withdrawal.raw_amount) is int
    bnb = native_identity("bsc-mainnet")
    mixed = aggregate_portfolio(
        [pending],
        fungibles=(FungibleBalance("bsc-mainnet", bnb, WALLET_BNB, 18),),
    )
    assert mixed.amount("bsc-mainnet", bnb) == WALLET_BNB
    assert mixed.amount("bsc-mainnet", "lista-slisbnb:withdrawal:7") == WITHDRAW_BNB

    missing = world()
    install_chain_ids(missing)
    _install_lista(missing, rows=[], balance=0, quoted=0)
    gone = run("lista_slisbnb", missing, "bsc-mainnet", prior={"request_ids": ["7"]})
    assert gone.status == "none_observed"
    assert gone.legs == ()
    assert gone.assessments[0].status == "disappeared_unverified"
    assert gone.assessments[0].is_claimable is None
    assert gone.assessments[0].status != "claimed"
    gone_coverage = build_protocol_coverage(
        wallet_id=WALLET_ID,
        network="bsc-mainnet",
        positions=(gone,),
        requested_plugins=("lista_slisbnb",),
    )
    assert gone_coverage["withdrawals"].category == "partial"
    assert gone_coverage["withdrawals"].reason_codes == ("disappeared_unverified",)

    claimed_transport = world()
    install_chain_ids(claimed_transport)
    _install_lista(claimed_transport, rows=[], balance=0, quoted=0)
    claimed = run(
        "lista_slisbnb",
        claimed_transport,
        "bsc-mainnet",
        prior={"request_ids": ["7"]},
        private={"claim_evidence": [{"request_id": "7", "tx_status": "success"}]},
    )
    assert claimed.assessments[0].status == "claimed"
    assert claimed.assessments[0].is_claimable is None
    assert claimed.legs == ()

    failed_tx = world()
    install_chain_ids(failed_tx)
    _install_lista(failed_tx, rows=[], balance=0, quoted=0)
    unverified = run(
        "lista_slisbnb",
        failed_tx,
        "bsc-mainnet",
        prior={"request_ids": ["7"]},
        private={"claim_evidence": [{"request_id": "7", "tx_status": "failed"}]},
    )
    assert unverified.assessments[0].status == "disappeared_unverified"
    assert unverified.assessments[0].is_claimable is None

    kept = world()
    install_chain_ids(kept)
    _install_lista(kept, rows=[], balance=None, quoted=None)
    prior = {"cursor": "block:10", "note": "keep-me", "request_ids": ["7"]}
    failed = run("lista_slisbnb", kept, "bsc-mainnet", prior=prior)
    assert failed.status == "unknown"
    assert failed.prior_kept is True
    assert failed.cursor == "block:10"
    assert failed.legs == ()
    assert failed.retained_prior["note"] == "keep-me"
    assert failed.retained_prior["request_ids"] == ["7"]
    assert "read_failed" in failed.reason_codes
    assert any(json.loads(call.body.decode("utf-8"))["method"] == "eth_call" for call in kept.backend.calls)

    confirmed = chain_event("ethereum-mainnet", "0x" + "11" * 32, kind="asset_out", tx_status="success", finality="confirmed")
    finalized = chain_event("ethereum-mainnet", "0x" + "22" * 32, kind="asset_out", tx_status="success", finality="finalized")
    destination = chain_event("ethereum-mainnet", "0x" + "33" * 32, kind="asset_in", tx_status="success", finality="finalized")
    status, reason = assess_bridge(confirmed)
    assert status == FlowStatus.source_confirmed
    assert status != FlowStatus.completed
    assert reason == "destination_not_observed"
    status, reason = assess_bridge(finalized)
    assert status == FlowStatus.source_finalized
    assert status != FlowStatus.completed
    assert reason == "destination_not_observed"
    status, reason = assess_bridge(finalized, destination)
    assert status == FlowStatus.completed
    assert reason == "destination_confirmed"
    status, reason = assess_bridge(confirmed, timed_out=True)
    assert status == FlowStatus.timed_out
    assert reason == "timed_out"
    status, reason = assess_bridge(finalized, plugin_has_data=False)
    assert status == FlowStatus.unknown
    assert reason == "plugin_without_data"
    assert status != FlowStatus.source_finalized

    source_only = build_protocol_coverage(
        wallet_id=WALLET_ID,
        network="ethereum-mainnet",
        positions=(),
        requested_plugins=(),
        bridge_status=FlowStatus.source_finalized,
        bridge_known=True,
    )
    assert source_only["bridges"].category == "partial"
    assert source_only["bridges"].reason_codes == ("destination_not_observed",)
    timed = build_protocol_coverage(
        wallet_id=WALLET_ID,
        network="ethereum-mainnet",
        positions=(),
        requested_plugins=(),
        bridge_status=FlowStatus.timed_out,
        bridge_known=True,
    )
    assert timed["bridges"].category == "partial"
    assert timed["bridges"].reason_codes == ("timed_out",)
    blind = build_protocol_coverage(
        wallet_id=WALLET_ID,
        network="ethereum-mainnet",
        positions=(),
        requested_plugins=(),
        bridge_known=False,
    )
    assert blind["bridges"].category == "error"
    assert blind["bridges"].reason_codes == ("plugin_without_data",)


def test_mcm_w8_04_exposure_news_links_positions_and_dedups_reposts():
    sources = load_news_sources()
    required = {
        "https://blog.lista.org/",
        "https://blog.uxlink.io/",
        "https://docs.uxlink.io/layer/announcement/notice",
        "https://governance.aave.com/latest.rss?order=created",
        "https://kinzafinance.medium.com/",
        "https://medium.com/feed/@kinzafinance",
        "https://status.solana.com/history.rss",
        "https://x.com/UXLINKofficial",
        "https://x.com/aave",
        "https://x.com/kinzafinance",
        "https://x.com/lista_dao",
    }
    found = {feed.url for source in sources.values() for feed in source.feeds}
    assert required <= found
    assert all(source.official is True and source.retrieved == "2026-10-09" for source in sources.values())
    x_accounts = [feed for source in sources.values() for feed in source.feeds if feed.kind == "x_account"]
    assert {feed.url for feed in x_accounts} >= {
        "https://x.com/UXLINKofficial",
        "https://x.com/aave",
        "https://x.com/kinzafinance",
        "https://x.com/lista_dao",
    }
    assert all(feed.fetch == "manual_not_fetched_by_monitor" for feed in x_accounts)
    others = [feed for source in sources.values() for feed in source.feeds if feed.kind != "x_account"]
    assert others
    assert all(feed.fetch == "not_fetched_by_monitor" for feed in others)
    for path in (ROOT / "config" / "news-sources").glob("*.json"):
        text = path.read_text(encoding="utf-8").lower()
        assert "api_key" not in text
        assert "live_pass" not in text
        assert "credential" not in text

    rejected = parse_feed((NEWS_FIXTURES / "doctype.rss.xml").read_bytes(), source_id="synthetic")
    assert rejected.ok is False
    assert rejected.reason == "dtd_or_entity_rejected"
    assert rejected.items == ()
    lista_feed = parse_feed(
        (NEWS_FIXTURES / "lista-pause.rss.xml").read_bytes(),
        source_id="lista",
        source_protocols=("lista",),
    )
    assert lista_feed.ok is True
    assert lista_feed.items[0].title == "Lista pause"
    assert lista_feed.items[0].url == "https://news.example.invalid/lista/pause"
    assert lista_feed.items[0].protocols == ("lista_slisbnb",)
    assert lista_feed.items[0].kind == "incident"
    atom = parse_feed(
        (NEWS_FIXTURES / "aave-governance.atom.xml").read_bytes(),
        source_id="aave",
        source_protocols=("aave",),
    )
    assert atom.ok is True
    assert atom.items[0].title == "Aave governance note"
    assert atom.items[0].kind == "governance"
    assert atom.items[0].protocols == ("aave_v3",)

    lista_book = IncidentBook()
    lista_book.add_all(lista_feed.items)
    lista_position = PositionObservation(
        plugin_id="lista_slisbnb",
        wallet_id=WALLET_ID,
        network="bsc-mainnet",
        status="observed",
        legs=(PositionLeg(
            kind="receipt",
            identity=SLISBNB.lower(),
            raw_amount=SLIS_RAW,
            decimals=18,
            network="bsc-mainnet",
            plugin_id="lista_slisbnb",
            underlying_identity="evm-native:56",
        ),),
    )
    linked = link_exposure(lista_book.incidents, [WalletExposure(WALLET_ID, positions=(lista_position,))])
    assert len(linked) == 1
    assert linked[0].kind == "exposure"
    assert linked[0].via == "position"
    assert linked[0].loss_alert is False
    assert linked[0].protocols == ("lista_slisbnb",)

    aave_book = IncidentBook()
    aave_book.add_all(atom.items)
    pool = AAVE_POOLS["ethereum-mainnet"]
    approved = link_exposure(
        aave_book.incidents,
        [WalletExposure(WALLET_B, approvals=({"spender": pool, "status": "active"},))],
    )
    assert len(approved) == 1
    assert approved[0].via == "approval"
    assert approved[0].kind == "exposure"
    assert approved[0].protocols == ("aave_v3",)
    assert approved[0].loss_alert is False
    for status_name in ("revoked", "expired"):
        silent = link_exposure(
            aave_book.incidents,
            [WalletExposure(WALLET_B, approvals=({"spender": pool, "status": status_name},))],
        )
        assert silent == ()

    unrelated = parse_structured(
        [{
            "body": "Lista UXLINK and BNB are named in this fixture body",
            "protocols": ["79thVault"],
            "published_at": "2026-10-03T00:00:00Z",
            "title": "79thVault drained",
            "url": "https://news.example.invalid/79th",
        }],
        source_id="lista",
        source_protocols=("lista", "uxlink"),
    )
    assert unrelated.ok is True
    assert unrelated.items[0].protocols == ()
    unrelated_book = IncidentBook()
    unrelated_book.add(unrelated.items[0])
    assert link_exposure(unrelated_book.incidents, [WalletExposure(WALLET_ID, positions=(lista_position,))]) == ()

    crowd = parse_structured(
        [
            {"protocols": ["kinza"], "published_at": "2026-10-01T00:00:00Z", "title": "Kinza weekly", "url": "https://news.example.invalid/kinza"},
            {"protocols": ["lista"], "published_at": "2026-10-01T01:00:00Z", "title": "Lista weekly", "url": "https://news.example.invalid/lista-weekly"},
            {"protocols": ["uxlink"], "published_at": "2026-10-01T02:00:00Z", "title": "UXLINK weekly", "url": "https://news.example.invalid/uxlink"},
        ],
        source_id="synthetic",
    )
    crowd_book = IncidentBook()
    crowd_book.add_all(crowd.items)
    crowd_notices = link_exposure(
        crowd_book.incidents,
        [
            WalletExposure(WALLET_ID, positions=(lista_position,)),
            WalletExposure(WALLET_C),
        ],
    )
    assert any(notice.wallet_id == WALLET_ID and notice.protocols == ("lista_slisbnb",) for notice in crowd_notices)
    assert all(notice.wallet_id != WALLET_C for notice in crowd_notices)
    assert all("kinza" not in notice.protocols and "uxlink" not in notice.protocols for notice in crowd_notices)

    unknown = PositionObservation(
        plugin_id="aave_v3",
        wallet_id=WALLET_ID,
        network="ethereum-mainnet",
        status="unknown",
        cursor="block:1",
        prior_kept=True,
        reason_codes=("read_failed",),
    )
    second = parse_structured(
        [{
            "protocols": ["aave"],
            "published_at": "2026-10-04T00:00:00Z",
            "title": "Aave parameter note",
            "url": "https://news.example.invalid/aave/parameter",
        }],
        source_id="aave",
    )
    unknown_book = IncidentBook()
    unknown_book.add_all(atom.items)
    unknown_book.add_all(second.items)
    unknown_notices = link_exposure(unknown_book.incidents, [WalletExposure(WALLET_ID, positions=(unknown,))])
    assert len(unknown_notices) == 1
    assert unknown_notices[0].kind == "exposure_unknown"
    assert unknown_notices[0].incident_id is None
    assert unknown_notices[0].loss_alert is False
    assert unknown_notices[0].via == "check"

    assert canonicalize_url("HTTPS://News.Example.invalid/lista/pause?utm_source=x&ref=y&keep=1#frag") == (
        "https://news.example.invalid/lista/pause?keep=1"
    )
    start = datetime(2026, 10, 1, tzinfo=timezone.utc)
    within = (start + timedelta(hours=1)).isoformat()
    later = (start + timedelta(hours=48, seconds=1)).isoformat()
    deduped = parse_structured(
        [
            {
                "protocols": ["lista"],
                "published_at": start.isoformat(),
                "title": "Lista pause",
                "url": "https://News.Example.invalid/lista/pause?utm_source=rss&ref=1#top",
            },
            {
                "protocols": ["lista"],
                "published_at": within,
                "title": "Lista pause",
                "url": "https://news.example.invalid/lista/pause?utm_medium=repost",
            },
            {
                "protocols": ["lista"],
                "published_at": within,
                "title": "Repost: Lista pause",
                "url": "https://news.example.invalid/lista/mirror",
            },
            {
                "correction_of": "https://News.Example.invalid/lista/pause?utm_source=rss#top",
                "protocols": ["lista"],
                "published_at": within,
                "title": "Update: treasury parameter",
                "url": "https://news.example.invalid/lista/correction",
            },
            {
                "protocols": ["lista"],
                "published_at": later,
                "title": "Lista pause",
                "url": "https://news.example.invalid/lista/later",
            },
        ],
        source_id="lista",
    )
    assert deduped.ok is True
    book = IncidentBook()
    book.add_all(deduped.items)
    assert len(book.incidents) == 2
    assert len(book.alert_ids) == 2
    first, second_incident = book.incidents
    assert first.corrected is True
    assert first.title == "Update: treasury parameter"
    assert first.normalized_title == "lista pause"
    assert len(first.source_refs) == 4
    assert "https://news.example.invalid/lista/pause" in first.urls
    assert "https://news.example.invalid/lista/mirror" in first.urls
    assert "https://news.example.invalid/lista/correction" in first.urls
    assert first.published_at == start
    assert second_incident.normalized_title == "lista pause"
    assert second_incident.urls == ["https://news.example.invalid/lista/later"]
    assert book.alert_ids[0] == first.alert_id
    assert book.alert_ids[1] == second_incident.alert_id


def _install_lista(transport, *, rows, balance, quoted, statuses=None) -> None:
    install(
        transport,
        "evm-bsc",
        ListaSlisBnb.operation(LISTA_PROXY, "paused()", []),
        rpc_ok(abi_hex(["bool"], [False])),
    )
    install(
        transport,
        "evm-bsc",
        ListaSlisBnb.operation(LISTA_PROXY, "getUserWithdrawalRequests(address)", [OWNER]),
        rpc_ok(abi_hex(["(uint256,uint256,uint256)[]"], [rows])),
    )
    for index, (claimable, bnb_raw) in (statuses or {}).items():
        install(
            transport,
            "evm-bsc",
            ListaSlisBnb.operation(LISTA_PROXY, "getUserRequestStatus(address,uint256)", [OWNER, index]),
            rpc_ok(abi_hex(["bool", "uint256"], [claimable, bnb_raw])),
        )
    if balance is not None:
        install(
            transport,
            "evm-bsc",
            ListaSlisBnb.operation(SLISBNB, "balanceOf(address)", [OWNER]),
            rpc_ok(abi_hex(["uint256"], [balance])),
        )
    if quoted is not None:
        install(
            transport,
            "evm-bsc",
            ListaSlisBnb.operation(LISTA_PROXY, "convertSnBnbToBnb(uint256)", [balance]),
            rpc_ok(abi_hex(["uint256"], [quoted])),
        )


def test_mcm_w8_02_aave_reserves_that_miss_reported_debt_are_unknown():
    transport = world()
    install_chain_ids(transport)
    pool = AAVE_POOLS["ethereum-mainnet"]
    install(
        transport,
        "evm-eth",
        AaveV3.operation(pool, "getUserAccountData(address)", [OWNER]),
        rpc_ok(abi_hex(["uint256"] * 6, [COLLATERAL_BASE, DEBT_BASE, 0, 8000, 7000, HEALTH])),
    )
    install(transport, "evm-eth", AaveV3.operation(A_TOKEN, "balanceOf(address)", [OWNER]), rpc_ok(abi_hex(["uint256"], [ATOKEN_RAW])))
    install(transport, "evm-eth", AaveV3.operation(DEBT_TOKEN, "balanceOf(address)", [OWNER]), rpc_ok(abi_hex(["uint256"], [0])))
    observed = run(
        "aave_v3",
        transport,
        "ethereum-mainnet",
        private={"reserves": [{
            "a_token": A_TOKEN,
            "debt_decimals": 6,
            "decimals": 18,
            "network": "ethereum-mainnet",
            "underlying": UNDERLYING,
            "variable_debt_token": DEBT_TOKEN,
        }]},
    )
    # The pool reports debt that the supplied reserve list does not explain: never a partial "observed".
    assert observed.status == "unknown"
    assert observed.legs == ()
    assert "reserves_incomplete" in observed.reason_codes
