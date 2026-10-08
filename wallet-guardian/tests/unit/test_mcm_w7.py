"""Offline acceptance for MCM-W7. Synthetic fixtures only. No network.

MCM-W7-01 price-identity
MCM-W7-02 function-coverage
MCM-W7-03 list-exact-match
MCM-W7-04 risk-provenance
"""

from __future__ import annotations

import json
import socket
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from guardian.identities import _b58encode, parse_address
from guardian.pricing.catalog import T0_NETWORKS, default_risk_sources_path, load_risk_sources
from guardian.pricing.coingecko import CoinGeckoClient
from guardian.pricing.quote import PriceProvenance, PriceQuote, crosses_usd_threshold, value_usd
from guardian.risk.chainabuse import ChainabuseClient
from guardian.risk.goplus import GoPlusClient
from guardian.risk.labels import (
    assess_poisoning,
    community_report_label,
    goplus_bucket,
    label_from_sanction,
    labels_from_goplus,
    note_source_failure,
    official_exact_label,
    sanction_bucket,
    summarize,
)
from guardian.risk.ofac import diff_lists, ingest, screen
from guardian.sources.budget import BudgetBook
from guardian.sources.fixtures import FixtureBackend, RawResponse, request_key
from guardian.sources.transport import ReadOnlyTransport, build_endpoint_registry

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "tests" / "fixtures" / "risk"
CONFIG = ROOT / "config" / "risk-sources.json"
PUBLIC_IP = "1.1.1.1"
COINGECKO_URL = "https://coingecko.example.invalid"
GOPLUS_URL = "https://goplus.example.invalid"
CHAINABUSE_URL = "https://chainabuse.example.invalid"
NOW = 1_700_000_000
TODAY = date(2026, 10, 9)
RETRIEVED = "2026-10-09T00:00:00Z"
CG_KEY = "cg-test-secret-value"
GP_KEY = "goplus-test-secret-value"
CA_KEY = "chainabuse-test-secret-value"
ETH = "0x0000000000000000000000000000000000000abc"
ETH_NEAR = "0x0000000000000000000000000000000000000abd"
ETH_ADDED = "0x0000000000000000000000000000000000000ab1"
PASSPORT = "0x0000000000000000000000000000000000000002"
SOL = "8qbHbw2BbbTHBW1sbeqakYXVKRQM8Ne7pLK7m6CVfeR"
BTC = "bc1qqszqgpqyqszqgpqyqszqgpqyqszqgpqy5dl506"
LEGACY = "1TYPjCiGbKiwP6r12cdkmVjySbQryonEV"
KNOWN = "0x1111000000000000000000000000000000001111"
SENDER = "0x1111aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa1111"
RECIPIENT = "0x2222000000000000000000000000000000002222"
PREFIX_ONLY = "0x1111000000000000000000000000000000002222"


@pytest.fixture(autouse=True)
def _offline(monkeypatch):
    def explode(*_args, **_kwargs):
        raise AssertionError("network is forbidden in MCM-W7 tests")

    monkeypatch.setattr(socket, "socket", explode)
    monkeypatch.setattr(socket, "create_connection", explode)
    monkeypatch.setattr(socket, "getaddrinfo", explode)
    import httpx

    monkeypatch.setattr(httpx, "Client", explode)


def _resolve(_host: str) -> list[str]:
    return [PUBLIC_IP]


def catalog():
    return load_risk_sources(CONFIG)


def world(*, daily: int = 100, retry_attempts: int = 0):
    document = {
        "budgets": {
            "chainabuse": {"max_concurrency": 2, "max_requests_per_day": daily, "max_rps": 100, "retry_attempts": retry_attempts},
            "coingecko": {"max_concurrency": 2, "max_requests_per_day": daily, "max_rps": 100, "retry_attempts": retry_attempts},
            "goplus": {"max_concurrency": 2, "max_requests_per_day": daily, "max_rps": 100, "retry_attempts": retry_attempts},
        },
        "sources": {
            "chainabuse": {"adapter": "chainabuse", "budget_group": "chainabuse", "endpoint_env": "CHAINABUSE_API_URL"},
            "coingecko": {"adapter": "coingecko", "budget_group": "coingecko", "endpoint_env": "COINGECKO_API_URL"},
            "goplus": {"adapter": "goplus", "budget_group": "goplus", "endpoint_env": "GOPLUS_API_URL"},
        },
    }
    secrets = {
        "CHAINABUSE_API_URL": CHAINABUSE_URL,
        "COINGECKO_API_URL": COINGECKO_URL,
        "GOPLUS_API_URL": GOPLUS_URL,
    }
    endpoints = build_endpoint_registry(document, secrets=secrets)
    book = BudgetBook.from_document(document["budgets"], clock=lambda: float(NOW), sleep=lambda _seconds: None)
    backend = FixtureBackend({})
    return ReadOnlyTransport(endpoints, book, backend, resolve=_resolve)


def coingecko(transport, environ=None):
    return CoinGeckoClient(
        transport,
        "coingecko",
        catalog(),
        environ or {},
        clock=lambda: float(NOW),
        sleep=lambda _seconds: None,
        random=lambda: 0.0,
    )


def goplus(transport, environ=None):
    return GoPlusClient(
        transport,
        "goplus",
        catalog(),
        environ or {},
        clock=lambda: float(NOW),
        sleep=lambda _seconds: None,
        random=lambda: 0.0,
    )


def install(transport, source, operation, payload):
    call = transport.encode(source, operation)
    if isinstance(payload, RawResponse):
        body = payload
    else:
        body = RawResponse(200, {}, json.dumps(payload).encode("utf-8"))
    transport.backend.mapping[request_key(call.method, call.url, call.body)] = body
    return call


def urls(transport) -> list[str]:
    return [call.url for call in transport.backend.calls]


def assert_not_zero_or_one(quote):
    assert quote.price_usd is None
    assert quote.valuation == "valuation_unknown"
    assert quote.threshold_decision == "valuation_unknown"
    assert quote.price_usd != Decimal("0")
    assert quote.price_usd != Decimal("1")
    assert quote.value_usd is None


