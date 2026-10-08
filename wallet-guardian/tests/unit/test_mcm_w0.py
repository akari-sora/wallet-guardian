"""Offline acceptance for MCM-W0. No network, no private state."""

from __future__ import annotations

import argparse
import ast
import importlib
import json
import re
import shutil
from pathlib import Path

import pytest

from guardian.cli import build_parser
from guardian.errors import SecurityError
from guardian.network_registry import PACKAGE_STATUS, RegistryError, load_registry
from guardian.security.rpc_guard import FORBIDDEN_RPC_METHODS, assert_agent_method
from guardian.storage import Database

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src" / "guardian"
CHAINS = ROOT / "config" / "chains"
MAP = ROOT / "docs" / "legacy-monitor-map.md"
CONTRACT = ROOT / "docs" / "mcm-01.md"

ENTRYPOINTS = [
    "guardian.cli:build_parser",
    "guardian.cli:dispatch",
    "guardian.cli:main",
    "guardian.service:run_once",
    "guardian.service:_run_once",
    "guardian.service:doctor",
    "guardian.service:provider_probes",
    "guardian.service:etherscan_chainlist_probe",
    "guardian.service:screen_ofac",
    "guardian.service:operation_coverage",
    "guardian.service:self_check",
    "guardian.service:_hyperliquid",
    "guardian.monitoring:SnapshotCycle",
    "guardian.monitoring:reconcile_scopes",
    "guardian.providers:RpcClient",
    "guardian.providers:read_lista_state",
    "guardian.providers:safe_get_text",
    "guardian.providers:safe_post_json",
    "guardian.security.rpc_guard:assert_agent_method",
    "guardian.security.ssrf:validate_url",
    "guardian.security.ssrf:next_redirect",
    "guardian.storage:Database",
    "guardian.domain:normalize_hyperliquid",
    "guardian.network_registry:load_registry",
]
CAPABILITIES = [
    "balances.native",
    "balances.fungible",
    "balances.nft",
    "history.native_external",
    "history.native_internal",
    "history.fungible",
    "history.nft",
    "permissions.erc20",
    "permissions.erc721_token",
    "permissions.erc721_operator",
    "permissions.erc1155_operator",
    "permits.erc2612_observed",
    "permits.dai_variant",
    "permit2.allowance",
    "permit2.signature_usage",
    "permissions.spl_delegate",
    "permissions.spl_authority",
    "permissions.tron_account",
    "permissions.ton_extensions",
    "permissions.move_capability",
    "permissions.cosmos_authz",
    "permissions.cosmos_feegrant",
    "positions",
    "withdrawals",
    "bridges",
    "asset_risk",
    "address_risk",
    "sanctions",
    "exposure_news",
    "permits.offchain_unused",
]
T0 = {
    "ethereum-mainnet",
    "bsc-mainnet",
    "arbitrum-one",
    "base-mainnet",
    "optimism-mainnet",
    "polygon-pos",
    "avalanche-c",
    "solana-mainnet",
}
T0_EVM_CHAIN_IDS = {
    "ethereum-mainnet": 1,
    "bsc-mainnet": 56,
    "arbitrum-one": 42161,
    "base-mainnet": 8453,
    "optimism-mainnet": 10,
    "polygon-pos": 137,
    "avalanche-c": 43114,
}
MONITOR_NAMES = {
    "monitoring.py",
    "providers.py",
    "storage.py",
    "network_registry.py",
}
_ADDRESS = re.compile(r"(?<![0-9A-Fa-f])0x[0-9A-Fa-f]{40}(?![0-9A-Fa-f])")
_SYNTHETIC = re.compile(r"0x0{39}[0-9A-Fa-f]$", re.IGNORECASE)
_NODEREAL_KEY = re.compile(r"nodereal\.io/v1/[A-Za-z0-9]", re.IGNORECASE)
_SPEC_FORBIDDEN = {
    "eth_sendTransaction",
    "eth_sendRawTransaction",
    "wallet_sendCalls",
    "eth_sign",
    "personal_sign",
}
_STDLIB = {"__future__", "json", "re", "pathlib", "typing"}


def _is_forbidden(value: str) -> bool:
    return value in _SPEC_FORBIDDEN or value in FORBIDDEN_RPC_METHODS or value.startswith("eth_signTypedData")


def _imports_actions(tree: ast.AST) -> bool:
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if "actions" in alias.name.split("."):
                    return True
        elif isinstance(node, ast.ImportFrom) and node.module and "actions" in node.module.split("."):
            return True
    return False


