"""Public monitor profile and private account loading.

Public files name environment variables. They do not contain addresses, API
keys, or state. Private paths must sit outside the git work tree. This module
does not open a socket, sign, or broadcast.

A monitored address is sent only to an approved chain RPC or indexer, and it is
stored in the private accounts file. News, promotions, and risk providers that
lack consent do not receive it. Public export and routine diagnostics do not
copy it or any credential. See ``guardian.security.privacy_boundary``.
"""

from __future__ import annotations

import json
import os
import re
import stat
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from guardian.errors import ConfigError
from guardian.identities import (
    IdentityError,
    NormalizedAddress,
    WalletIdentity,
    index_wallets,
    parse_address,
)
from guardian.jsonschema_lite import SchemaError, ValidationError, validate
from guardian.network_aliases import apply_account_aliases, apply_public_aliases
from guardian.network_registry import load_registry
from guardian.resources import resource_path
from guardian.security.redact import redact
_ENV_NAME = re.compile(r"^[A-Z][A-Z0-9_]{1,95}$")
_FORBIDDEN_KEYS = frozenset({
    "address",
    "addresses",
    "apikey",
    "privatekey",
    "secret",
    "token",
    "state",
    "mnemonic",
    "seed",
})
_EVM_VALUE = re.compile(r"(?i)(?<![0-9a-f])0x[0-9a-f]{40}(?![0-9a-f])")
_BARE_HEX = re.compile(r"^[0-9a-fA-F]{40}$")
_TOKEN_VALUE = re.compile(r"(?i)(?<![A-Za-z0-9])(?:glpat-|sk-|sk_)[A-Za-z0-9_\-]{6,}")
_URL = re.compile(r"https?://[^\s\"']+", re.IGNORECASE)
_QUERY_SECRET = re.compile(
    r"(?i)(?:^|[?&#])(?:api[_-]?key|apikey|access[_-]?key|token|key|secret)="
)
_OPAQUE_SEGMENT = re.compile(r"/[A-Za-z0-9_\-]{32,}")
_USERINFO = re.compile(r"^[a-z][a-z0-9+.-]*://[^\s/]+@", re.IGNORECASE)
# Risk and price adapters are not tied to one chain family.
_ADAPTER_FAMILY = {
    "evm_rpc": "evm",
    "etherscan_v2": "evm",
    "blockscout": "evm",
    "nodereal_transfers": "evm",
    "alchemy_transfers": "evm",
    "solana_rpc": "solana",
    "helius_parsed_events": "solana",
    "esplora": "bitcoin",
    "tron_grid": "tron",
    "toncenter_v3": "ton",
    "toncenter_v2": "ton",
    "sui_graphql": "sui",
    "sui_grpc": "sui",
    "aptos_indexer": "aptos",
    "aptos_rest": "aptos",
    "cosmos_rest": "cosmos",
    "comet_rpc": "cosmos",
    "goplus": None,
    "ofac_sls": None,
    "chainabuse": None,
    "coingecko": None,
    "debank": None,
}


class MaskedSecret:
    """A credential that repr, str, and format cannot reveal."""

    __slots__ = ("_value",)

    def __init__(self, value: str) -> None:
        self._value = value

    @property
    def present(self) -> bool:
        return bool(self._value.strip())

    def reveal(self) -> str:
        return self._value

    def __repr__(self) -> str:
        return "MaskedSecret('***')"

    def __str__(self) -> str:
        return "***"

    def __format__(self, spec: str) -> str:
        return "***"


@dataclass(frozen=True)
class SourceStatus:
    source_id: str
    adapter: str
    enabled: bool
    credential_present: bool
    availability: str
    blocked_reason: str | None


@dataclass(frozen=True)
class AccountRecord:
    wallet_id: str
    label: str
    family: str
    normalized: NormalizedAddress
    networks: tuple[str, ...]
    relationship: str
    own_group: str | None
    trc20_contracts: tuple[str, ...] = ()
    allowance_spenders: tuple[str, ...] = ()


@dataclass(frozen=True)
class AccountsDocument:
    schema_version: str
    tenant_id: str
    accounts: tuple[AccountRecord, ...]


