"""Token quotes and the unvalued large-move fallback. A failed read is not zero."""

from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace

import pytest

from guardian.errors import ConfigError
from guardian.pricing.catalog import load_risk_sources
from guardian.pricing.coingecko import CoinGeckoClient
from guardian.pending_alerts import candidate_from_snapshot, snapshot_candidate
from guardian.pricing.token_decimals import fill_row_decimals
from guardian.pricing.token_valuation import apply_token_valuation
from guardian.rules import AlertPolicy, Candidate, validate_token_raw_thresholds
from guardian.sources.transport import TransportError

USDC = "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48"
OTHER = "0x" + "ab" * 20
NOW = 1_700_000_000


class _Transport:
    def __init__(self, body=None, exc: Exception | None = None) -> None:
        self.body = body
        self.exc = exc
        self.calls = []

    def request(self, source_id: str, operation) -> SimpleNamespace:
        self.calls.append(operation)
        if self.exc is not None:
            raise self.exc
        return SimpleNamespace(body=self.body, category="ok", status=200, blocked=False, reason="")

    def retry_attempts(self, source_id: str) -> int:
        return 1


def _client(transport: _Transport) -> CoinGeckoClient:
    return CoinGeckoClient(transport, "prices", load_risk_sources(), {}, clock=lambda: NOW)


def _token(
    asset: str,
    raw: int,
    *,
    decimals: int | None = None,
    network: str = "ethereum-mainnet",
    valuation_reason: str | None = None,
) -> Candidate:
    return Candidate(
        tenant_id="local",
        wallet_id="desk",
        network=network,
        capability="history.fungible",
        identity=f"{network}:tx:{raw}:asset_in:{asset}",
        kind="asset",
        raw_amount=raw,
        is_native=False,
        asset=asset,
        decimals=decimals,
        direction="in",
        tx_id="0x" + "11" * 32,
        valuation_reason=valuation_reason,
    )


def _config(thresholds: dict | None = None) -> SimpleNamespace:
    alerts: dict = {"asset_usd_threshold": "1000"}
    if thresholds is not None:
        alerts["token_raw_thresholds"] = thresholds
    return SimpleNamespace(document={"alerts": alerts, "sources": {}})


def test_contract_price_values_a_token_and_a_miss_is_not_zero() -> None:
    body = {USDC: {"usd": 1, "last_updated_at": NOW}}
    transport = _Transport(body)
    parsed = _client(transport).token_prices(
        network_id="ethereum-mainnet",
        contracts=[USDC],
        max_age_seconds=900,
        now=NOW,
    )
    assert parsed[USDC]["quality"] == "available"
    assert parsed[USDC]["price"] == Decimal("1")
    assert transport.calls[0].path == "/api/v3/simple/token_price/ethereum"
    assert "contract_addresses" in transport.calls[0].query
    assert "symbols" not in transport.calls[0].query
    missing = _client(_Transport({})).token_prices(
        network_id="ethereum-mainnet",
        contracts=[OTHER],
        max_age_seconds=900,
        now=NOW,
    )
    assert missing[OTHER]["quality"] == "unavailable"
    assert "price" not in missing[OTHER]
    assert missing[OTHER].get("price") != 0


def test_failed_token_price_does_not_become_zero() -> None:
    parsed = _client(_Transport(exc=TransportError("read_error"))).token_prices(
        network_id="ethereum-mainnet",
        contracts=[USDC],
        max_age_seconds=900,
        now=NOW,
    )
    assert parsed[USDC]["quality"] == "unavailable"
    assert parsed[USDC].get("price", None) in {None, ""}
    zero = _client(_Transport({USDC: {"usd": 0, "last_updated_at": NOW}})).token_prices(
        network_id="ethereum-mainnet",
        contracts=[USDC],
        max_age_seconds=900,
        now=NOW,
    )
    assert zero[USDC]["quality"] == "unavailable"
    assert zero[USDC].get("reason") == "non_positive_price"


def test_stable_and_raw_threshold_alert_without_a_price() -> None:
    large = _token(USDC, 2_000 * 10**6)
    small = _token(USDC, 10**6)
    other = _token(OTHER, 10**30)
    rows = [large, small, other]
    apply_token_valuation(rows, _config(), _Transport(), lambda: NOW)
    policy = AlertPolicy(Decimal("0.5"), Decimal("1000"))
    decisions = {item.identity: item for item in policy.apply(rows)}
    assert decisions[large.identity].kind == "unvalued_large_move"
    assert decisions[large.identity].alert is True
    assert decisions[large.identity].valuation_unknown is True
    assert large.quote_usd is None
    assert decisions[small.identity].alert is False
    assert decisions[other.identity].alert is False
    configured = [_token(OTHER, 50)]
    apply_token_valuation(
        configured,
        _config({f"ethereum-mainnet:{OTHER}": "50"}),
        _Transport(),
        lambda: NOW,
    )
    hit = policy.apply(configured)[0]
    assert hit.kind == "unvalued_large_move"
    assert hit.alert is True
    assert configured[0].quote_usd is None


