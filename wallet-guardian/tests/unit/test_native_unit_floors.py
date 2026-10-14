"""Init writes native-unit floors. An unjudged movement does not move the baseline."""

from __future__ import annotations

from guardian.onboarding import _build


def _answers(networks: list[str]) -> dict:
    return {
        "accounts": [
            {
                "address": "0x" + ("0" * 39) + "1",
                "family": "evm",
                "label": "synthetic-desk",
                "networks": networks,
                "relationship": "watch_only",
            }
        ],
        "consent": True,
        "credential_envs": {},
        "key_mode": "none",
        "plugins": [],
        "tenant_id": "local",
    }


def test_init_writes_floors_only_for_enabled_non_eth_networks() -> None:
    eth = _build(_answers(["ethereum-mainnet"]), recorded_at="2026-10-14T00:00:00Z")
    assert "native_unit_thresholds" not in eth["monitor"]["alerts"]
    mixed = _build(
        _answers(["ethereum-mainnet", "bsc-mainnet", "polygon-pos", "avalanche-c"]),
        recorded_at="2026-10-14T00:00:00Z",
    )
    floors = mixed["monitor"]["alerts"]["native_unit_thresholds"]
    assert floors == {
        "avalanche-c": "10",
        "bsc-mainnet": "1.0",
        "polygon-pos": "1000",
    }
    assert "ethereum-mainnet" not in floors
    solana = _build(
        _answers(["solana-mainnet", "bitcoin-mainnet"]),
        recorded_at="2026-10-14T00:00:00Z",
    )
    solana_floors = solana["monitor"]["alerts"]["native_unit_thresholds"]
    assert "solana-mainnet" not in solana_floors
    assert solana_floors["bitcoin-mainnet"] == "0.01"