@dataclass
class PublicConfig:
    document: dict[str, Any]
    path: Path
    source_status: dict[str, SourceStatus] = field(default_factory=dict)

    def __repr__(self) -> str:
        networks = self.document.get("enabled_networks") or []
        return f"PublicConfig(schema_version={self.document.get('schema_version')!r}, networks={len(networks)})"


@dataclass
class PrivateInputs:
    accounts: AccountsDocument
    source_status: dict[str, SourceStatus]
    notes: tuple[str, ...]
    state_dir: Path

    def __repr__(self) -> str:
        flags = {key: value.availability for key, value in self.source_status.items()}
        return f"PrivateInputs(sources={flags!r}, notes={list(self.notes)!r})"


def load_public_config(path: str | Path) -> PublicConfig:
    """Schema-validate a public monitor profile and check references."""
    path = Path(path)
    if not path.is_file():
        raise ConfigError("public config was not found")
    text = path.read_text(encoding="utf-8")
    if _text_has_secret(_scrub_log_contracts(text)):
        raise ConfigError("forbidden value")
    document = _read_object(text, "public config")
    apply_public_aliases(document, load_registry(resource_path("config", "chains")))
    _validate_document(document, resource_path("schemas", "monitor.schema.json"), "monitor")
    reject_private_material(document)
    _semantic_public(document)
    return PublicConfig(document=document, path=path)


def load_accounts(path: str | Path, *, registry: dict[str, dict[str, Any]] | None = None) -> AccountsDocument:
    """Schema-validate accounts and normalize each declared address."""
    path = Path(path)
    if not path.is_file():
        raise ConfigError("accounts file was not found")
    document = _read_object(path.read_text(encoding="utf-8"), "accounts")
    networks = registry if registry is not None else load_registry(resource_path("config", "chains"))
    apply_account_aliases(document, networks)
    _validate_document(document, resource_path("schemas", "accounts.schema.json"), "accounts")
    seen_ids: set[str] = set()
    records: list[AccountRecord] = []
    identities: list[WalletIdentity] = []
    for account in document["accounts"]:
        wallet_id = account["wallet_id"]
        if wallet_id in seen_ids:
            raise ConfigError("duplicate_wallet_id")
        seen_ids.add(wallet_id)
        family = account["family"]
        declared = tuple(account["networks"])
        for network in declared:
            entry = networks.get(network)
            if entry is None:
                raise ConfigError(f"unknown network {network}")
            if entry["family"] != family:
                raise ConfigError(f"network family mismatch: {network}")
        normalized = _normalize_account(family, account["address"], declared)
        record = AccountRecord(
            wallet_id=wallet_id,
            label=account["label"],
            family=family,
            normalized=normalized,
            networks=declared,
            relationship=account["relationship"],
            own_group=account.get("own_group"),
            trc20_contracts=_tron_watch(account, "trc20_contracts", family),
            allowance_spenders=_tron_watch(account, "allowance_spenders", family),
        )
        records.append(record)
        identities.append(WalletIdentity(wallet_id, record.label, normalized))
    try:
        index_wallets(identities)
    except IdentityError as exc:
        raise ConfigError(exc.reason) from None
    return AccountsDocument(document["schema_version"], document["tenant_id"], tuple(records))


def resolve_private_inputs(public_config: PublicConfig, environ: Mapping[str, str]) -> PrivateInputs:
    """Locate private files from env var names. Missing credentials block one source."""
    if not isinstance(public_config, PublicConfig):
        raise ConfigError("public config is missing")
    names = public_config.document["private_inputs"]
    accounts_path = _env_path(environ, names["accounts_file_env"])
    state_dir = _env_path(environ, names["state_dir_env"])
    secret_path = _env_path(environ, names["secret_profile_file_env"])
    root = git_work_tree()
    if root is not None and path_inside_work_tree(accounts_path, root):
        raise ConfigError("private accounts path is inside the git work tree")
    if root is not None and path_inside_work_tree(state_dir, root):
        raise ConfigError("private state path is inside the git work tree")
    if root is not None and path_inside_work_tree(secret_path, root):
        raise ConfigError("private secret path is inside the git work tree")
    if not accounts_path.is_file():
        raise ConfigError("accounts file was not found")
    if not secret_path.is_file():
        raise ConfigError("secret profile was not found")
    _require_owner_only(accounts_path, "accounts")
    _require_owner_only(secret_path, "secret profile")
    accounts = load_accounts(accounts_path)
    profile = _load_secret_profile(secret_path)
    status = _source_status(public_config.document, profile, environ)
    # Drop the only copy of the credential values before returning.
    profile.clear()
    note = _prepare_state_dir(state_dir)
    notes = (note,) if note else ()
    public_config.source_status = status
    return PrivateInputs(accounts=accounts, source_status=status, notes=notes, state_dir=state_dir)