def test_fresh_contract_price_reaches_asset_usd() -> None:
    class _Endpoints:
        def get(self, name: str) -> SimpleNamespace:
            return SimpleNamespace(endpoint="https://prices.example.invalid")

    class _Priced(_Transport):
        endpoints = _Endpoints()

    transport = _Priced({USDC: {"usd": 1, "last_updated_at": NOW}})
    public = SimpleNamespace(document={
        "alerts": {"asset_usd_threshold": "1000"},
        "sources": {"prices": {"adapter": "coingecko", "enabled": True}},
    })
    rows = [_token(USDC, 2_000 * 10**6, decimals=6)]
    apply_token_valuation(rows, public, transport, lambda: NOW)
    decision = AlertPolicy(Decimal("0.5"), Decimal("1000")).apply(rows)[0]
    assert decision.kind == "asset_usd"
    assert decision.alert is True
    assert decision.valuation_unknown is False
    assert rows[0].quote_usd == Decimal("2000")
    assert rows[0].quote_usd != 0


def test_priced_token_without_decimals_is_a_visible_unvalued_alert() -> None:
    class _Endpoints:
        def get(self, name: str) -> SimpleNamespace:
            return SimpleNamespace(endpoint="https://prices.example.invalid")

    class _Priced(_Transport):
        endpoints = _Endpoints()

    transport = _Priced({OTHER: {"usd": 2, "last_updated_at": NOW}})
    public = SimpleNamespace(document={
        "alerts": {"asset_usd_threshold": "1000"},
        "sources": {"prices": {"adapter": "coingecko", "enabled": True}},
    })
    rows = [_token(OTHER, 10**18, valuation_reason="decimals_unreadable")]
    apply_token_valuation(rows, public, transport, lambda: NOW)
    decision = AlertPolicy(Decimal("0.5"), Decimal("1000")).apply(rows)[0]
    assert decision.kind == "unvalued_large_move"
    assert decision.alert is True
    assert decision.valuation_unknown is True
    assert decision.reason == "decimals_unreadable"
    assert rows[0].quote_usd is None
    assert rows[0].decimals is None


def test_pending_replay_keeps_decimals_and_unvalued_large() -> None:
    original = _token(OTHER, 50, decimals=6)
    original = Candidate(
        tenant_id=original.tenant_id,
        wallet_id=original.wallet_id,
        network=original.network,
        capability=original.capability,
        identity=original.identity,
        kind=original.kind,
        raw_amount=original.raw_amount,
        is_native=False,
        asset=OTHER,
        decimals=6,
        unvalued_large=True,
        direction="in",
        tx_id=original.tx_id,
    )
    restored = candidate_from_snapshot(snapshot_candidate(original))
    assert restored.decimals == 6
    assert restored.unvalued_large is True
    assert restored.quote_usd is None
    older = snapshot_candidate(_token(OTHER, 1, decimals=None))
    del older["decimals"]
    del older["unvalued_large"]
    legacy = candidate_from_snapshot(older)
    assert legacy.decimals is None
    assert legacy.unvalued_large is False


def test_rpc_decimals_call_is_cached_per_contract() -> None:
    class _Rpc:
        def __init__(self) -> None:
            self.calls = 0

        def request(self, source_id: str, operation) -> SimpleNamespace:
            self.calls += 1
            assert operation.method == "eth_call"
            assert operation.params[0]["data"] == "0x313ce567"
            return SimpleNamespace(body={"result": "0x" + "0" * 63 + "6"}, ok=True)

    transport = _Rpc()
    rows = [
        {"asset": OTHER, "network": "ethereum-mainnet", "is_native": False, "raw_amount": 1},
        {"asset": "0x" + "AB" * 20, "network": "ethereum-mainnet", "is_native": False, "raw_amount": 2},
    ]
    cache: dict = {}
    fill_row_decimals(rows, transport, "rpc-ethereum-mainnet", cache)
    fill_row_decimals(rows, transport, "rpc-ethereum-mainnet", cache)
    assert transport.calls == 1
    assert rows[0]["decimals"] == 6
    assert rows[1]["decimals"] == 6

    class _Down(_Rpc):
        def request(self, source_id: str, operation) -> SimpleNamespace:
            self.calls += 1
            return SimpleNamespace(body={"error": {"code": -32000}}, ok=False)

    down = _Down()
    blank = [{"asset": OTHER, "network": "ethereum-mainnet", "is_native": False}]
    fill_row_decimals(blank, down, "rpc-ethereum-mainnet", {})
    assert blank[0].get("decimals") is None
    assert blank[0]["valuation_reason"] == "decimals_unreadable"


def test_token_raw_threshold_rejects_a_bad_key() -> None:
    with pytest.raises(ConfigError):
        validate_token_raw_thresholds({"not-a-network:0x" + "ab" * 20: "1"})
    with pytest.raises(ConfigError):
        validate_token_raw_thresholds({f"ethereum-mainnet:{USDC}": "0"})