def flip_alpha(text: str) -> str:
    chars = []
    flipped = False
    for char in text:
        if not flipped and char.isalpha():
            chars.append(char.swapcase())
            flipped = True
        else:
            chars.append(char)
    assert flipped
    assert "".join(chars) != text
    return "".join(chars)


def test_mcm_w7_01_price_identity_same_symbol_does_not_borrow_price():
    loaded = catalog()
    assert default_risk_sources_path() == CONFIG
    assert load_risk_sources().coingecko_query_param == "x_cg_demo_api_key"
    text = CONFIG.read_text(encoding="utf-8")
    assert "live_pass" not in text
    assert '"enabled": true' not in text.lower()
    assert loaded.usd_threshold == Decimal("1000")
    assert loaded.max_price_age_seconds == 3600
    assert loaded.coingecko_query_param == "x_cg_demo_api_key"
    networks = {row.network_id: row for row in loaded.pricing_networks}
    assert set(networks) == set(T0_NETWORKS)
    transport = world()
    client = coingecko(transport)
    before = len(transport.backend.calls)
    for row in loaded.pricing_networks:
        chain = json.loads((ROOT / "config" / "chains" / f"{row.network_id}.json").read_text(encoding="utf-8"))
        assert chain["enabled"] is False
        assert chain["native_asset"]["identity"] == row.native_identity
        assert chain["native_asset"]["symbol"] == row.native_symbol
        assert chain["native_asset"]["decimals"] == row.native_decimals
        assert row.source.status == "documented"
        assert "live" not in row.source.status
        assert row.source.retrieved == "2026-10-09"
        assert row.source.url.startswith("https://")
        assert row.native_coin_id != row.native_symbol
        native = client.price_operation(network_id=row.network_id, identity=row.native_identity)
        assert native is not None
        assert native.path == "/api/v3/simple/price"
        assert native.query["ids"] == row.native_coin_id
        assert native.query["vs_currencies"] == "usd"
        assert native.query["include_last_updated_at"] == "true"
        assert "symbols" not in native.query
        assert "names" not in native.query
        sample = ETH if row.family == "evm" else SOL
        parsed = parse_address(row.family, sample)
        token = client.price_operation(network_id=row.network_id, identity=sample)
        assert token is not None
        assert token.path == f"/api/v3/simple/token_price/{row.platform_id}"
        expected_contract = parsed.identity_key if row.family == "evm" else parsed.canonical
        assert token.query["contract_addresses"] == expected_contract
        assert "symbols" not in token.query
    assert len(transport.backend.calls) == before

    real = client.price_operation(network_id="ethereum-mainnet", identity=ETH)
    fake = client.price_operation(network_id="ethereum-mainnet", identity=ETH_NEAR)
    checksum = parse_address("evm", ETH).display
    assert checksum != ETH
    assert client.price_operation(network_id="ethereum-mainnet", identity=checksum).query == real.query
    install(transport, "coingecko", real, {ETH: {"usd": 2.5, "last_updated_at": NOW}, "USDC": {"usd": 1, "last_updated_at": NOW}})
    install(transport, "coingecko", fake, {ETH: {"usd": 2.5, "last_updated_at": NOW}, "USDC": {"usd": 1, "last_updated_at": NOW}})
    priced = client.quote(network_id="ethereum-mainnet", identity=checksum, symbol="USDC", name="USD Coin", decimals=6, raw=1_000_000)
    missed = client.quote(network_id="ethereum-mainnet", identity=ETH_NEAR, symbol="USDC", name="USD Coin", decimals=6, raw=1_000_000)
    assert priced.identity == ETH
    assert priced.symbol == "USDC"
    assert priced.price_usd == Decimal("2.5")
    assert isinstance(priced.price_usd, Decimal)
    assert priced.price_quality == "available"
    assert priced.valuation == "valued"
    assert priced.value_usd == Decimal("2.5")
    assert priced.threshold_decision == "below"
    assert priced.provenance.source == "coingecko"
    assert priced.provenance.last_updated_at == NOW
    assert missed.price_usd is None
    assert missed.valuation == "valuation_unknown"
    assert missed.reason == "absent"
    assert missed.symbol == "USDC"
    assert missed.price_usd != priced.price_usd
    seen = urls(transport)
    assert any(ETH in url and "symbols=" not in url and "names=" not in url and "USDC" not in url for url in seen)
    assert any(ETH_NEAR in url and "USDC" not in url for url in seen)

    native_calls = len(transport.backend.calls)
    assert client.quote(network_id="solana-mainnet", identity="SOL", symbol="SOL").reason == "bad_identity"
    assert client.quote(network_id="bitcoin-mainnet", identity="bitcoin-native:BTC", symbol="BTC").reason == "unsupported_network"
    assert len(transport.backend.calls) == native_calls

    mint = _b58encode(bytes([3]) * 32)
    native_op = client.price_operation(network_id="solana-mainnet", identity="solana-native:SOL")
    mint_op = client.price_operation(network_id="solana-mainnet", identity=mint)
    install(transport, "coingecko", native_op, {"SOL": {"usd": 1, "last_updated_at": NOW}, "solana": {"usd": 20, "last_updated_at": NOW}})
    install(transport, "coingecko", mint_op, {"solana": {"usd": 20, "last_updated_at": NOW}})
    native_quote = client.quote(network_id="solana-mainnet", identity="solana-native:SOL", symbol="SOL", name="Solana")
    mint_quote = client.quote(network_id="solana-mainnet", identity=mint, symbol="SOL", name="Solana")
    assert native_quote.identity == "solana-native:SOL"
    assert native_quote.price_usd == Decimal("20")
    assert native_quote.price_usd != Decimal("1")
    assert mint_quote.price_usd is None
    assert mint_quote.valuation == "valuation_unknown"
    native_url = urls(transport)[-2]
    mint_url = urls(transport)[-1]
    assert "ids=solana" in native_url
    assert "symbols=" not in native_url
    assert "/api/v3/simple/token_price/solana" in mint_url
    assert mint in mint_url
    assert "ids=solana" not in mint_url

    wrapped = client.price_operation(network_id="ethereum-mainnet", identity="0x00000000000000000000000000000000000000ee")
    install(transport, "coingecko", wrapped, {})
    eth_symbol = client.quote(
        network_id="ethereum-mainnet",
        identity="0x00000000000000000000000000000000000000ee",
        symbol="ETH",
        name="Ether",
    )
    assert eth_symbol.price_usd is None
    assert "/api/v3/simple/token_price/ethereum" in urls(transport)[-1]
    assert "ids=ethereum" not in urls(transport)[-1]