def safe_export(public_config: PublicConfig | dict[str, Any], accounts: Any, state_summary: dict[str, Any] | None = None) -> dict[str, Any]:
    """Share counts and source names. Do not copy anything that identifies a wallet."""
    document = public_config.document if isinstance(public_config, PublicConfig) else public_config
    if not isinstance(document, dict):
        raise ConfigError("public config is missing")
    by_family: dict[str, int] = {}
    by_network: dict[str, int] = {}
    for record in _account_records(accounts):
        by_family[record.family] = by_family.get(record.family, 0) + 1
        for network in record.networks:
            by_network[network] = by_network.get(network, 0) + 1
    presence = _presence_map(public_config, state_summary)
    sources = []
    for name in sorted(document["sources"]):
        source = document["sources"][name]
        sources.append({
            "name": name,
            "adapter": source["adapter"],
            "enabled": source["enabled"] is True,
            "credential_present": presence.get(name, False) is True,
        })
    exported = {
        "schema_version": document["schema_version"],
        "enabled_networks": list(document["enabled_networks"]),
        "wallet_counts": {
            "by_family": dict(sorted(by_family.items())),
            "by_network": dict(sorted(by_network.items())),
        },
        "sources": sources,
        "budgets": json.loads(json.dumps(document["budgets"])),
    }
    reject_private_material(exported)
    return exported


def reject_private_material(value: Any) -> None:
    """Refuse secret-shaped keys and values anywhere in a public document."""
    _walk(value)


def file_permission_bits(path: Path) -> int | None:
    """Unix mode bits, or None when this platform does not enforce them."""
    if os.name != "posix":
        return None
    return stat.S_IMODE(path.stat().st_mode)


def git_work_tree(start: Path | None = None) -> Path | None:
    """Nearest directory that contains ``.git``, or None outside a checkout.

    An installed monitor has no checkout. Private files are then allowed.
    A source checkout is still found from the loaded package, not from a
    caller-supplied label.
    """
    if start is None:
        from guardian.runtime_binding import source_checkout_root

        checkout = source_checkout_root()
        start = checkout if checkout is not None else Path.cwd()
    current = Path(start).resolve()
    while True:
        if (current / ".git").exists():
            return current
        parent = current.parent
        if parent == current:
            return None
        current = parent


def path_inside_work_tree(path: Path, root: Path | None = None) -> bool:
    base = root if root is not None else git_work_tree()
    if base is None:
        return False
    base = base.resolve()
    child = os.path.normcase(str(path.resolve()))
    parent = os.path.normcase(str(base))
    if child == parent:
        return True
    prefix = parent if parent.endswith(os.sep) else parent + os.sep
    return child.startswith(prefix)


