"""START-01. Explicit aliases use the registry id. State keys stay put."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

from guardian.configuration import load_accounts, load_public_config
from guardian.errors import ConfigError
from guardian.network_aliases import ALIASES, canonical_network, format_network_error
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


def _copy_monitor(path: Path) -> dict:
    document = json.loads((ROOT / "examples" / "monitor.nokeys.json").read_text(encoding="utf-8"))
    path.write_text(json.dumps(document), encoding="utf-8")
    return document


def _routes(document: dict, network: str, capability: str) -> list[dict]:
    return [
        route
        for route in document["routing"]
        if route.get("network") == network and route.get("capability") == capability
    ]


def _seed_baseline(database_path: Path) -> None:
    database = Database(database_path)
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
                "payload": {
                    "raw_amount": "8421285000000000000",
                    "coverage": {
                        "network": "arbitrum-one",
                        "capability": "balances.native",
                        "cursor": "100",
                        "raw_amount": "8421285000000000000",
                    },
                },
            }],
            cursors=[{
                "wallet_id": WALLET,
                "network": "arbitrum-one",
                "capability": "balances.native",
                "cursor": "100",
                "block_number": "100",
            }],
        )
    finally:
        database.close()


def _outside_checkout() -> Path:
    from guardian.configuration import path_inside_work_tree

    path = Path(tempfile.mkdtemp(prefix="wg-startfix-"))
    if path_inside_work_tree(path):
        shutil.rmtree(path, ignore_errors=True)
        raise RuntimeError("temporary directory is inside the git work tree")
    return path


def _cli(args: list[str], *, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "guardian.cli", *args],
        cwd=ROOT,
        env=env if env is not None else os.environ.copy(),
        check=False,
        capture_output=True,
        text=True,
    )


def test_start_fix_identical_routes_collapse_and_conflicts_are_refused(tmp_path: Path) -> None:
    """Same canonical route collapses. A different source list or order is an error."""
    identical = tmp_path / "identical.json"
    document = _copy_monitor(identical)
    document["routing"].append({
        "network": "arbitrum-mainnet",
        "capability": "balances.native",
        "source_ids": ["rpc-arbitrum-one"],
    })
    identical.write_text(json.dumps(document), encoding="utf-8")
    raw = identical.read_text(encoding="utf-8")
    loaded = load_public_config(identical)
    native = _routes(loaded.document, "arbitrum-one", "balances.native")
    assert len(native) == 1
    assert native[0]["source_ids"] == ["rpc-arbitrum-one"]
    assert "arbitrum-mainnet" in raw
    assert identical.read_text(encoding="utf-8") == json.dumps(document)

    conflict = tmp_path / "conflict.json"
    rejected = json.loads((ROOT / "examples" / "monitor.nokeys.json").read_text(encoding="utf-8"))
    rejected["routing"].append({
        "network": "arbitrum-mainnet",
        "capability": "balances.native",
        "source_ids": ["etherscan"],
    })
    conflict.write_text(json.dumps(rejected), encoding="utf-8")
    disk = conflict.read_bytes()
    database_path = tmp_path / "state" / "monitor.sqlite"
    _seed_baseline(database_path)
    database = Database(database_path)
    try:
        stored = database.capability_state_for(TENANT)
    finally:
        database.close()
    with pytest.raises(ConfigError, match="conflicting routes for arbitrum-one balances.native") as conflict_error:
        load_public_config(conflict)
    assert "source lists differ after alias normalization" in str(conflict_error.value)
    assert "etherscan" not in str(conflict_error.value)
    assert conflict.read_bytes() == disk
    database = Database(database_path)
    try:
        assert database.capability_state_for(TENANT) == stored
        assert database.capability_state_for(TENANT, network="arbitrum-mainnet") == []
    finally:
        database.close()

    reversed_sources = json.loads((ROOT / "examples" / "monitor.nokeys.json").read_text(encoding="utf-8"))
    reversed_sources["routing"].append({
        "network": "arbitrum-mainnet",
        "capability": "history.native_external",
        "source_ids": ["rpc-arbitrum-one", "blockscout-arbitrum-public", "etherscan"],
    })
    order_path = tmp_path / "order.json"
    order_path.write_text(json.dumps(reversed_sources), encoding="utf-8")
    with pytest.raises(ConfigError, match="source lists differ after alias normalization"):
        load_public_config(order_path)


def test_start_fix_cli_rejects_route_collision_without_rewriting_state(tmp_path: Path) -> None:
    document = json.loads((ROOT / "examples" / "monitor.nokeys.json").read_text(encoding="utf-8"))
    document["routing"].append({
        "network": "arbitrum-mainnet",
        "capability": "balances.native",
        "source_ids": ["etherscan"],
    })
    path = tmp_path / "monitor.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    disk = path.read_bytes()
    database_path = tmp_path / "state" / "monitor.sqlite"
    _seed_baseline(database_path)
    env = os.environ.copy()
    env["MONITOR_STATE_DIR"] = str(database_path.parent)
    completed = _cli(["config", "validate", "--mcm-config", str(path)], env=env)
    blob = completed.stdout + completed.stderr
    assert completed.returncode == 2
    payload = json.loads(completed.stdout)
    assert payload["status"] == "error"
    assert payload["error_type"] == "ConfigError"
    assert "conflicting routes for arbitrum-one balances.native" in payload["error"]
    assert "source lists differ after alias normalization" in payload["error"]
    assert path.read_bytes() == disk
    database = Database(database_path)
    try:
        stored = database.capability_state_for(TENANT, network="arbitrum-one")
        assert len(stored) == 1
        assert stored[0]["cursor"] == "100"
        assert stored[0]["payload"]["raw_amount"] == "8421285000000000000"
        assert database.capability_state_for(TENANT, network="arbitrum-mainnet") == []
    finally:
        database.close()
    assert "8421285000000000000" not in blob


def test_start_fix_cli_reuses_canonical_baseline_for_alias(tmp_path: Path) -> None:
    """Typing the alias keeps the registry id, so the existing baseline is that row."""
    outside = _outside_checkout()
    try:
        document = json.loads((ROOT / "examples" / "monitor.nokeys.json").read_text(encoding="utf-8"))
        document["enabled_networks"] = [
            "arbitrum-mainnet" if item == "arbitrum-one" else item
            for item in document["enabled_networks"]
        ]
        for route in document["routing"]:
            if route.get("network") == "arbitrum-one":
                route["network"] = "arbitrum-mainnet"
        monitor = outside / "monitor.json"
        monitor.write_text(json.dumps(document), encoding="utf-8")
        assert "arbitrum-mainnet" in monitor.read_text(encoding="utf-8")
        accounts = outside / "accounts.json"
        accounts.write_text(json.dumps({
            "schema_version": "guardian.accounts.v1",
            "tenant_id": TENANT,
            "accounts": [],
        }), encoding="utf-8")
        secret = outside / "secrets.json"
        secret.write_text("{}", encoding="utf-8")
        accounts.chmod(0o600)
        secret.chmod(0o600)
        state = outside / "state"
        state.mkdir()
        database_path = state / "monitor.sqlite"
        _seed_baseline(database_path)
        env = os.environ.copy()
        env["MONITOR_ACCOUNTS_FILE"] = str(accounts)
        env["MONITOR_SECRET_PROFILE"] = str(secret)
        env["MONITOR_STATE_DIR"] = str(state)
        env["GUARDIAN_STATE_DIR"] = str(state)
        coverage = _cli(["coverage", "--mcm-config", str(monitor)], env=env)
        assert coverage.returncode == 0, coverage.stdout + coverage.stderr
        payload = json.loads(coverage.stdout)
        assert payload["status"] == "ok"
        row = next(item for item in payload["rows"] if item.get("capability") == "balances.native")
        assert row["network"] == "arbitrum-one"
        assert row["cursor"] == "100"
        assert row["raw_amount"] == "8421285000000000000"
        assert "arbitrum-mainnet" not in coverage.stdout
        exported = _cli(["export", "--public", "--mcm-config", str(monitor)], env=env)
        assert exported.returncode == 0, exported.stderr
        public = json.loads(exported.stdout)
        assert "arbitrum-one" in public["enabled_networks"]
        assert "arbitrum-mainnet" not in public["enabled_networks"]
        assert "arbitrum-mainnet" in monitor.read_text(encoding="utf-8")
        database = Database(database_path)
        try:
            stored = database.capability_state_for(TENANT, network="arbitrum-one")
            assert len(stored) == 1
            assert stored[0]["cursor"] == "100"
            assert stored[0]["payload"]["raw_amount"] == "8421285000000000000"
            assert database.capability_state_for(TENANT, network="arbitrum-mainnet") == []
        finally:
            database.close()
    finally:
        shutil.rmtree(outside, ignore_errors=True)


def test_start_fix_network_errors_do_not_echo_urls_or_addresses(tmp_path: Path) -> None:
    """Keyed URLs and 0x addresses stay out of library errors and the CLI redactor."""
    registry = _registry()
    secret = "synthetic-path-key-STARTFIX"
    url = f"https://rpc.example.invalid/v2/{secret}"
    address = "0x00000000000000000000000000000000000000ab"
    shaped = "0xdeadbeefcafebabe"
    for name in (url, address, shaped):
        text = format_network_error(name, registry)
        assert name not in text
        assert secret not in text
        assert address not in text
        assert shaped not in text
        assert "example.invalid" not in text
        assert "<invalid name>" in text
    for name in (url, shaped):
        path = tmp_path / "bad.json"
        path.write_text(json.dumps({"enabled_networks": [name]}), encoding="utf-8")
        with pytest.raises(ConfigError) as caught:
            load_public_config(path)
        assert name not in str(caught.value)
        assert secret not in str(caught.value)
        assert "<invalid name>" in str(caught.value)
        completed = _cli(["config", "validate", "--mcm-config", str(path)])
        blob = completed.stdout + completed.stderr
        assert completed.returncode == 2
        payload = json.loads(completed.stdout)
        assert payload["error_type"] == "ConfigError"
        assert name not in blob
        assert secret not in blob
        assert address not in blob
        assert shaped not in blob
        assert "example.invalid" not in blob
        assert "<invalid name>" in payload["error"]
    leaked = tmp_path / "address.json"
    leaked.write_text(json.dumps({"enabled_networks": [address]}), encoding="utf-8")
    completed = _cli(["config", "validate", "--mcm-config", str(leaked)])
    blob = completed.stdout + completed.stderr
    assert completed.returncode == 2
    assert address not in blob
    assert "00000000000000000000000000000000000000ab" not in blob