def test_mcm_w7_01_price_identity_missing_and_failed_reads_are_valuation_unknown():
    transport = world()
    client = coingecko(transport, {"COINGECKO_API_KEY": CG_KEY})
    identity = "0x00000000000000000000000000000000000000aa"
    operation = client.price_operation(network_id="ethereum-mainnet", identity=identity)
    assert operation.query["x_cg_demo_api_key"] == CG_KEY
    assert "x_cg_pro_api_key" not in operation.query
    cases = {
        "absent": {},
        "null": {identity: {"usd": None, "last_updated_at": NOW}},
        "malformed": [],
        "text": "not-json",
        "bad-number": {identity: {"usd": "not-a-number", "last_updated_at": NOW}},
        "negative": {identity: {"usd": -1, "last_updated_at": NOW}},
    }
    for reason, payload in cases.items():
        if reason == "malformed":
            body = RawResponse(200, {}, b"[]")
        elif reason == "text":
            body = RawResponse(200, {}, b"not-json")
        else:
            body = payload
        install(transport, "coingecko", operation, body)
        quote = client.quote(network_id="ethereum-mainnet", identity=identity, symbol="USDC", name="USD Coin")
        assert_not_zero_or_one(quote)
        assert quote.symbol == "USDC"
        if reason == "absent":
            assert quote.reason == "absent"
        elif reason == "null":
            assert quote.reason == "null_price"
        elif reason in {"bad-number", "negative"}:
            assert quote.reason in {"malformed", "negative_price"}
        else:
            assert quote.reason
        assert CG_KEY not in quote.reason
        assert CG_KEY not in quote.provenance.path

    install(transport, "coingecko", operation, RawResponse(429, {}, b""))
    limited = client.quote(network_id="ethereum-mainnet", identity=identity, symbol="USDC")
    assert_not_zero_or_one(limited)
    assert limited.reason == "rate_limited"

    install(transport, "coingecko", operation, RawResponse(401, {}, b""))
    rejected = client.quote(network_id="ethereum-mainnet", identity=identity, symbol="USDC")
    assert_not_zero_or_one(rejected)
    assert rejected.reason == "credential_rejected"

    install(transport, "coingecko", operation, RawResponse(403, {}, b""))
    forbidden = client.quote(network_id="ethereum-mainnet", identity=identity, symbol="USDC")
    assert_not_zero_or_one(forbidden)
    assert forbidden.reason == "forbidden"

    sent = urls(transport)
    assert sent
    assert all("x_cg_demo_api_key=" in url for url in sent)
    assert all("x_cg_pro_api_key" not in url for url in sent)
    assert CG_KEY not in repr(client)
    assert all(CG_KEY not in client.redact_error(url) for url in sent)

    keyless_transport = world()
    keyless = coingecko(keyless_transport)
    keyless_op = keyless.price_operation(network_id="ethereum-mainnet", identity=identity)
    assert "x_cg_demo_api_key" not in keyless_op.query
    install(keyless_transport, "coingecko", keyless_op, {identity: {"usd": 2.5, "last_updated_at": NOW}})
    keyless_quote = keyless.quote(network_id="ethereum-mainnet", identity=identity, symbol="USDC")
    assert keyless_quote.price_usd == Decimal("2.5")
    assert "x_cg_demo_api_key" not in urls(keyless_transport)[-1]

    capped = world(daily=1)
    capped_client = coingecko(capped)
    first = capped_client.price_operation(network_id="ethereum-mainnet", identity=identity)
    install(capped, "coingecko", first, {identity: {"usd": 2.5, "last_updated_at": NOW}})
    assert capped_client.quote(network_id="ethereum-mainnet", identity=identity).price_usd == Decimal("2.5")
    calls = len(capped.backend.calls)
    exhausted = capped_client.quote(network_id="ethereum-mainnet", identity=ETH, symbol="USDC")
    assert_not_zero_or_one(exhausted)
    assert exhausted.reason == "daily_cap"
    assert len(capped.backend.calls) == calls


