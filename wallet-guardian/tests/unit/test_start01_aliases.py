"""START-01. Explicit aliases use the registry id. State keys stay put."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from guardian.configuration import load_accounts, load_public_config
from guardian.errors import ConfigError
from guardian.network_aliases import ALIASES, canonical_network
from guardian.network_registry import load_registry
from guardian.onboarding import _build, _normalize_answers
from guardian.resources import resource_path
from guardian.storage import Database

ROOT = Path(__file__).resolve().parents[2]
WALLET = "00000000-0000-4000-8000-0000000000a1"
ADDRESS = "0x0000000000000000000000000000000000000001"
TENANT = "local"


def _registry():
    return load_registry(resource_path("config", "chains"))


def _aliased_monitor(path: Path) -> Path:
    document = json.loads((ROOT / "examples" / "monitor.nokeys.json").read_text(encoding="utf-8"))
    document["enabled_networks"] = [_swap(item) for item in document["enabled_networks"]]
    document["enabled_networks"].append("arbitrum-one")
    for route in document["routing"]:
        route["network"] = _swap(route["network"])
    path.write_text(json.dumps(document), encoding="utf-8")
    return path


def _swap(name: str) -> str:
    if name == "arbitrum-one":
        return "arbitrum-mainnet"
    if name == "polygon-pos":
        return "polygon-mainnet"
    return name


def test_start01_alias_normalization_preserves_state_identity(tmp_path: Path) -> None:
    """Both spellings are the registry id. The stored baseline row is not replaced."""
    registry = _registry()
    assert canonical_network("arbitrum-mainnet", registry) == "arbitrum-one"
    assert canonical_network("arbitrum-one", registry) == "arbitrum-one"
    assert canonical_network("polygon-mainnet", registry) == "polygon-pos"
    assert canonical_network("polygon-pos", registry) == "polygon-pos"
    assert ALIASES == {
        "arbitrum-mainnet": "arbitrum-one",
        "polygon-mainnet": "polygon-pos",
    }

    database = Database(tmp_path / "monitor.sqlite")
    try:
        database.commit_capability_cycle(
            TENANT,
            events=[],
            capability_rows=[{
                "wallet_id": WALLET,
                "network": "arbitrum-one",
                "capability": "balances.native",
                "quality": "partial",
                "cursor": "100",
                "payload": {"raw_amount": "8421285000000000000"},
            }],
            cursors=[{
                "wallet_id": WALLET,
                "network": "arbitrum-one",
                "capability": "balances.native",
                "cursor": "100",
                "block_number": "100",
            }],
        )
        before = database.capability_state_for(TENANT)
        source = _aliased_monitor(tmp_path / "monitor.json")
        raw = source.read_text(encoding="utf-8")
        assert "arbitrum-mainnet" in raw
        loaded = load_public_config(source)
        assert "arbitrum-mainnet" not in loaded.document["enabled_networks"]
        assert "polygon-mainnet" not in loaded.document["enabled_networks"]
        assert loaded.document["enabled_networks"].count("arbitrum-one") == 1
        assert "polygon-pos" in loaded.document["enabled_networks"]
        assert all(route["network"] != "arbitrum-mainnet" for route in loaded.document["routing"])
        assert any(route["network"] == "arbitrum-one" for route in loaded.document["routing"])
        assert "arbitrum-mainnet" in source.read_text(encoding="utf-8")

        accounts_path = tmp_path / "accounts.json"
        accounts_path.write_text(json.dumps({
            "schema_version": "guardian.accounts.v1",
            "tenant_id": TENANT,
            "accounts": [{
                "wallet_id": WALLET,
                "label": "synthetic-desk",
                "family": "evm",
                "address": ADDRESS,
                "networks": ["arbitrum-mainnet", "arbitrum-one", "polygon-mainnet"],
                "relationship": "watch_only",
            }],
        }), encoding="utf-8")
        accounts = load_accounts(accounts_path, registry=registry)
        assert accounts.accounts[0].networks == ("arbitrum-one", "polygon-pos")
        enabled = [
            (account.wallet_id, network)
            for account in accounts.accounts
            for network in account.networks
            if network in set(loaded.document["enabled_networks"])
        ]
        assert (WALLET, "arbitrum-one") in enabled
        assert (WALLET, "arbitrum-mainnet") not in enabled
        stored = database.capability_state_for(TENANT, network="arbitrum-one")
        assert len(stored) == 1
        assert stored[0]["payload"]["raw_amount"] == "8421285000000000000"
        assert stored[0]["cursor"] == "100"
        assert database.capability_state_for(TENANT, network="arbitrum-mainnet") == []
        assert database.capability_state_for(TENANT) == before
    finally:
        database.close()


def test_start01_unknown_and_ambiguous_names_are_not_guessed(tmp_path: Path) -> None:
    registry = _registry()
    valid = ", ".join(sorted(registry))
    ambiguous = tmp_path / "ambiguous.json"
    ambiguous.write_text(json.dumps({
        "schema_version": "guardian.monitor.v1",
        "enabled_networks": ["polygon"],
    }), encoding="utf-8")
    with pytest.raises(ConfigError, match="ambiguous network") as ambiguous_error:
        load_public_config(ambiguous)
    text = str(ambiguous_error.value)
    assert "polygon-pos" in text
    assert "polygon-mainnet" in text
    assert valid in text
    assert "suggestions:" in text

    unknown = tmp_path / "unknown.json"
    unknown.write_text(json.dumps({
        "schema_version": "guardian.monitor.v1",
        "enabled_networks": ["zzzz-not-a-chain"],
    }), encoding="utf-8")
    with pytest.raises(ConfigError, match="unknown network") as unknown_error:
        load_public_config(unknown)
    unknown_text = str(unknown_error.value)
    assert "ethereum-mainnet" in unknown_text
    assert "suggestions: none" in unknown_text
    assert "zzzz-not-a-chain" in unknown_text

    with pytest.raises(ConfigError, match="unknown network") as ethereum_error:
        canonical_network("ethereum", registry)
    assert "ethereum-mainnet" in str(ethereum_error.value)
    assert canonical_network("ethereum-mainnet", registry) == "ethereum-mainnet"


def test_start01_onboarding_writes_canonical_ids() -> None:
    answers = _normalize_answers({
        "accounts": [{
            "address": ADDRESS,
            "family": "evm",
            "label": "synthetic-desk",
            "networks": ["arbitrum-mainnet", "polygon-pos"],
            "relationship": "watch_only",
        }],
        "consent": True,
        "key_mode": "none",
        "plugins": [],
        "tenant_id": TENANT,
    })
    plan = _build(answers, recorded_at="2026-10-10T00:00:00Z")
    assert plan["monitor"]["enabled_networks"] == ["arbitrum-one", "polygon-pos"]
    assert plan["accounts"]["accounts"][0]["networks"] == ["arbitrum-one", "polygon-pos"]
    assert all(route["network"] in {"arbitrum-one", "polygon-pos"} for route in plan["monitor"]["routing"])
    assert all(route["network"] != "arbitrum-mainnet" for route in plan["monitor"]["routing"])