def _forbidden_uses(tree: ast.AST, *, allow_deny_list: bool) -> list[str]:
    skipped: set[int] = set()
    if allow_deny_list:
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign) and any(
                isinstance(target, ast.Name) and target.id == "FORBIDDEN_RPC_METHODS" for target in node.targets
            ):
                for child in ast.walk(node):
                    skipped.add(id(child))
    found: list[str] = []
    for node in ast.walk(tree):
        if id(node) in skipped:
            continue
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and _is_forbidden(node.value):
            found.append(node.value)
        if isinstance(node, ast.Call):
            func = node.func
            name = func.id if isinstance(func, ast.Name) else func.attr if isinstance(func, ast.Attribute) else ""
            if _is_forbidden(name):
                found.append(name)
    return found


def _secret_problems(text: str) -> list[str]:
    problems = []
    for match in _ADDRESS.finditer(text):
        if _SYNTHETIC.fullmatch(match.group(0)) is None:
            problems.append(match.group(0))
    lowered = text.lower()
    if "glpat-" in lowered:
        problems.append("glpat-")
    if "apikey=" in lowered:
        problems.append("apikey=")
    if _NODEREAL_KEY.search(text):
        problems.append("nodereal-key-url")
    return problems


def _public_texts() -> list[tuple[str, str]]:
    rows = [(str(MAP.relative_to(ROOT)), MAP.read_text(encoding="utf-8"))]
    rows.append((str(CONTRACT.relative_to(ROOT)), CONTRACT.read_text(encoding="utf-8")))
    for path in sorted(CHAINS.glob("*.json")):
        rows.append((str(path.relative_to(ROOT)), path.read_text(encoding="utf-8")))
    plan_dir = ROOT / "docs" / "mcm-01"
    for path in sorted(plan_dir.glob("*.json")):
        rows.append((str(path.relative_to(ROOT)), path.read_text(encoding="utf-8")))
    return rows


def _copy_registry(tmp_path: Path) -> Path:
    dest = tmp_path / "chains"
    dest.mkdir(parents=True)
    for src in CHAINS.glob("*.json"):
        shutil.copy(src, dest / src.name)
    return dest


def _rewrite(path: Path, mutate) -> None:
    payload = json.loads(path.read_text(encoding="utf-8"))
    mutate(payload)
    path.write_text(json.dumps(payload), encoding="utf-8")


def test_mcm_w0_01_map_public_private_entries_and_tables(tmp_path):
    text = MAP.read_text(encoding="utf-8")
    assert "MCM-W0-01" in text
    assert "操作者機器" in text
    for token in (
        "alerted",
        "latest_seen",
        "nonces",
        "bsc_last_block",
        "nr_getAssetTransfers",
        "txlistinternal",
        "not_observable",
        "分頁不是完成",
        "PRAGMA user_version=1",
    ):
        assert token in text

    for spec in ENTRYPOINTS:
        assert f"`{spec}`" in text
        module_name, attr = spec.split(":")
        module = importlib.import_module(module_name)
        assert hasattr(module, attr)

    commands = []
    for action in build_parser()._actions:
        if isinstance(action, argparse._SubParsersAction):
            commands.extend(action.choices)
    assert "run-once" in commands and "coverage" in commands and "replay" in commands
    for name in commands:
        assert f"`{name}`" in text

    database = Database(tmp_path / "w0.sqlite")
    try:
        names = database.table_names()
        version = database.conn.execute("PRAGMA user_version").fetchone()[0]
    finally:
        database.close()
    assert version == 1
    assert "snapshots" in names and "alert_dedup" in names and "cursors" in names
    for name in names:
        assert f"`{name}`" in text

    contract = CONTRACT.read_text(encoding="utf-8")
    assert "MULTICHAIN-MONITOR-01" in contract
    assert "d6331f02b1a1fcdb75f7c265a29d484056a42515" in contract
    assert "沒有任何鏈在本包被 live 驗證" in contract
    assert "shadow" in contract
    packages = json.loads((ROOT / "docs" / "mcm-01" / "work-packages.plan.json").read_text(encoding="utf-8"))
    assert packages["package_id"] == "MULTICHAIN-MONITOR-01"
    assert packages["status"] == "planning_only"
    assert [item["id"] for item in packages["packages"]] == [f"MCM-W{index}" for index in range(18)]
    for item in packages["packages"]:
        assert item["id"] in contract
        assert item["branch"] in contract
        for case in item["acceptance"]:
            assert case["test_id"] in contract

    probes = set()
    for line in text.splitlines():
        if not line.startswith("| ") or line.startswith("| network") or line.startswith("|---"):
            continue
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if len(cells) < 6 or cells[1] not in CAPABILITIES:
            continue
        probes.add(cells[3])
        assert cells[2] in {"implemented", "partial", "unsupported", "not_applicable", "not_observable"}
        assert cells[3] in {"documented", "untested", "blocked", "live_fail"}
        assert cells[4] in {"partial", "stale", "error", "unknown"}
        assert cells[5] in {"observed", "unknown"}
    assert probes == {"documented", "untested", "blocked"}
    assert text.count("| balances.native |") == 48
    for cap in CAPABILITIES:
        assert text.count(f"| {cap} |") == 48
    assert "complete_in_declared_scope" not in text
    assert "| live_pass |" not in text