def test_mcm_w7_01_price_identity_stale_price_does_not_cross_threshold_and_decimals_are_exact():
    loaded = catalog()
    transport = world()
    client = coingecko(transport)
    identity = "0x00000000000000000000000000000000000000bb"
    operation = client.price_operation(network_id="ethereum-mainnet", identity=identity)
    fresh_at = NOW - loaded.max_price_age_seconds
    stale_at = fresh_at - 1
    install(transport, "coingecko", operation, {identity: {"usd": 5000, "last_updated_at": fresh_at}})
    fresh = client.quote(network_id="ethereum-mainnet", identity=identity, decimals=0, raw=1)
    install(transport, "coingecko", operation, {identity: {"usd": 5000, "last_updated_at": stale_at}})
    stale = client.quote(network_id="ethereum-mainnet", identity=identity, decimals=0, raw=1)
    install(transport, "coingecko", operation, {identity: {"usd": 5000}})
    undated = client.quote(network_id="ethereum-mainnet", identity=identity, decimals=0, raw=1)
    assert fresh.price_quality == "available"
    assert fresh.age_seconds == loaded.max_price_age_seconds
    assert fresh.threshold_decision == "above"
    assert fresh.valuation == "valued"
    assert stale.price_usd == Decimal("5000")
    assert stale.price_quality == "stale"
    assert stale.age_seconds == loaded.max_price_age_seconds + 1
    assert stale.value_usd == Decimal("5000")
    assert stale.threshold_decision == "valuation_unknown"
    assert stale.valuation == "valuation_unknown"
    assert stale.reason == "stale"
    assert undated.price_usd == Decimal("5000")
    assert undated.price_quality == "unknown"
    assert undated.age_seconds is None
    assert undated.threshold_decision == "valuation_unknown"
    assert undated.reason == "timestamp_missing"

    below_id = "0x0000000000000000000000000000000000000011"
    above_id = "0x0000000000000000000000000000000000000012"
    below_op = client.price_operation(network_id="ethereum-mainnet", identity=below_id)
    above_op = client.price_operation(network_id="ethereum-mainnet", identity=above_id)
    install(transport, "coingecko", below_op, {below_id: {"usd": 1, "last_updated_at": NOW}})
    install(transport, "coingecko", above_op, {above_id: {"usd": 1, "last_updated_at": NOW}})
    below = client.quote(network_id="ethereum-mainnet", identity=below_id, decimals=2, raw=99999)
    above = client.quote(network_id="ethereum-mainnet", identity=above_id, decimals=2, raw=100000)
    assert below.value_usd == Decimal("999.99")
    assert below.threshold_decision == "below"
    assert above.value_usd == Decimal("1000")
    assert above.threshold_decision == "above"
    assert crosses_usd_threshold(Decimal("999.99")) == "below"
    assert crosses_usd_threshold(Decimal("1000")) == "above"
    assert crosses_usd_threshold(Decimal("1000.00")) == "above"
    assert crosses_usd_threshold(None) == "valuation_unknown"
    assert crosses_usd_threshold(1.0) == "valuation_unknown"
    with pytest.raises(ValueError):
        value_usd(1, 18, 1.0)

    cases = (
        ("0x0000000000000000000000000000000000000018", 18, 10**18, 2.5, Decimal("2.5")),
        ("0x0000000000000000000000000000000000000006", 6, 10**6, 2.5, Decimal("2.5")),
        ("0x0000000000000000000000000000000000000009", 9, 2**53 + 1, 1, Decimal(2**53 + 1) / Decimal(10**9)),
        ("0x0000000000000000000000000000000000000008", 8, 10**8, 3.25, Decimal("3.25")),
        ("0x0000000000000000000000000000000000000053", 18, 2**53 + 9, 2, Decimal(2**53 + 9) * Decimal(2) / Decimal(10**18)),
    )
    for ident, decimals, raw, price, expected in cases:
        operation = client.price_operation(network_id="ethereum-mainnet", identity=ident)
        install(transport, "coingecko", operation, {ident: {"usd": price, "last_updated_at": NOW}})
        quote = client.quote(network_id="ethereum-mainnet", identity=ident, decimals=decimals, raw=raw)
        assert quote.value_usd == expected
        assert isinstance(quote.value_usd, Decimal)
        assert quote.price_usd == Decimal(str(price))
    huge = 2**53 + 9
    assert int(float(huge)) != huge
    assert cases[-1][4] != Decimal(int(float(huge))) * Decimal(2) / Decimal(10**18)

    dusty = client.price_operation(network_id="ethereum-mainnet", identity=identity)
    install(transport, "coingecko", dusty, {identity: {"usd": 5000, "last_updated_at": NOW, "low_liquidity": True}})
    flagged = client.quote(network_id="ethereum-mainnet", identity=identity, decimals=0, raw=1)
    caller = client.quote(network_id="ethereum-mainnet", identity=below_id, decimals=0, raw=100000, unverified=True)
    assert flagged.price_usd == Decimal("5000")
    assert flagged.display_only is True
    assert flagged.threshold_decision == "valuation_unknown"
    assert flagged.reason == "unverified_or_low_liquidity"
    assert caller.price_usd == Decimal("1")
    assert caller.value_usd == Decimal("100000")
    assert caller.display_only is True
    assert caller.threshold_decision == "valuation_unknown"


def test_mcm_w7_02_function_coverage_flagged_result_and_unsupported_chain():
    loaded = catalog()
    token = loaded.goplus_function("token_security")
    approval = loaded.goplus_function("approval_security")
    address = loaded.goplus_function("address_security")
    solana = loaded.goplus_function("solana_token_security")
    assert token.chain_id("ethereum-mainnet") == "1"
    assert token.chain_id("base-mainnet") == "8453"
    assert token.chain_id("solana-mainnet") is None
    assert approval.chain_id("base-mainnet") is None
    assert approval.chain_id("optimism-mainnet") is None
    assert "8453" not in approval.documented_chain_ids
    assert address.chain_id("mantle-mainnet") is None
    assert solana.chain_id("solana-mainnet") == "solana"
    assert solana.chain_id("ethereum-mainnet") is None
    assert token.source.status == "documented, not live verified"
    assert solana.source.url.endswith("solanatokensecurityusingget")

    transport = world()
    client = goplus(transport)
    eth = client.operation(function="token_security", network_id="ethereum-mainnet", target=ETH)
    sol = client.operation(function="solana_token_security", network_id="solana-mainnet", target=SOL)
    addr = client.operation(function="address_security", network_id="ethereum-mainnet", target=ETH)
    assert eth.path == "/api/v1/token_security/1"
    assert sol.path == "/api/v1/solana/token_security"
    assert addr.path == f"/api/v1/address_security/{ETH}"
    assert eth.path != sol.path
    assert addr.path != sol.path
    assert client.operation(function="solana_token_security", network_id="ethereum-mainnet", target=ETH) is None
    sol_address = client.operation(function="address_security", network_id="solana-mainnet", target=SOL)
    assert sol_address.path == f"/api/v1/address_security/{SOL}"
    assert sol_address.path != sol.path
    assert sol_address.query["chain_id"] == "solana"

    install(transport, "goplus", eth, {"code": 1, "message": "OK", "result": {ETH: {"is_honeypot": "1", "is_open_source": "0"}}})
    flagged = client.read(function="token_security", network_id="ethereum-mainnet", target=parse_address("evm", ETH).display)
    assert flagged.status == "flagged"
    assert flagged.safe is False
    assert flagged.requested is True
    assert flagged.credential_status == "keyless"
    assert dict(flagged.flags)["is_honeypot"] == "flagged"
    assert dict(flagged.flags)["is_open_source"] == "not_flagged"
    labels = labels_from_goplus(flagged, retrieved_at=RETRIEVED)
    assert len(labels) == 1
    assert labels[0].kind == "structural"
    assert labels[0].function == "token_security"
    assert labels[0].detail == "is_honeypot"
    assert labels[0].matched_identifier == ETH
    assert goplus_bucket(flagged) == "checked"

    calls = len(transport.backend.calls)
    base = client.read(function="approval_security", network_id="base-mainnet", target=ETH)
    mantle = client.read(function="address_security", network_id="mantle-mainnet", target=ETH)
    wrong_family = client.read(function="solana_token_security", network_id="ethereum-mainnet", target=ETH)
    assert len(transport.backend.calls) == calls
    assert base.status == "unsupported"
    assert mantle.status == "unsupported"
    assert wrong_family.status == "unsupported"
    assert base.requested is False
    assert base.safe is False
    assert goplus_bucket(base) == "unsupported"

    install(transport, "goplus", sol, {"code": 1, "message": "OK", "result": {SOL: {"mutable_metadata": "1"}}})
    install(transport, "goplus", addr, {"code": 1, "message": "OK", "result": {"phishing_activities": "1", "stealing_attack": "0"}})
    solana_read = client.read(function="solana_token_security", network_id="solana-mainnet", target=SOL)
    address_read = client.read(function="address_security", network_id="ethereum-mainnet", target=ETH)
    assert solana_read.status == "flagged"
    assert solana_read.function == "solana_token_security"
    assert dict(solana_read.flags)["mutable_metadata"] == "flagged"
    assert address_read.status == "flagged"
    assert address_read.function == "address_security"
    assert address_read.function != solana_read.function
    assert dict(address_read.flags)["phishing_activities"] == "flagged"
    assert dict(address_read.flags)["stealing_attack"] == "not_flagged"
    assert "/api/v1/solana/token_security" in urls(transport)[-2]
    assert "/api/v1/address_security/" in urls(transport)[-1]


