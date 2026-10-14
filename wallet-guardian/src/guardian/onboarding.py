"""Single-operator onboarding. Writes config. Does not scan, sign, or send.

``guardian init`` asks which addresses, chains, and plugins to watch, then
which env var names will hold endpoints and keys. It records data-sharing
consent and writes files the operator already agreed to. It does not derive
addresses, enable GoPlus or Chainabuse, install a service, or start a sender.

A tenant id is a label for this operator. It is not isolation between
mutually untrusted customers. Keys are never copied into a file.
"""

from __future__ import annotations

import json
import re
import tempfile
from collections.abc import Callable, Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from guardian.configuration import git_work_tree, load_accounts, load_public_config, path_inside_work_tree
from guardian.errors import ConfigError
from guardian.identities import IdentityError, parse_address
from guardian.locations import config_dir, ensure_private_dir, secure_write_text, state_dir
from guardian.network_aliases import canonical_network
from guardian.network_registry import load_registry
from guardian.resources import resource_path

_ENV_NAME = re.compile(r"^[A-Z][A-Z0-9_]{1,95}$")
_TENANT = re.compile(r"^[a-z][a-z0-9_-]{0,95}$")
_SOURCE_ID = re.compile(r"^[a-z][a-z0-9_-]{0,95}$")
_OWN_GROUP = re.compile(r"^[a-z][a-z0-9_-]{0,95}$")
_UUID = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)
_ANSWER_FIELDS = frozenset({
    "accounts",
    "consent",
    "credential_envs",
    "key_mode",
    "plugins",
    "tenant_id",
})
_ACCOUNT_FIELDS = frozenset({
    "address",
    "family",
    "label",
    "networks",
    "own_group",
    "relationship",
    "wallet_id",
})
_PLUGINS = frozenset({
    "aave-v3",
    "aave_v3",
    "compound-v3",
    "compound_v3",
    "erc4626",
    "erc4626-pins",
    "erc4626-registered",
    "erc4626_pins",
    "kinza",
    "lido",
    "lista-slisbnb",
    "lista_slisbnb",
    "solana-stake",
    "solana_stake",
    "uxlink-holdings",
    "uxlink_holdings",
})
_RELATIONSHIPS = frozenset({"owned_user_declared", "watch_only"})
_FAMILIES = frozenset({"aptos", "bitcoin", "cosmos", "evm", "solana", "sui", "ton", "tron"})
# Operator-unit floors for non-ETH networks init can enable. Not a USD equivalent.
# Solana is omitted on purpose. run-once does not parse Solana transactions
# and native balance deltas are EVM-only, so a SOL floor would not alert.
_NATIVE_UNIT_FLOORS = {
    "avalanche-c": "10",
    "bitcoin-mainnet": "0.01",
    "bsc-mainnet": "1.0",
    "polygon-pos": "1000",
}
_READ_ADAPTER = {
    "aptos": "aptos_rest",
    "bitcoin": "esplora",
    "cosmos": "cosmos_rest",
    "solana": "solana_rpc",
    "sui": "sui_graphql",
    "ton": "toncenter_v3",
    "tron": "tron_grid",
}
_FILE_NAMES = (
    "accounts.json",
    "delivery-profile.json",
    "env-paths.json",
    "monitor.json",
    "secret-profile.json",
)
_ACCOUNTS_ENV = "MONITOR_ACCOUNTS_FILE"
_STATE_ENV = "MONITOR_STATE_DIR"
_SECRET_ENV = "MONITOR_SECRET_PROFILE"
_DELIVERY_ENV = "MONITOR_DELIVERY_PROFILE"
_CONFIG_ENV = "GUARDIAN_MONITOR_CONFIG"
_SENSITIVE_PARTS = ("KEY", "MNEMONIC", "PASSWORD", "PRIVATE", "SECRET", "SEED", "TOKEN")
_PATH_PREFIXES = ("GUARDIAN_", "MONITOR_", "XDG_")