def test_mcm_w0_01_map_public_private_no_secrets():
    for label, text in _public_texts():
        assert _secret_problems(text) == [], label
    legacy = MAP.read_text(encoding="utf-8")
    assert "0x0000000000000000000000000000000000000001" in legacy


def test_mcm_w0_02_readonly_boundary_monitoring_modules():
    bad = ast.parse('def f(rpc):\n    rpc.call("eth_sendTransaction", [])\n')
    assert _forbidden_uses(bad, allow_deny_list=False) == ["eth_sendTransaction"]
    deny = ast.parse('FORBIDDEN_RPC_METHODS = frozenset({"eth_sendTransaction", "eth_signTypedData_v4"})\n')
    assert _forbidden_uses(deny, allow_deny_list=True) == []

    parsed = []
    for path in sorted(SRC.rglob("*.py")):
        relative = path.relative_to(SRC)
        if "actions" in relative.parts or path.name in {"service.py", "cli.py"}:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        parsed.append(relative)
        # Every non-action, non-wiring module is on the monitor side, including the
        # MCM modules added by later packages (sources/, configuration, identities ...).
        assert not _imports_actions(tree), relative
        assert _forbidden_uses(tree, allow_deny_list=path.name == "rpc_guard.py") == [], relative

    parsed_names = {path.name for path in parsed}
    assert {"domain.py", "amounts.py", "network_registry.py", "rpc_guard.py"} <= parsed_names
    assert MONITOR_NAMES <= parsed_names
    assert "service.py" not in parsed_names and "cli.py" not in parsed_names
    assert _imports_actions(ast.parse((SRC / "service.py").read_text(encoding="utf-8")))
    assert _imports_actions(ast.parse((SRC / "cli.py").read_text(encoding="utf-8")))

    registry_tree = ast.parse((SRC / "network_registry.py").read_text(encoding="utf-8"))
    for node in ast.walk(registry_tree):
        if isinstance(node, ast.Import):
            modules = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            modules = [node.module or ""]
        else:
            continue
        for module_name in modules:
            assert module_name.split(".")[0] in _STDLIB

    for method in sorted(FORBIDDEN_RPC_METHODS | _SPEC_FORBIDDEN):
        with pytest.raises(SecurityError):
            assert_agent_method(method)
    assert_agent_method("eth_chainId")
    assert_agent_method("eth_getBalance")
    assert_agent_method("eth_call")


def test_mcm_w0_02_readonly_boundary_send_hard_off():
    env_lines = [
        line.strip()
        for line in (ROOT / "config" / "env.example").read_text(encoding="utf-8").splitlines()
        if line.startswith("GUARDIAN_MAINNET_SEND=")
    ]
    assert env_lines == ["GUARDIAN_MAINNET_SEND=false"]

    source = (ROOT / "action-client" / "src" / "main.ts").read_text(encoding="utf-8")
    start = source.index("function requestUserSend")
    body = source[start:source.index("\nfunction ", start)]
    assert "if (!MAINNET_SEND)" in body
    marker = 'throw new Error("獨立重讀不是送出授權。允許實際送出保持關閉。不會呼叫錢包。");'
    assert marker in body
    assert body.index(marker) > body.index("if (!MAINNET_SEND)")