def test_mcm_w7_02_function_coverage_errors_are_unknown_not_safe():
    transport = world()
    client = goplus(transport)
    operation = client.operation(function="token_security", network_id="ethereum-mainnet", target=ETH)
    other = "0x0000000000000000000000000000000000000004"
    install(transport, "goplus", operation, {"code": 1, "message": "OK", "result": {other: {"is_honeypot": "1"}}})
    missing = client.read(function="token_security", network_id="ethereum-mainnet", target=ETH)
    assert missing.status == "unknown"
    assert missing.reason == "missing_result"
    assert missing.safe is False
    assert labels_from_goplus(missing, retrieved_at=RETRIEVED) == ()

    install(transport, "goplus", operation, {"code": 0, "message": "NOT OK", "result": {ETH: {"is_honeypot": "1"}}})
    bad_code = client.read(function="token_security", network_id="ethereum-mainnet", target=ETH)
    assert bad_code.status == "unknown"
    assert bad_code.reason == "code"
    assert bad_code.flags == ()

    install(transport, "goplus", operation, {"code": True, "message": "OK", "result": {ETH: {"is_honeypot": "1"}}})
    assert client.read(function="token_security", network_id="ethereum-mainnet", target=ETH).status == "unknown"
    install(transport, "goplus", operation, {"code": "1", "message": "OK", "result": {ETH: {"is_honeypot": "1"}}})
    assert client.read(function="token_security", network_id="ethereum-mainnet", target=ETH).reason == "code"

    install(transport, "goplus", operation, {"code": 1, "message": "OK", "result": {ETH: {"is_honeypot": "0", "is_open_source": "0"}}})
    zeros = client.read(function="token_security", network_id="ethereum-mainnet", target=ETH)
    assert zeros.status == "not_flagged"
    assert zeros.reason == "not_flagged_by_this_function"
    assert zeros.safe is False
    assert zeros.status != "safe"
    assert labels_from_goplus(zeros, retrieved_at=RETRIEVED) == ()
    clear = summarize((), checked=(f"goplus:{zeros.function}",))
    assert clear.status == "no_hit_in_checked_sources"
    assert f"goplus:{zeros.function}" in clear.sources_checked
    assert clear.labels == ()
    assert "safe" not in clear.status
    assert "clean" not in clear.status
    install(transport, "goplus", operation, {"code": 1, "message": "OK", "result": {}})
    empty = client.read(function="token_security", network_id="ethereum-mainnet", target=ETH)
    assert empty.status == "unknown"
    assert empty.reason == "missing_result"
    assert empty.safe is False

    install(transport, "goplus", operation, {"code": 1, "message": "OK", "result": {ETH: {"is_honeypot": 1}}})
    integer_flag = client.read(function="token_security", network_id="ethereum-mainnet", target=ETH)
    assert integer_flag.status == "unknown"
    assert integer_flag.reason == "flag_not_string"
    assert integer_flag.safe is False

    install(transport, "goplus", operation, RawResponse(429, {}, b""))
    limited = client.read(function="token_security", network_id="ethereum-mainnet", target=ETH)
    assert limited.status == "unknown"
    assert limited.reason == "rate_limited"
    assert limited.safe is False
    assert goplus_bucket(limited) == "unknown"

    install(transport, "goplus", operation, RawResponse(401, {}, b""))
    assert client.read(function="token_security", network_id="ethereum-mainnet", target=ETH).reason == "credential_rejected"
    install(transport, "goplus", operation, RawResponse(403, {}, b""))
    forbidden = client.read(function="token_security", network_id="ethereum-mainnet", target=ETH)
    assert forbidden.status == "unknown"
    assert forbidden.reason == "forbidden"
    install(transport, "goplus", operation, RawResponse(200, {}, b"[]"))
    assert client.read(function="token_security", network_id="ethereum-mainnet", target=ETH).reason == "malformed"

    capped = world(daily=1)
    capped_client = goplus(capped)
    capped_op = capped_client.operation(function="token_security", network_id="ethereum-mainnet", target=ETH)
    install(capped, "goplus", capped_op, {"code": 1, "message": "OK", "result": {ETH: {"is_honeypot": "1"}}})
    assert capped_client.read(function="token_security", network_id="ethereum-mainnet", target=ETH).status == "flagged"
    calls = len(capped.backend.calls)
    exhausted = capped_client.read(function="token_security", network_id="bsc-mainnet", target=ETH)
    assert exhausted.status == "unknown"
    assert exhausted.reason == "daily_cap"
    assert exhausted.safe is False
    assert len(capped.backend.calls) == calls