def _semantic_public(document: dict[str, Any]) -> None:
    from guardian.rules import validate_native_unit_thresholds, validate_token_raw_thresholds

    registry = load_registry(resource_path("config", "chains"))
    alerts = document.get("alerts")
    if isinstance(alerts, dict):
        validate_native_unit_thresholds(alerts.get("native_unit_thresholds"))
        validate_token_raw_thresholds(alerts.get("token_raw_thresholds"))
    enabled = set(document["enabled_networks"])
    for network in enabled:
        if network not in registry:
            raise ConfigError(f"unknown network {network}")
    sources = document["sources"]
    budgets = document["budgets"]
    shared: dict[str, set[str]] = {}
    for source_id, source in sources.items():
        _env_only(source.get("endpoint_env"), "endpoint_env")
        credential = source.get("credential_env")
        if credential is not None:
            _env_only(credential, "credential_env")
        budget = source["budget_group"]
        if budget not in budgets:
            raise ConfigError(f"unknown budget_group {budget}")
        if isinstance(credential, str):
            shared.setdefault(credential, set()).add(budget)
        adapter = source["adapter"]
        if adapter not in _ADAPTER_FAMILY:
            raise ConfigError(f"unknown adapter {adapter}")
    for credential, groups in shared.items():
        if len(groups) != 1:
            raise ConfigError(f"shared credential_env must share one budget_group: {credential}")
    # Plugin ids are data. This loader does not import or execute them.
    for name in document["plugins"]:
        if not isinstance(name, str):
            raise ConfigError("plugin name is not executable data")
    resolver = document.get("resolver")
    if resolver is not None:
        if not isinstance(resolver, dict):
            raise ConfigError("resolver config is missing")
        for field_name in ("mode_env", "endpoints_env", "bootstrap_env"):
            if field_name in resolver:
                _env_only(resolver[field_name], field_name)
    for route in document["routing"]:
        network = route["network"]
        if network not in enabled:
            raise ConfigError(f"routing network is not enabled: {network}")
        family = registry[network]["family"]
        for source_id in route["source_ids"]:
            if source_id not in sources:
                raise ConfigError(f"unknown source {source_id}")
            adapter = sources[source_id]["adapter"]
            expected = _ADAPTER_FAMILY[adapter]
            if expected is not None and expected != family:
                raise ConfigError(f"adapter family mismatch: {adapter} on {network}")


def _normalize_account(family: str, address: str, networks: tuple[str, ...]) -> NormalizedAddress:
    try:
        if family != "cosmos":
            return parse_address(family, address)
        normalized = None
        for network in networks:
            parsed = parse_address(family, address, network)
            if normalized is None:
                normalized = parsed
            elif parsed.identity_key != normalized.identity_key:
                raise ConfigError("network family mismatch")
        if normalized is None:
            raise ConfigError("bad_address")
        return normalized
    except IdentityError as exc:
        raise ConfigError(exc.reason) from None


def _tron_watch(account: Mapping[str, Any], field: str, family: str) -> tuple[str, ...]:
    """Configured TRON contracts or spenders. Canonical base58, capped, never discovered."""
    raw = account.get(field) or ()
    if raw in (None, ()):
        return ()
    if not isinstance(raw, list) or any(not isinstance(item, str) or not item.strip() for item in raw):
        raise ConfigError("bad_address")
    if family != "tron":
        raise ConfigError("tron_watch_family")
    if len(raw) > 8:
        raise ConfigError("tron_watch_cap")
    found: list[str] = []
    for item in raw:
        try:
            found.append(parse_address("tron", item).canonical)
        except IdentityError as exc:
            raise ConfigError(exc.reason) from None
    return tuple(found)


def _source_status(document: dict[str, Any], profile: dict[str, str], environ: Mapping[str, str]) -> dict[str, SourceStatus]:
    status: dict[str, SourceStatus] = {}
    for source_id, source in document["sources"].items():
        credential = source.get("credential_env")
        if not isinstance(credential, str):
            status[source_id] = SourceStatus(source_id, source["adapter"], source["enabled"] is True, False, "not_required", None)
            continue
        present = _credential_present(credential, profile, environ)
        if not present and source.get("adapter") == "coingecko":
            # The public tier needs no key. A missing demo key is a lower limit, not a gap.
            status[source_id] = SourceStatus(
                source_id, source["adapter"], source["enabled"] is True, False, "keyless", None,
            )
            continue
        if not present and source.get("adapter") == "tron_grid":
            # The optional key is not required. A keyless read still sends the watch address.
            status[source_id] = SourceStatus(
                source_id, source["adapter"], source["enabled"] is True, False, "keyless", None,
            )
            continue
        status[source_id] = SourceStatus(
            source_id,
            source["adapter"],
            source["enabled"] is True,
            present,
            "available" if present else "missing",
            None if present else "credential_missing",
        )
    return status


def _credential_present(name: str, profile: dict[str, str], environ: Mapping[str, str]) -> bool:
    from guardian.credentials import resolve_named

    return resolve_named(name, profile, environ).present


def _load_secret_profile(path: Path) -> dict[str, str]:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        raise ConfigError("secret profile is not readable") from None
    document = _read_object(text, "secret profile")
    cleaned: dict[str, str] = {}
    for key, value in document.items():
        if not isinstance(key, str) or _ENV_NAME.fullmatch(key) is None:
            raise ConfigError("secret profile keys must be env var names")
        if not isinstance(value, str):
            raise ConfigError("secret profile values must be strings")
        cleaned[key] = value
    return cleaned