def execute_init(
    *,
    directory: str | None,
    answers_path: str | None,
    yes: bool,
    force: bool,
    environ: Mapping[str, str],
    prompt: Callable[[str], str] = input,
) -> tuple[dict[str, Any], int]:
    """Build a profile. Write it only after consent, and never over a state database."""
    answers = _load_answers(answers_path, prompt)
    root, state = _directories(directory)
    _refuse_work_tree(root)
    _refuse_work_tree(state)
    recorded_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    plan = _build(answers, recorded_at=recorded_at)
    texts = _render(plan, root, state)
    _refuse_secret_leaks(texts, environ, root, state)
    _validate_in_memory(texts["monitor.json"], texts["accounts.json"])
    summary = _summary(plan, texts, root, state, wrote=False, backups=[])
    if not answers_path and not yes:
        preview = "\n".join(
            f"{row['source_id']}: sends_addresses={row['sends_addresses']} to {row['recipient']}. {row['data']}"
            for row in plan["sharing"]
        )
        try:
            typed = prompt(preview + "\nType yes to record data-sharing consent and write config: ").strip()
        except EOFError:
            raise ConfigError("init needs an answer") from None
        answers["consent"] = typed == "yes"
        yes = typed == "yes"
    if answers.get("consent") is False or not yes:
        summary["status"] = "consent_required"
        summary["wrote"] = False
        return summary, 2
    existing = [name for name in _FILE_NAMES if (root / name).is_file()]
    if existing and not force:
        summary["status"] = "refused"
        summary["wrote"] = False
        summary["existing"] = existing
        summary["error"] = "existing config was not overwritten; pass --force to back up and write new files"
        return summary, 2
    backups = _backup(root, existing) if existing else []
    _write_all(root, state, texts)
    written = _summary(plan, texts, root, state, wrote=True, backups=backups)
    written["status"] = "ok"
    return written, 0


def install_service_plan(*, dry_run: bool, mcm_config: str | None) -> dict[str, Any]:
    """Print the unattended plan. This entry never enables a unit."""
    from guardian.service_install import execute_install_service

    if not isinstance(dry_run, bool):
        raise ConfigError("dry_run")
    return execute_install_service(dry_run=True, apply=False, mcm_config=mcm_config)