def test_mcm_w7_02_function_coverage_configured_key_is_not_sent():
    transport = world()
    client = goplus(transport, {"GOPLUS_API_KEY": GP_KEY})
    operation = client.operation(function="token_security", network_id="ethereum-mainnet", target=ETH)
    assert GP_KEY not in json.dumps(operation.query)
    install(transport, "goplus", operation, {"code": 1, "message": "OK", "result": {ETH: {"is_honeypot": "1"}}})
    flagged = client.read(function="token_security", network_id="ethereum-mainnet", target=ETH)
    assert flagged.status == "flagged"
    assert flagged.credential_status == "credential_transport_unsupported"
    assert flagged.safe is False
    assert urls(transport)
    assert all(GP_KEY not in url for url in urls(transport))
    assert GP_KEY not in repr(client)
    assert GP_KEY not in client.redact_error(GOPLUS_URL + "?" + GP_KEY)


def test_mcm_w7_03_list_exact_match_synthetic_sdn_and_normalization():
    note = json.loads((FIXTURES / "provenance.json").read_text(encoding="utf-8"))
    assert note["synthetic"] is True
    assert note["not_evidence"] is True
    loaded = catalog()
    current = ingest(FIXTURES / "sdn-v1.xml", loaded, retrieved_at=RETRIEVED)
    assert current.status == "parsed"
    assert current.publish_date == "2026-10-08"
    assert current.version == "2026-10-08:4"
    eth_networks = set(loaded.ticker("ETH").networks)
    for path in (ROOT / "config" / "chains").glob("*.json"):
        body = json.loads(path.read_text(encoding="utf-8"))
        assert body["enabled"] is False
        if body.get("family") == "evm":
            assert body["network"] in eth_networks
    assert "solana-mainnet" not in eth_networks
    assert "bitcoin-mainnet" not in eth_networks
    assert "tron-mainnet" not in eth_networks
    assert loaded.ticker("SOL").networks == ("solana-mainnet",)
    assert loaded.ticker("TRX").networks == ("tron-mainnet",)
    assert loaded.ticker("BTC").networks == ("bitcoin-mainnet",)
    assert loaded.ticker("XBT").networks == ("bitcoin-mainnet",)
    eth = screen(current, network_id="ethereum-mainnet", address=ETH, catalog=loaded, now=TODAY)
    checksum = parse_address("evm", ETH).display
    upper = "0x" + ETH[2:].upper()
    by_checksum = screen(current, network_id="ethereum-mainnet", address=checksum, catalog=loaded, now=TODAY)
    by_upper = screen(current, network_id="ethereum-mainnet", address=upper, catalog=loaded, now=TODAY)
    assert eth.status == "hit"
    assert eth.stale is False
    assert eth.reason == "exact"
    assert eth.ticker == "ETH"
    assert eth.entry_uid == "fixture-900001"
    assert eth.matched_identifier == checksum
    assert eth.comparison_key == ETH
    assert by_checksum.status == "hit"
    assert by_upper.status == "hit"
    assert by_checksum.comparison_key == eth.comparison_key
    for network_id in ("arbitrum-one", "base-mainnet", "bsc-mainnet", "optimism-mainnet", "polygon-pos", "avalanche-c"):
        hit = screen(current, network_id=network_id, address=ETH, catalog=loaded, now=TODAY)
        assert hit.status == "hit"
        assert hit.comparison_key == ETH
    near = screen(current, network_id="ethereum-mainnet", address=ETH_NEAR, catalog=loaded, now=TODAY)
    assert near.status == "no_hit"
    assert near.matched_identifier is None
    passport = screen(current, network_id="ethereum-mainnet", address=PASSPORT, catalog=loaded, now=TODAY)
    assert passport.status == "no_hit"
    solana_screen = screen(current, network_id="solana-mainnet", address=ETH, catalog=loaded, now=TODAY)
    assert solana_screen.status != "hit"

    sol = screen(current, network_id="solana-mainnet", address=SOL, catalog=loaded, now=TODAY)
    assert sol.status == "hit"
    assert sol.ticker == "SOL"
    assert sol.matched_identifier == SOL
    flipped_sol = screen(current, network_id="solana-mainnet", address=flip_alpha(SOL), catalog=loaded, now=TODAY)
    assert flipped_sol.status != "hit"

    bech32 = screen(current, network_id="bitcoin-mainnet", address=BTC.upper(), catalog=loaded, now=TODAY)
    assert bech32.status == "hit"
    assert bech32.ticker == "XBT"
    assert bech32.matched_identifier == BTC
    legacy = screen(current, network_id="bitcoin-mainnet", address=LEGACY, catalog=loaded, now=TODAY)
    assert legacy.status == "hit"
    assert legacy.ticker == "BTC"
    assert screen(current, network_id="bitcoin-mainnet", address=flip_alpha(LEGACY), catalog=loaded, now=TODAY).status != "hit"
    changed = BTC[:8] + ("p" if BTC[8] != "p" else "q") + BTC[9:]
    assert changed != BTC
    assert screen(current, network_id="bitcoin-mainnet", address=changed, catalog=loaded, now=TODAY).status != "hit"
    assert screen(current, network_id="ethereum-mainnet", address=SOL, catalog=loaded, now=TODAY).status != "hit"