def _prepare_state_dir(path: Path) -> str | None:
    if path.exists() and not path.is_dir():
        raise ConfigError("state dir is not a directory")
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    if os.name != "posix":
        return "windows_permission_bits_not_enforced"
    os.chmod(path, 0o700)
    mode = stat.S_IMODE(path.stat().st_mode)
    if mode & 0o077:
        raise ConfigError("state directory is group or world accessible")
    return None


def _require_owner_only(path: Path, kind: str) -> None:
    mode = file_permission_bits(path)
    if mode is None:
        return
    if mode & 0o077:
        raise ConfigError(f"{kind} file is group or world readable")


def _env_path(environ: Mapping[str, str], name: str) -> Path:
    raw = environ.get(name)
    if not isinstance(raw, str) or not raw.strip():
        raise ConfigError(f"{name} is not set")
    return Path(raw).expanduser()


def _env_only(value: Any, field_name: str) -> None:
    if not isinstance(value, str) or _ENV_NAME.fullmatch(value) is None or "://" in value or "/" in value:
        raise ConfigError(f"{field_name} must be an env var name")


def _presence_map(public_config: PublicConfig | dict[str, Any], state_summary: dict[str, Any] | None) -> dict[str, bool]:
    found: dict[str, bool] = {}
    if isinstance(public_config, PublicConfig):
        for key, status in public_config.source_status.items():
            found[key] = status.credential_present
    if isinstance(state_summary, dict) and isinstance(state_summary.get("source_presence"), dict):
        for key, value in state_summary["source_presence"].items():
            if isinstance(key, str) and isinstance(value, bool):
                found[key] = value
    return found


def _account_records(accounts: Any) -> list[AccountRecord]:
    if isinstance(accounts, AccountsDocument):
        return list(accounts.accounts)
    if isinstance(accounts, (list, tuple)) and all(isinstance(item, AccountRecord) for item in accounts):
        return list(accounts)
    raise ConfigError("accounts are missing")


def _validate_document(document: dict[str, Any], schema_path: Path, label: str) -> None:
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    try:
        validate(document, schema)
    except ValidationError as exc:
        raise ConfigError(redact(str(exc))) from None
    except SchemaError as exc:
        raise ConfigError(f"{label} schema cannot be evaluated: {exc}") from None


def _read_object(text: str, label: str) -> dict[str, Any]:
    try:
        document = json.loads(text)
    except json.JSONDecodeError:
        raise ConfigError(f"{label} is not valid json") from None
    if not isinstance(document, dict):
        raise ConfigError(f"{label} must be an object")
    return document


def _walk(value: Any) -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str) or _norm_key(key) in _FORBIDDEN_KEYS:
                shown = key if isinstance(key, str) else "key"
                raise ConfigError(f"forbidden field: {shown}")
            if key == "log_contract" and isinstance(item, str):
                if not re.fullmatch(r"0x[0-9a-fA-F]{40}", item):
                    raise ConfigError("forbidden value")
                continue
            _walk(item)
        return
    if isinstance(value, list):
        for item in value:
            _walk(item)
        return
    if isinstance(value, str) and _text_has_secret(value):
        raise ConfigError("forbidden value")


def _scrub_log_contracts(text: str) -> str:
    """An emitting-contract filter is not a monitored wallet. The runtime still refuses a wallet."""
    return re.sub(r'("log_contract"\s*:\s*)"0x[0-9a-fA-F]{40}"', r'\1""', text)


def _text_has_secret(value: str) -> bool:
    if _EVM_VALUE.search(value) or _BARE_HEX.fullmatch(value) or _TOKEN_VALUE.search(value):
        return True
    if "apikey=" in value.lower() or "glpat-" in value.lower():
        return True
    for match in _URL.finditer(value):
        url = match.group(0)
        if _QUERY_SECRET.search(url) or _OPAQUE_SEGMENT.search(url) or _USERINFO.search(url):
            return True
    return False


def _norm_key(key: str) -> str:
    return re.sub(r"[^a-z0-9]", "", key.lower())