def _load_answers(path: str | None, prompt: Callable[[str], str]) -> dict[str, Any]:
    if path:
        file = Path(path)
        if not file.is_file():
            raise ConfigError("answers file was not found")
        try:
            document = json.loads(file.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            raise ConfigError("answers file is not valid json") from None
        if not isinstance(document, dict):
            raise ConfigError("answers file must be an object")
        return _normalize_answers(document)
    try:
        return _normalize_answers(_prompt_answers(prompt))
    except EOFError:
        raise ConfigError("init needs an answer") from None


def _prompt_answers(prompt: Callable[[str], str]) -> dict[str, Any]:
    tenant = prompt("Tenant id [local]: ").strip() or "local"
    accounts: list[dict[str, Any]] = []
    while True:
        family = prompt("Family (blank finishes accounts): ").strip()
        if family == "":
            break
        address = prompt("Address: ").strip()
        networks = [item.strip() for item in prompt("Networks comma-separated: ").split(",") if item.strip()]
        label = prompt("Label: ").strip()
        relationship = prompt("Relationship [watch_only]: ").strip() or "watch_only"
        accounts.append({
            "address": address,
            "family": family,
            "label": label,
            "networks": networks,
            "relationship": relationship,
        })
    plugins_raw = prompt("Plugins comma-separated (blank for none): ").strip()
    plugins = [item.strip() for item in plugins_raw.split(",") if item.strip()]
    key_mode = prompt("Key mode none or byo [none]: ").strip() or "none"
    credential_envs: dict[str, str] = {}
    if key_mode == "byo":
        entered = prompt("credential env overrides as source=ENV, comma-separated (blank for defaults): ").strip()
        for item in [part.strip() for part in entered.split(",") if part.strip()]:
            if "=" not in item:
                raise ConfigError("credential env override must be source=ENV_NAME")
            source_id, env_name = item.split("=", 1)
            credential_envs[source_id.strip()] = env_name.strip()
    return {
        "accounts": accounts,
        "consent": None,
        "credential_envs": credential_envs,
        "key_mode": key_mode,
        "plugins": plugins,
        "tenant_id": tenant,
    }


def _normalize_answers(document: Mapping[str, Any]) -> dict[str, Any]:
    extra = [key for key in document if key not in _ANSWER_FIELDS]
    if extra:
        raise ConfigError("answers contain an unknown field")
    tenant = document.get("tenant_id", "local")
    if not isinstance(tenant, str) or _TENANT.fullmatch(tenant) is None:
        raise ConfigError("tenant_id is not a local label")
    key_mode = document.get("key_mode", "none")
    if key_mode not in {"none", "byo"}:
        raise ConfigError("key_mode must be none or byo")
    consent = document.get("consent", None)
    if consent is not None and not isinstance(consent, bool):
        raise ConfigError("consent must be a boolean")
    plugins = document.get("plugins", [])
    if not isinstance(plugins, list) or any(not isinstance(item, str) for item in plugins):
        raise ConfigError("plugins must be a list of ids")
    plugins = list(dict.fromkeys(plugins))
    for plugin in plugins:
        if plugin not in _PLUGINS:
            raise ConfigError("plugin is not in the local registry")
    credential_envs = document.get("credential_envs", {})
    if credential_envs is None:
        credential_envs = {}
    if not isinstance(credential_envs, dict):
        raise ConfigError("credential_envs must be an object of env var names")
    if key_mode == "none" and credential_envs:
        raise ConfigError("key_mode none does not take credential env overrides")
    cleaned_envs: dict[str, str] = {}
    for source_id, env_name in credential_envs.items():
        if not isinstance(source_id, str) or _SOURCE_ID.fullmatch(source_id) is None:
            raise ConfigError("credential env source id is invalid")
        if not isinstance(env_name, str) or _ENV_NAME.fullmatch(env_name) is None:
            raise ConfigError("credential env override must be an env var name")
        cleaned_envs[source_id] = env_name
    accounts_raw = document.get("accounts")
    if not isinstance(accounts_raw, list) or not accounts_raw:
        raise ConfigError("at least one account is required")
    accounts = [_normalize_account(item) for item in accounts_raw]
    return {
        "accounts": accounts,
        "consent": consent,
        "credential_envs": cleaned_envs,
        "key_mode": key_mode,
        "plugins": list(plugins),
        "tenant_id": tenant,
    }


def _normalize_account(item: Any) -> dict[str, Any]:
    if not isinstance(item, dict):
        raise ConfigError("account must be an object")
    extra = [key for key in item if key not in _ACCOUNT_FIELDS]
    if extra:
        raise ConfigError("account contains an unknown field")
    family = item.get("family")
    if family not in _FAMILIES:
        raise ConfigError("unknown address family")
    address = item.get("address")
    if not isinstance(address, str) or not address.strip():
        raise ConfigError("account address is missing")
    networks = item.get("networks")
    if not isinstance(networks, list) or not networks or any(not isinstance(name, str) for name in networks):
        raise ConfigError("account networks must be a non-empty list")
    label = item.get("label")
    if not isinstance(label, str) or not label.strip() or len(label.strip()) > 80:
        raise ConfigError("account label is missing")
    relationship = item.get("relationship", "watch_only")
    if relationship not in _RELATIONSHIPS:
        raise ConfigError("account relationship is invalid")
    wallet_id = item.get("wallet_id")
    if wallet_id is None:
        wallet_id = str(uuid4())
    elif not isinstance(wallet_id, str) or _UUID.fullmatch(wallet_id) is None:
        raise ConfigError("wallet_id must be a uuid")
    own_group = item.get("own_group")
    if own_group is not None and (not isinstance(own_group, str) or _OWN_GROUP.fullmatch(own_group) is None):
        raise ConfigError("own_group is invalid")
    registry = _registry()
    canonical_names: list[str] = []
    for network in networks:
        name = canonical_network(network, registry)
        entry = registry[name]
        if entry.get("family") != family:
            raise ConfigError(f"network family mismatch: {name}")
        if name not in canonical_names:
            canonical_names.append(name)
    networks = canonical_names
    try:
        if family == "cosmos":
            normalized = parse_address(family, address.strip(), networks[0])
        else:
            normalized = parse_address(family, address.strip())
    except IdentityError as exc:
        raise ConfigError(exc.reason) from None
    account = {
        "address": normalized.canonical,
        "family": family,
        "label": label.strip(),
        "networks": list(dict.fromkeys(networks)),
        "relationship": relationship,
        "wallet_id": wallet_id,
    }
    if own_group is not None:
        account["own_group"] = own_group
    return account


def _registry() -> dict[str, dict[str, Any]]:
    return load_registry(resource_path("config", "chains"))


def _build(answers: Mapping[str, Any], *, recorded_at: str) -> dict[str, Any]:
    enabled: list[str] = []
    for account in answers["accounts"]:
        for network in account["networks"]:
            if network not in enabled:
                enabled.append(network)
    sources: dict[str, dict[str, Any]] = {}
    budgets: dict[str, dict[str, Any]] = {}
    routing: list[dict[str, Any]] = []
    registry = _registry()
    for network in enabled:
        family = registry[network]["family"]
        if family == "evm":
            _add_evm(network, sources, budgets, routing)
        elif family == "solana":
            _add_solana(sources, budgets, routing)
        else:
            _add_family_reader(network, family, sources, budgets, routing)
    overrides: Mapping[str, str] = answers["credential_envs"]
    for source_id, env_name in overrides.items():
        source = sources.get(source_id)
        if source is None or "credential_env" not in source:
            raise ConfigError("credential env override does not match a selected source")
        source["credential_env"] = env_name
    _same_budget_for_shared_credential(sources)
    secrets = {name: "" for name in sorted(_secret_names(sources))}
    monitor = {
        "schema_version": "guardian.monitor.v1",
        "mode": "read_only",
        "enabled_networks": enabled,
        "private_inputs": {
            "accounts_file_env": _ACCOUNTS_ENV,
            "state_dir_env": _STATE_ENV,
            "secret_profile_file_env": _SECRET_ENV,
        },
        "polling": {
            "interval_seconds": 10800,
            "around_the_clock": True,
            "discovery_interval_seconds": 86400,
            "bootstrap_lookback_days": 30,
            "bootstrap_request_cap": 2000,
            "webhooks_enabled": False,
        },
        "alerts": {
            "native_eth_threshold": "0.5",
            "asset_usd_threshold": "1000",
            "threshold_operator": "any",
            "permission_changes": True,
            "exposure_risk_changes": True,
            "exposure_news_only": True,
            "generic_news": False,
            "unknown_price_policy": "show_unknown_not_zero",
            "baseline_policy": "initial_summary_and_existing_risk_not_new_history",
            "quiet_if_no_change": True,
            "monitor_health_changes": True,
        },
    }
    floors = {
        network: _NATIVE_UNIT_FLOORS[network]
        for network in enabled
        if network in _NATIVE_UNIT_FLOORS
    }
    if floors:
        monitor["alerts"]["native_unit_thresholds"] = floors
    monitor.update({
        "sources": sources,
        "routing": routing,
        "budgets": budgets,
        "plugins": list(answers["plugins"]),
        "sanctions_lists": ["us-ofac-sdn"],
        "privacy": {
            "upload_state": False,
            "third_party_wallet_queries": "deny_until_local_consent",
            "public_report_mode": "aggregate_no_wallet_identifiers",
        },
        "delivery": {
            "mode": "outbox_only",
            "profile_env": _DELIVERY_ENV,
        },
        "execution": {
            "sign": False,
            "broadcast": False,
            "auto_purchase": False,
            "auto_disable_schedule": False,
        },
        "operator_consent": {
            "data_sharing_acknowledged": True,
            "recorded_at": recorded_at,
            "third_party_user_address_lookups": "off",
            "key_mode": answers["key_mode"],
        },
    })
    accounts = {
        "schema_version": "guardian.accounts.v1",
        "tenant_id": answers["tenant_id"],
        "accounts": list(answers["accounts"]),
    }
    delivery = {"mode": "outbox_only", "single_sender_handoff": False}
    sharing = [_share_source(source_id, source) for source_id, source in sources.items()]
    sharing.extend(_disabled_third_parties())
    return {
        "accounts": accounts,
        "delivery": delivery,
        "key_mode": answers["key_mode"],
        "monitor": monitor,
        "secrets": secrets,
        "sharing": sharing,
    }


def _add_evm(network: str, sources: dict[str, Any], budgets: dict[str, Any], routing: list[dict[str, Any]]) -> None:
    rpc_id = f"rpc-{network}"
    _put_source(sources, budgets, rpc_id, {
        "adapter": "evm_rpc",
        "budget_group": rpc_id,
        "enabled": True,
        "endpoint_env": f"RPC_{_env_token(network)}_URL",
    })
    for capability in ("balances.native", "history.token", "permissions.standard"):
        routing.append({"capability": capability, "network": network, "source_ids": [rpc_id]})
    history: list[str] = []
    if network != "bsc-mainnet":
        _put_etherscan(sources, budgets)
        history.append("etherscan")
    if network == "bsc-mainnet":
        _put_source(sources, budgets, "nodereal-bsc", {
            "adapter": "nodereal_transfers",
            "budget_group": "nodereal-bsc",
            "credential_env": "NODEREAL_API_KEY",
            "enabled": True,
            "endpoint_env": "NODEREAL_BSC_URL",
        })
        history.append("nodereal-bsc")
    blockscout_id = _blockscout_id(network)
    if blockscout_id is not None:
        _put_source(sources, budgets, blockscout_id, _blockscout_source(network, blockscout_id))
        history.append(blockscout_id)
    routing.append({"capability": "history.native_external", "network": network, "source_ids": history})


def _put_etherscan(sources: dict[str, Any], budgets: dict[str, Any]) -> None:
    _put_source(sources, budgets, "etherscan", {
        "adapter": "etherscan_v2",
        "budget_group": "etherscan-account",
        "credential_env": "ETHERSCAN_API_KEY",
        "enabled": True,
        "endpoint_env": "ETHERSCAN_API_BASE",
    })


def _blockscout_id(network: str) -> str | None:
    if network in {"bsc-mainnet"}:
        return None
    if network == "base-mainnet":
        return "blockscout-base"
    if network == "optimism-mainnet":
        return "blockscout-optimism"
    slug = network.removesuffix("-mainnet").replace("-", "")
    return f"blockscout-{slug}"


def _blockscout_source(network: str, source_id: str) -> dict[str, Any]:
    if network == "base-mainnet":
        endpoint, credential = "BLOCKSCOUT_BASE_URL", "BLOCKSCOUT_BASE_API_KEY"
    elif network == "optimism-mainnet":
        endpoint, credential = "BLOCKSCOUT_OPTIMISM_URL", "BLOCKSCOUT_OPTIMISM_API_KEY"
    else:
        token = _env_token(network)
        endpoint, credential = f"BLOCKSCOUT_{token}_URL", f"BLOCKSCOUT_{token}_API_KEY"
    return {
        "adapter": "blockscout",
        "budget_group": source_id,
        "credential_env": credential,
        "enabled": True,
        "endpoint_env": endpoint,
        "method_credits": 20,
    }


def _add_solana(sources: dict[str, Any], budgets: dict[str, Any], routing: list[dict[str, Any]]) -> None:
    _put_source(sources, budgets, "rpc-solana-mainnet", {
        "adapter": "solana_rpc",
        "budget_group": "rpc-solana-mainnet",
        "enabled": True,
        "endpoint_env": "RPC_SOLANA_MAINNET_URL",
    })
    _put_source(sources, budgets, "helius-parser", {
        "adapter": "helius_parsed_events",
        "budget_group": "helius-account",
        "credential_env": "HELIUS_API_KEY",
        "enabled": True,
        "endpoint_env": "HELIUS_PARSED_EVENTS_URL",
    })
    routing.append({
        "capability": "balances.native",
        "network": "solana-mainnet",
        "source_ids": ["rpc-solana-mainnet"],
    })


def _add_family_reader(
    network: str,
    family: str,
    sources: dict[str, Any],
    budgets: dict[str, Any],
    routing: list[dict[str, Any]],
) -> None:
    source_id = f"read-{network}"
    spec: dict[str, Any] = {
        "adapter": _READ_ADAPTER[family],
        "budget_group": source_id,
        "enabled": True,
        "endpoint_env": f"{_env_token(network)}_URL",
    }
    if family == "tron":
        spec["credential_env"] = "TRON_PRO_API_KEY"
        spec["endpoint_env"] = "TRONGRID_API_URL"
    _put_source(sources, budgets, source_id, spec)
    routing.append({"capability": "balances.native", "network": network, "source_ids": [source_id]})


def _put_source(sources: dict[str, Any], budgets: dict[str, Any], source_id: str, spec: dict[str, Any]) -> None:
    if source_id in sources:
        return
    sources[source_id] = spec
    group = spec["budget_group"]
    budgets.setdefault(group, {
        "max_concurrency": 1,
        "max_requests_per_day": 2000,
        "max_rps": 1,
        "retry_attempts": 2,
    })


def _same_budget_for_shared_credential(sources: Mapping[str, Mapping[str, Any]]) -> None:
    groups: dict[str, set[str]] = {}
    for source in sources.values():
        credential = source.get("credential_env")
        if isinstance(credential, str):
            groups.setdefault(credential, set()).add(str(source["budget_group"]))
    for credential, found in groups.items():
        if len(found) != 1:
            raise ConfigError(f"shared credential_env must share one budget_group: {credential}")


def _secret_names(sources: Mapping[str, Mapping[str, Any]]) -> set[str]:
    names: set[str] = set()
    for source in sources.values():
        for field in ("endpoint_env", "credential_env"):
            value = source.get(field)
            if isinstance(value, str):
                names.add(value)
    return names


def _share_source(source_id: str, source: Mapping[str, Any]) -> dict[str, Any]:
    adapter = str(source.get("adapter"))
    credential = source.get("credential_env")
    keyed = isinstance(credential, str)
    endpoint_env = str(source.get("endpoint_env"))
    if adapter == "tron_grid":
        recipient = "TronGrid at the host named by " + endpoint_env
        data = (
            "Wallet address is sent on keyless and keyed reads. "
            "The optional TRON_PRO_API_KEY is sent only as the TRON-PRO-API-KEY header. "
            "A missing key does not skip the read and is not credential_missing."
        )
        sends_addresses = True
    elif adapter in {"evm_rpc", "solana_rpc"}:
        recipient = "the RPC host named by " + endpoint_env
        data = (
            "Wallet address is sent in balance, log, and call reads to the host you set in the endpoint variable. "
            "The machine hostname is not sent."
        )
        sends_addresses = True
    elif keyed:
        recipient = _provider_name(adapter) + " at the host named by " + endpoint_env
        data = (
            "Wallet address is included when this source is called. "
            "With no key, the address is not sent and coverage stays credential_missing."
        )
        sends_addresses = True
    else:
        recipient = _provider_name(adapter) + " at the host named by " + endpoint_env
        data = (
            "This family is not queried in this build, so the address is not sent. "
            "Coverage stays unsupported, not zero, until an adapter exists."
        )
        sends_addresses = False
    return {
        "adapter": adapter,
        "credential_env": credential if keyed else None,
        "data": data,
        "enabled": source.get("enabled") is True,
        "endpoint_env": source.get("endpoint_env"),
        "recipient": recipient,
        "sends_addresses": sends_addresses,
        "sends_hostnames": False,
        "source_id": source_id,
        "third_party_user_address_lookup": False,
    }


def _provider_name(adapter: str) -> str:
    return {
        "aptos_rest": "the Aptos REST host",
        "blockscout": "Blockscout",
        "cosmos_rest": "the Cosmos REST host",
        "esplora": "the Esplora host",
        "etherscan_v2": "Etherscan",
        "helius_parsed_events": "Helius",
        "nodereal_transfers": "NodeReal",
        "sui_graphql": "the Sui GraphQL host",
        "toncenter_v3": "TON Center",
        "tron_grid": "TronGrid",
    }.get(adapter, adapter)


def _disabled_third_parties() -> list[dict[str, Any]]:
    rows = []
    for source_id, adapter, recipient in (
        ("goplus", "goplus", "GoPlus"),
        ("chainabuse", "chainabuse", "Chainabuse"),
    ):
        rows.append({
            "adapter": adapter,
            "credential_env": None,
            "data": "User-address lookup is off. Init does not enable it and does not send the address.",
            "enabled": False,
            "endpoint_env": None,
            "recipient": recipient,
            "sends_addresses": False,
            "sends_hostnames": False,
            "source_id": source_id,
            "third_party_user_address_lookup": True,
        })
    return rows


def _env_token(network: str) -> str:
    return network.upper().replace("-", "_")


def _directories(directory: str | None) -> tuple[Path, Path]:
    if directory:
        root = Path(directory).expanduser()
        return root, root / "state"
    return config_dir(), state_dir()


def _refuse_work_tree(path: Path) -> None:
    root = git_work_tree()
    if root is not None and path_inside_work_tree(path, root):
        raise ConfigError("private dir is inside the git work tree")


def _render(plan: Mapping[str, Any], root: Path, state: Path) -> dict[str, str]:
    env_paths = {
        _ACCOUNTS_ENV: str(root / "accounts.json"),
        _CONFIG_ENV: str(root / "monitor.json"),
        _DELIVERY_ENV: str(root / "delivery-profile.json"),
        _SECRET_ENV: str(root / "secret-profile.json"),
        _STATE_ENV: str(state),
    }
    return {
        "accounts.json": _dump(plan["accounts"]),
        "delivery-profile.json": _dump(plan["delivery"]),
        "env-paths.json": _dump(env_paths),
        "monitor.json": _dump(plan["monitor"]),
        "secret-profile.json": _dump(plan["secrets"]),
    }


def _dump(document: Mapping[str, Any]) -> str:
    return json.dumps(document, ensure_ascii=False, indent=2) + "\n"


def _refuse_secret_leaks(
    texts: Mapping[str, str],
    environ: Mapping[str, str],
    root: Path,
    state: Path,
) -> None:
    blob = "\n".join(texts.values())
    intentional = {str(root), str(state), str(root / "accounts.json"), str(root / "monitor.json")}
    intentional.update(str(root / name) for name in _FILE_NAMES)
    for name, value in environ.items():
        if not isinstance(name, str) or not isinstance(value, str):
            continue
        secret = value.strip()
        if len(secret) < 8 or secret in intentional or secret not in blob:
            continue
        upper = name.upper()
        if upper.startswith(_PATH_PREFIXES):
            continue
        if "://" in secret or any(part in upper for part in _SENSITIVE_PARTS):
            raise ConfigError("refusing to write an environment secret")


def _validate_in_memory(monitor_text: str, accounts_text: str) -> None:
    with tempfile.TemporaryDirectory(prefix="guardian-init-") as temp:
        root = Path(temp)
        monitor_path = root / "monitor.json"
        accounts_path = root / "accounts.json"
        monitor_path.write_text(monitor_text, encoding="utf-8")
        accounts_path.write_text(accounts_text, encoding="utf-8")
        load_public_config(monitor_path)
        load_accounts(accounts_path)


def _summary(
    plan: Mapping[str, Any],
    texts: Mapping[str, str],
    root: Path,
    state: Path,
    *,
    wrote: bool,
    backups: list[dict[str, str]],
) -> dict[str, Any]:
    env_paths = json.loads(texts["env-paths.json"])
    return {
        "backups": backups,
        "data_sharing": plan["sharing"],
        "delivery_mode": "outbox_only",
        "env": env_paths,
        "files": list(_FILE_NAMES),
        "key_mode": plan["key_mode"],
        "not_auto_enabled": ["address_derivation", "chainabuse", "goplus", "sender", "systemd"],
        "private_dir": str(root),
        "sender_started": False,
        "state_dir": str(state),
        "tenant_scope": "single_operator_not_a_tenant_isolation_boundary",
        "third_party_user_address_lookups": "off",
        "wrote": wrote,
    }


def _backup(root: Path, names: list[str]) -> list[dict[str, str]]:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    written: list[dict[str, str]] = []
    for name in names:
        source = root / name
        if not source.is_file():
            continue
        dest = _backup_path(source, stamp)
        secure_write_text(dest, source.read_text(encoding="utf-8"))
        written.append({"backup": dest.name, "file": name})
    return written


def _backup_path(path: Path, stamp: str) -> Path:
    candidate = path.with_name(f"{path.name}.bak-{stamp}")
    index = 2
    while candidate.exists():
        candidate = path.with_name(f"{path.name}.bak-{stamp}-{index}")
        index += 1
    return candidate


def _write_all(root: Path, state: Path, texts: Mapping[str, str]) -> None:
    ensure_private_dir(root)
    ensure_private_dir(state)
    for name, text in texts.items():
        secure_write_text(root / name, text)