def test_mcm_w7_03_list_exact_match_version_diff_records_delisting():
    loaded = catalog()
    older = ingest(FIXTURES / "sdn-v1.xml", loaded, retrieved_at=RETRIEVED)
    newer = ingest(FIXTURES / "sdn-v2.xml", loaded, retrieved_at=RETRIEVED)
    diff = diff_lists(older, newer)
    assert diff.comparable is True
    assert older.version != newer.version
    removed = ETH
    added = ETH_ADDED
    assert any(item.comparison_key == added and item.ticker == "ETH" for item in diff.added)
    assert any(item.comparison_key == SOL for item in newer.identifiers)
    delisted = [item for item in diff.delisted if item.identifier.comparison_key == removed]
    assert len(delisted) == 1
    assert delisted[0].status == "delisted"
    assert delisted[0].present_in_version == older.version
    assert delisted[0].removed_in_version == newer.version
    assert older.version in delisted[0].evidence
    assert newer.version in delisted[0].evidence
    assert removed not in {item.comparison_key for item in diff.added}
    assert SOL not in {item.comparison_key for item in diff.added}
    assert SOL not in {item.identifier.comparison_key for item in diff.delisted}
    assert BTC not in {item.identifier.comparison_key for item in diff.delisted}
    current = screen(newer, network_id="ethereum-mainnet", address=removed, catalog=loaded, now=TODAY)
    assert current.status == "no_hit"
    assert any(item.identifier.comparison_key == removed for item in diff.delisted)
    same = diff_lists(older, older)
    assert same.comparable is False
    assert same.added == ()
    assert same.delisted == ()
    assert diff_lists(older, None).comparable is False


def test_mcm_w7_03_list_exact_match_rejects_dtd_and_unknown_or_stale_list():
    loaded = catalog()
    for name in ("sdn-doctype.xml", "sdn-entity.xml"):
        rejected = ingest(FIXTURES / name, loaded, retrieved_at=RETRIEVED)
        assert rejected.status == "unknown"
        assert rejected.reason == "dtd_or_entity_rejected"
        assert rejected.identifiers == ()
        blocked = screen(rejected, network_id="ethereum-mainnet", address=ETH, catalog=loaded, now=TODAY)
        assert blocked.status == "unknown"
        assert blocked.status != "hit"
        assert blocked.status != "no_hit"
    bom = ingest(b"\xff\xfe<\x00", loaded)
    assert bom.status == "unknown"
    assert bom.reason == "unparseable"
    assert bom.identifiers == ()
    assert screen(bom, network_id="ethereum-mainnet", address=ETH, catalog=loaded, now=TODAY).status == "unknown"
    future = ingest(
        b"""<?xml version="1.0" encoding="UTF-8"?>
<sdnList><publshInformation><Publish_Date>2099-01-01</Publish_Date><Record_Count>1</Record_Count></publshInformation>
<sdnEntry><uid>fixture-900001</uid><idList><id><uid>fixture-900101</uid>
<idType>Digital Currency Address - ETH</idType>
<idNumber>0x0000000000000000000000000000000000000abc</idNumber>
</id></idList></sdnEntry></sdnList>""",
        loaded,
    )
    future_screen = screen(future, network_id="ethereum-mainnet", address=ETH, catalog=loaded, now=TODAY)
    assert future.status == "parsed"
    assert future_screen.status == "unknown"
    assert future_screen.reason == "undated"
    assert future_screen.status != "no_hit"
    missing = screen(None, network_id="ethereum-mainnet", address=ETH, catalog=loaded, now=TODAY)
    assert missing.status == "unknown"
    assert missing.reason == "list_missing"
    assert missing.status != "no_hit"
    absent = ingest(FIXTURES / "missing-sdn.xml", loaded)
    assert absent.status == "unknown"
    assert absent.reason == "list_missing"

    stale_list = ingest(FIXTURES / "sdn-stale.xml", loaded, retrieved_at=RETRIEVED)
    assert stale_list.publish_date == "2020-01-01"
    stale_hit = screen(stale_list, network_id="ethereum-mainnet", address=ETH, catalog=loaded, now=TODAY)
    stale_miss = screen(stale_list, network_id="ethereum-mainnet", address=ETH_NEAR, catalog=loaded, now=TODAY)
    assert stale_hit.status == "hit"
    assert stale_hit.stale is True
    assert stale_hit.matched_identifier == parse_address("evm", ETH).display
    assert stale_miss.status == "stale"
    assert stale_miss.status != "no_hit"
    assert sanction_bucket(stale_miss) == "stale"
    assert sanction_bucket(missing) == "unknown"


def test_mcm_w7_04_risk_provenance_keeps_kinds_and_precedence():
    loaded = catalog()
    current = ingest(FIXTURES / "sdn-v1.xml", loaded, retrieved_at=RETRIEVED)
    official = label_from_sanction(
        screen(current, network_id="ethereum-mainnet", address=ETH, catalog=loaded, now=TODAY),
        retrieved_at=RETRIEVED,
    )
    assert official is not None
    assert official.kind == "official_sanction"
    assert official.list_version == current.version
    assert official.evidence_ref
    exact = official_exact_label(
        source="official-fixture",
        function="listed_identifier",
        matched_identifier=ETH,
        evidence_ref="official-fixture:fixture-1",
        retrieved_at=RETRIEVED,
        list_version="fixture-1",
        list_date="2026-10-09",
    )
    report = community_report_label(
        matched_identifier=ETH,
        evidence_ref="chainabuse:fixture-report-1",
        retrieved_at=RETRIEVED,
        detail="community_report",
    )
    transport = world()
    client = goplus(transport)
    operation = client.operation(function="token_security", network_id="ethereum-mainnet", target=ETH)
    install(transport, "goplus", operation, {"code": 1, "message": "OK", "result": {ETH: {"is_honeypot": "1", "is_open_source": "0"}}})
    structural = labels_from_goplus(
        client.read(function="token_security", network_id="ethereum-mainnet", target=ETH),
        retrieved_at=RETRIEVED,
    )
    assert len(structural) == 1
    assert structural[0].kind == "structural"
    summary = summarize(
        [structural[0], report, exact, official],
        checked=("ofac", "official-fixture", "goplus:token_security"),
    )
    assert summary.status == "labelled"
    assert summary.highest_kind == "official_sanction"
    assert [label.kind for label in summary.labels] == [
        "official_sanction",
        "official_exact",
        "community_report",
        "structural",
    ]
    assert summary.labels[2].evidence_ref == "chainabuse:fixture-report-1"
    assert "is_open_source" not in {label.detail for label in summary.labels}

    abuse = ChainabuseClient(transport, "chainabuse", {"CHAINABUSE_API_KEY": CA_KEY})
    calls = len(transport.backend.calls)
    blocked = abuse.lookup(ETH)
    assert len(transport.backend.calls) == calls
    assert blocked.status == "blocked"
    assert blocked.reason == "credential_transport_unsupported"
    assert blocked.reports is None
    assert blocked.safe is False
    assert blocked.requested is False
    assert CA_KEY not in repr(abuse)
    missing_key = ChainabuseClient(transport, "chainabuse", {})
    assert missing_key.lookup(ETH).reason == "credential_missing"
    assert len(transport.backend.calls) == calls