def test_mcm_w0_03_network_registry_loads_planned_set():
    plan = json.loads((ROOT / "docs" / "mcm-01" / "network-tiers.plan.json").read_text(encoding="utf-8"))
    registry = load_registry(CHAINS)
    assert list(registry) == [row["network"] for row in plan]
    assert len(registry) == 24
    assert {network for network, entry in registry.items() if entry["tier"] == 0} == T0
    assert len(list(CHAINS.glob("*.json"))) == 24
    policies = set()
    for network, entry in registry.items():
        assert entry["enabled"] is False
        assert entry["status"] == PACKAGE_STATUS == "planned_not_live_verified"
        assert (CHAINS / f"{network}.json").is_file()
        policies.add(entry["finality_policy"])
        if entry["family"] == "evm":
            assert isinstance(entry["evm_chain_id"], int)
            assert entry["identity"]["expected_value"] == hex(entry["evm_chain_id"])
            assert entry["native_asset"]["identity"] == f"evm-native:{entry['evm_chain_id']}"
        else:
            assert entry["evm_chain_id"] is None
    assert len(policies) == 24
    for network, chain_id in T0_EVM_CHAIN_IDS.items():
        assert registry[network]["family"] == "evm"
        assert registry[network]["evm_chain_id"] == chain_id
    assert registry["solana-mainnet"]["native_asset"]["identity"] == "solana-native:SOL"
    assert registry["solana-mainnet"]["identity"]["expected_value"] == "5eykt4UsFv8P8NJdTREpY1vzqKqZKvdpKuc147dw2N9d"
    assert registry["bitcoin-mainnet"]["identity"]["expected_value"] == (
        "000000000019d6689c085ae165831e934ff763ae46a2a6c172b3f1b60a8ce26f"
    )
    assert registry["sui-mainnet"]["identity"]["expected_value"] == "35834a8a"
    assert "GraphQL" in registry["sui-mainnet"]["notes"] and "gRPC" in registry["sui-mainnet"]["notes"]
    assert registry["tron-mainnet"]["identity"]["expected_value"] is None
    assert registry["tron-mainnet"]["identity"]["status"] == "to_verify_by_probe"
    assert registry["ton-mainnet"]["identity"]["expected_value"] is None
    assert registry["aptos-mainnet"]["identity"]["expected_value"] == "1"
    assert registry["cosmos-hub"]["identity"]["expected_value"] == "cosmoshub-4"
    assert registry["osmosis-mainnet"]["identity"]["expected_value"] == "osmosis-1"
    assert "2026-10-12" in registry["opbnb-mainnet"]["notes"]
    assert "Etherscan" in registry["opbnb-mainnet"]["notes"]
    assert "HyperCore" in registry["hyperevm-mainnet"]["notes"]

    legacy = json.loads((ROOT / "config" / "chains.json").read_text(encoding="utf-8"))
    assert [row["chain_id"] for row in legacy["enabled"]] == [56]
    assert legacy["enabled"][0]["native_symbol"] == "BNB"


def test_mcm_w0_03_network_registry_rejects_bad_entries(tmp_path):
    def expect(name: str, mutate, match: str) -> None:
        dest = _copy_registry(tmp_path / name)
        mutate(dest)
        with pytest.raises(RegistryError, match=match):
            load_registry(dest)

    def enable(dest: Path) -> None:
        _rewrite(dest / "ethereum-mainnet.json", lambda row: row.update(enabled=True))

    def duplicate(dest: Path) -> None:
        def mutate(row: dict) -> None:
            row["evm_chain_id"] = 1
            row["identity"]["evm_chain_id"] = 1
            row["native_asset"]["identity"] = "evm-native:1"

        _rewrite(dest / "bsc-mainnet.json", mutate)

    def missing_chain(dest: Path) -> None:
        def mutate(row: dict) -> None:
            row["evm_chain_id"] = None
            row["identity"]["evm_chain_id"] = None

        _rewrite(dest / "ethereum-mainnet.json", mutate)

    def non_evm_chain(dest: Path) -> None:
        def mutate(row: dict) -> None:
            row["evm_chain_id"] = 99999
            row["identity"]["evm_chain_id"] = 99999

        _rewrite(dest / "solana-mainnet.json", mutate)

    def unknown_family(dest: Path) -> None:
        _rewrite(dest / "solana-mainnet.json", lambda row: row.update(family="cardano"))

    def outside_plan(dest: Path) -> None:
        payload = json.loads((dest / "ethereum-mainnet.json").read_text(encoding="utf-8"))
        payload["network"] = "xrp-mainnet"
        payload["display_name"] = "Synthetic extra"
        payload["evm_chain_id"] = 999999
        payload["identity"]["evm_chain_id"] = 999999
        payload["native_asset"]["identity"] = "evm-native:999999"
        payload["enabled"] = False
        (dest / "xrp-mainnet.json").write_text(json.dumps(payload), encoding="utf-8")

    expect("enabled", enable, "enabled must be false")
    expect("duplicate", duplicate, "duplicate evm_chain_id")
    expect("missing", missing_chain, "EVM family is missing evm_chain_id")
    expect("non-evm", non_evm_chain, "non-EVM family must not set evm_chain_id")
    expect("family", unknown_family, "unknown family")
    expect("extra", outside_plan, "is not enabled")