def test_mcm_w7_04_risk_provenance_no_hit_lists_unknown_sources():
    transport = world()
    client = goplus(transport)
    operation = client.operation(function="token_security", network_id="ethereum-mainnet", target=ETH)
    install(transport, "goplus", operation, RawResponse(429, {}, b""))
    failed = client.read(function="token_security", network_id="ethereum-mainnet", target=ETH)
    unsupported = client.read(function="approval_security", network_id="base-mainnet", target=ETH)
    missing = screen(None, network_id="ethereum-mainnet", address=ETH, catalog=catalog(), now=TODAY)
    blocked = ChainabuseClient(transport, "chainabuse", {}).lookup(ETH)
    summary = summarize(
        (),
        unknown=(f"goplus:{failed.function}", "ofac"),
        unsupported=(f"goplus:{unsupported.function}",),
        blocked=("chainabuse",),
    )
    assert failed.status == "unknown"
    assert unsupported.status == "unsupported"
    assert missing.status == "unknown"
    assert blocked.reason == "credential_missing"
    assert summary.status == "no_hit_in_checked_sources"
    assert summary.highest_kind is None
    assert summary.labels == ()
    assert "goplus:token_security" in summary.sources_unknown
    assert "ofac" in summary.sources_unknown
    assert "goplus:approval_security" in summary.sources_unsupported
    assert "chainabuse" in summary.sources_blocked
    assert summary.status not in {"clean", "safe"}
    assert "safe" not in summary.status
    assert "clean" not in summary.status


def test_mcm_w7_04_risk_provenance_poisoning_dust_labels_sender_only():
    sender = parse_address("evm", SENDER).display
    recipient = parse_address("evm", RECIPIENT).display
    known = parse_address("evm", KNOWN).display
    display_price = PriceQuote(
        network_id="ethereum-mainnet",
        identity=ETH,
        symbol="USDC",
        price_usd=Decimal("1000000"),
        price_quality="available",
        valuation="valued",
        reason="",
        value_usd=Decimal("1000000"),
        threshold_decision="above",
        age_seconds=0,
        display_only=False,
        provenance=PriceProvenance("coingecko", NOW, NOW, "/api/v3/simple/token_price/ethereum"),
    )
    assert display_price.threshold_decision == "above"
    dust = assess_poisoning(
        sender=sender,
        recipient=recipient,
        known_counterparties=(known,),
        raw_amount=0,
        display_price=display_price,
        retrieved_at=RETRIEVED,
    )
    assert len(dust.sender_labels) == 1
    label = dust.sender_labels[0]
    assert label.kind == "heuristic"
    assert label.detail == "poisoning_suspect"
    assert label.matched_identifier == sender
    assert label.matched_identifier != recipient
    assert label.matched_identifier != known
    assert dust.recipient_labels == ()
    assert dust.counterparty_labels == ()
    assert dust.usd_threshold_decision == "valuation_unknown"
    assert dust.display_price_triggers_alert is False
    tiny = assess_poisoning(
        sender=SENDER,
        recipient=RECIPIENT,
        known_counterparties=(KNOWN,),
        raw_amount=1,
        dust=True,
        display_price=display_price,
        retrieved_at=RETRIEVED,
    )
    assert tiny.sender_labels[0].detail == "poisoning_suspect"
    assert tiny.usd_threshold_decision == "valuation_unknown"
    prefix = assess_poisoning(
        sender=PREFIX_ONLY,
        recipient=RECIPIENT,
        known_counterparties=(KNOWN,),
        raw_amount=0,
        display_price=display_price,
    )
    large = assess_poisoning(
        sender=SENDER,
        recipient=RECIPIENT,
        known_counterparties=(KNOWN,),
        raw_amount=10**18,
        display_price=display_price,
    )
    assert prefix.sender_labels == ()
    assert large.sender_labels == ()
    assert prefix.usd_threshold_decision == "not_applicable"
    assert large.display_price_triggers_alert is False
    same_party = assess_poisoning(
        sender=SENDER,
        recipient=SENDER,
        known_counterparties=(KNOWN,),
        raw_amount=0,
        display_price=display_price,
    )
    assert same_party.sender_labels == ()
    assert same_party.recipient_labels == ()
    assert same_party.usd_threshold_decision == "not_applicable"


def test_mcm_w7_04_risk_provenance_source_failure_keeps_stale_label():
    loaded = catalog()
    current = ingest(FIXTURES / "sdn-v1.xml", loaded, retrieved_at=RETRIEVED)
    official = label_from_sanction(
        screen(current, network_id="ethereum-mainnet", address=ETH, catalog=loaded, now=TODAY),
        retrieved_at=RETRIEVED,
    )
    dust = assess_poisoning(
        sender=SENDER,
        recipient=RECIPIENT,
        known_counterparties=(KNOWN,),
        raw_amount=0,
        retrieved_at=RETRIEVED,
    )
    summary = summarize(
        [official, dust.sender_labels[0]],
        checked=("ofac", "heuristic"),
    )
    assert summary.highest_kind == "official_sanction"
    assert {label.kind for label in summary.labels} == {"official_sanction", "heuristic"}
    failed = note_source_failure(summary, source="ofac", reason="list_missing")
    kept = [label for label in failed.labels if label.kind == "official_sanction"]
    assert len(kept) == 1
    assert kept[0].stale is True
    assert kept[0].matched_identifier == official.matched_identifier
    assert kept[0].evidence_ref == official.evidence_ref
    heuristic = [label for label in failed.labels if label.kind == "heuristic"]
    assert len(heuristic) == 1
    assert heuristic[0].stale is False
    assert failed.status == "labelled"
    assert failed.highest_kind == "official_sanction"
    assert "ofac" in failed.sources_stale
    assert "ofac" not in failed.sources_checked
    assert failed.status != "no_hit_in_checked_sources"
