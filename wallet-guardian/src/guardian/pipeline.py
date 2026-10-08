"""One offline-capable read of the accounts in a public monitor profile.

The backend is injected. Tests pass a fixture backend. The CLI default is the
HTTP backend. This module does not sign, encode, or broadcast a transaction.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from guardian.chains.evm.adapter import LOG_RANGE_MAX, EvmAdapter, chain_id_operation, evm_rpc, hex_quantity
from guardian.chains.evm.permissions import _PERMISSION_CAPABILITIES, scan_approvals
from guardian.chains.solana.adapter import SolanaAdapter, genesis_hash_operation
from guardian.configuration import (
    AccountsDocument,
    PrivateInputs,
    PublicConfig,
    _env_path,
    _load_secret_profile,
)
from guardian.coverage import CoverageObservation, build_coverage, resolve_support
from guardian.delivery import deliver, load_handoff
from guardian.monitoring import CapabilityCycle
from guardian.network_registry import load_registry
from guardian.rules import AlertPolicy, Candidate
from guardian.sources.budget import BudgetBook
from guardian.sources.capabilities import ROOT, load_source_capabilities
from guardian.sources.transport import HttpxBackend, ReadOnlyTransport, build_endpoint_registry
from guardian.storage import Database

Clock = Callable[[], float]
_APPLICABLE = frozenset({"implemented", "partial"})


@dataclass
class RunReport:
    run_id: str
    started: str
    finished: str
    status: str
    coverage: list[dict[str, Any]]
    decisions: list[dict[str, Any]]
    outbox_written: int
    sent: int
    valuation_unknown: list[str]
    positions: str
    required: dict[str, str]
    native_reads: list[dict[str, str]]
    delivery_status: str
    misconfigured: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            "coverage": self.coverage,
            "decisions": self.decisions,
            "delivery_status": self.delivery_status,
            "misconfigured": self.misconfigured,
            "finished": self.finished,
            "live_verified": False,
            "native_reads": self.native_reads,
            "outbox_written": self.outbox_written,
            "positions": self.positions,
            "required": self.required,
            "run_id": self.run_id,
            "sent": self.sent,
            "started": self.started,
            "status": self.status,
            "valuation_unknown": self.valuation_unknown,
        }


def run_capability_pipeline(
    public_config: PublicConfig,
    accounts: AccountsDocument,
    private_inputs: PrivateInputs,
    database: Database,
    *,
    backend: Any,
    clock: Clock,
    no_send: bool = True,
    sender: Any = None,
) -> RunReport:
    """Read configured wallets and commit one capability cycle."""
    started_at = clock()
    started = _iso(started_at)
    tenant = accounts.tenant_id
    database.upsert_tenant(tenant, tenant)
    transport = _transport(public_config, backend, clock)
    registry = load_registry(ROOT / "config" / "chains")
    catalog = load_source_capabilities()
    policy = AlertPolicy.from_public(public_config.document["alerts"])
    observations: dict[tuple[str, str, str], CoverageObservation] = {}
    candidates: list[Candidate] = []
    baseline: list[tuple[str, str, str]] = []
    native_reads: list[dict[str, str]] = []
    required: dict[str, str] = {}
    enabled = [
        (account, network)
        for account in accounts.accounts
        for network in account.networks
        if network in set(public_config.document["enabled_networks"])
    ]
    seen = _seen(database, tenant)
    for account, network in enabled:
        entry = registry[network]
        family = entry.get("family")
        if family == "evm":
            _read_evm(
                public_config, private_inputs, transport, account, network, entry,
                observations, native_reads, seen, baseline, candidates, tenant,
            )
        elif family == "solana":
            _read_solana(
                public_config, private_inputs, transport, account, network, entry,
                observations, native_reads, seen, baseline, candidates, tenant, started,
            )
        else:
            _block_family(account.wallet_id, network, observations, "unsupported_family")
        _fill_gaps(public_config, private_inputs, catalog, account.wallet_id, network, observations)
    rows = build_coverage(enabled_pairs(enabled), observations, catalog, registry, started)
    for row in rows:
        if row["capability"] == "balances.native":
            required[_req_key(row["wallet_id"], row["network"], row["capability"])] = _required_value(row)
    decisions = policy.apply(candidates, baseline_scopes=baseline)
    alerts = [item.as_dict() for item in decisions if item.alert]
    cursors = _cursors(observations)
    cycle = CapabilityCycle(database, tenant)
    finished_cycle = cycle.finish(
        incoming=_incoming(native_reads, registry),
        results=_result_map(native_reads, registry),
        events=[],
        capability_rows=rows,
        cursors=cursors,
        alerts=alerts,
    )
    emitted = finished_cycle.get("notification_decision", [])
    finished = _iso(clock())
    status = "ok"
    if any(value in {"error", "blocked"} for value in required.values()):
        status = "degraded"
    if enabled and not native_reads and any(value in {"error", "blocked"} for value in required.values()):
        status = "failed"
    handoff = load_handoff(public_config, os.environ)
    delivery = deliver(
        database,
        tenant,
        mode="no_send" if no_send else "send",
        sender=sender,
        handoff=handoff,
        clock=clock,
    )
    unknown = [
        _req_key(row["wallet_id"], row["network"], row["capability"])
        for row in rows
        if row.get("price_quality") == "unknown" and row.get("support") in _APPLICABLE and row["capability"] == "balances.native"
    ]
    return RunReport(
        run_id=f"mcm-{int(started_at)}",
        started=started,
        finished=finished,
        status=status,
        coverage=rows,
        decisions=[item.as_dict() for item in decisions if item.alert or item.valuation_unknown],
        outbox_written=len(emitted),
        sent=0 if no_send else delivery.sent,
        valuation_unknown=unknown,
        positions="unknown",
        required=required,
        native_reads=native_reads,
        delivery_status=delivery.status,
        misconfigured=delivery.misconfigured,
    )


def probe_sources(
    public_config: PublicConfig,
    private_inputs: PrivateInputs,
    *,
    backend: Any,
    clock: Clock,
) -> dict[str, Any]:
    """Per-source configuration and, for RPC, a chain-id or genesis read."""
    transport = _transport(public_config, backend, clock)
    registry = load_registry(ROOT / "config" / "chains")
    sources = []
    for name in sorted(public_config.document["sources"]):
        source = public_config.document["sources"][name]
        status = private_inputs.source_status.get(name)
        credential_required = isinstance(source.get("credential_env"), str)
        present = bool(status and status.credential_present)
        identity: dict[str, Any] = {"probe": "untested", "status": "not_probed"}
        enabled = source.get("enabled") is True
        blocked = status.blocked_reason if status and status.blocked_reason else None
        endpoint = ""
        registered = transport.endpoints.sources.get(name)
        if registered is not None:
            endpoint = registered.endpoint
        if not enabled:
            identity = {"probe": "untested", "status": "not_probed", "reason": "source_disabled"}
        elif blocked:
            identity = {"probe": "untested", "status": "blocked", "reason": blocked}
        elif not endpoint:
            identity = {"probe": "untested", "status": "blocked", "reason": "endpoint_missing"}
        elif source.get("adapter") == "evm_rpc":
            identity = _probe_chain(transport, name, _network_of(name, registry), registry)
        elif source.get("adapter") == "solana_rpc":
            identity = _probe_genesis(transport, name, _network_of(name, registry), registry)
        sources.append({
            "adapter": source.get("adapter"),
            "configured": True,
            "credential_present": present,
            "credential_required": credential_required,
            "enabled": enabled,
            "id": name,
            "identity": identity,
        })
    return {"live_verified": False, "sources": sources, "status": "ok"}


def stored_coverage(database: Database, tenant: str) -> list[dict[str, Any]]:
    rows = []
    for row in database.capability_state_for(tenant):
        payload = row.get("payload")
        if isinstance(payload, dict) and isinstance(payload.get("coverage"), dict):
            rows.append(payload["coverage"])
    return rows


def default_backend() -> HttpxBackend:
    return HttpxBackend()


def _transport(public_config: PublicConfig, backend: Any, clock: Clock) -> ReadOnlyTransport:
    secrets = _secrets(public_config)
    book = BudgetBook.from_document(public_config.document["budgets"], clock=clock, sleep=lambda _seconds: None)
    endpoints = build_endpoint_registry(public_config.document, secrets=secrets, environ=os.environ)
    return ReadOnlyTransport(endpoints, book, backend, resolve=_resolve)


def _secrets(public_config: PublicConfig) -> dict[str, str]:
    path = _env_path(os.environ, public_config.document["private_inputs"]["secret_profile_file_env"])
    return _load_secret_profile(path)


def _resolve(host: str) -> list[str]:
    name = host.lower().rstrip(".")
    if name.endswith(".invalid"):
        return ["1.1.1.1"]
    from guardian.sources.transport import _default_resolve

    return _default_resolve(name)


def _read_evm(public_config, private_inputs, transport, account, network, entry, observations, native_reads, seen, baseline, candidates, tenant) -> None:
    wallet_id = account.wallet_id
    source_id = _family_source(public_config, private_inputs, network, "evm_rpc")
    if source_id is None:
        _block_caps(wallet_id, network, observations, ("balances.native", "history.fungible", *_PERMISSION_CAPABILITIES), "endpoint_missing")
        return
    adapter = EvmAdapter(entry, transport, rpc_source_id=source_id)
    session = adapter.open()
    if not session.identity.ok:
        _block_caps(
            wallet_id, network, observations,
            ("balances.native", "history.fungible", *_PERMISSION_CAPABILITIES),
            session.identity.reason or "identity_unreadable",
        )
        return
    balance = session.native_balance(account.normalized.canonical)
    if balance.observation is not None:
        observations[(wallet_id, network, "balances.native")] = balance.observation
    if balance.raw_wei:
        native_reads.append({"network": network, "raw_amount": balance.raw_wei, "wallet_id": wallet_id})
        _baseline(wallet_id, network, "balances.native", seen, baseline, candidates, tenant)
    head = _head(transport, source_id)
    if head is None:
        start, end = 0, LOG_RANGE_MAX
    else:
        start, end = max(0, head - LOG_RANGE_MAX), head
    history = session.token_history_from_logs(account.normalized.canonical, start, end)
    if history.observation is not None:
        observations[(wallet_id, network, "history.fungible")] = history.observation
        if history.observation.category in {"ok", "empty", "partial"}:
            _baseline(wallet_id, network, "history.fungible", seen, baseline, candidates, tenant)
    scan = scan_approvals(
        transport,
        source_id,
        account.normalized.canonical,
        start,
        end,
        previous_cursor=None,
        gap_detection_complete=False,
    )
    for capability, observation in scan.coverage.items():
        observations[(wallet_id, network, capability)] = observation
        if observation.category in {"ok", "empty", "partial"}:
            _baseline(wallet_id, network, capability, seen, baseline, candidates, tenant)
    risks = [row for row in scan.rows if row.status == "active" and ("unlimited" in row.flags or row.change in {"new", "increased", "first_observed"})]
    if risks and (wallet_id, network, "permissions.erc20") in baseline:
        for row in risks:
            candidates.append(Candidate(
                tenant_id=tenant,
                wallet_id=wallet_id,
                network=network,
                capability=row.capability,
                identity=f"approval:{row.change}:{row.log_index}",
                kind="permission",
                raw_amount=_raw_int(row.raw_amount),
                permission_change=row.change,
                prior_known=row.change != "first_observed",
                explicit_risk=True,
                tx_id=row.tx_id,
            ))
    _history_external(public_config, private_inputs, wallet_id, network, observations)


def _read_solana(public_config, private_inputs, transport, account, network, entry, observations, native_reads, seen, baseline, candidates, tenant, started) -> None:
    wallet_id = account.wallet_id
    source_id = _family_source(public_config, private_inputs, network, "solana_rpc")
    if source_id is None:
        _block_caps(wallet_id, network, observations, ("balances.native", "balances.fungible", "permissions.spl_delegate", "permissions.spl_authority"), "endpoint_missing")
        return
    adapter = SolanaAdapter(entry, transport, rpc_source_id=source_id)
    session = adapter.open()
    if not session.identity.ok:
        _block_caps(
            wallet_id, network, observations,
            ("balances.native", "balances.fungible", "permissions.spl_delegate", "permissions.spl_authority"),
            session.identity.reason or "identity_unreadable",
        )
        return
    balance = session.native_balance(account.normalized.canonical)
    scanned = session.scan(
        account.normalized.canonical,
        tenant_id=tenant,
        wallet_id=wallet_id,
        observed_at=started,
        fetch_transactions=False,
    )
    for capability, observation in scanned.observations.items():
        observations[(wallet_id, network, capability)] = observation
        if observation.category in {"ok", "empty", "partial"}:
            _baseline(wallet_id, network, capability, seen, baseline, candidates, tenant)
    raw = balance.raw_lamports if balance.raw_lamports is not None else scanned.native_lamports
    if raw is not None:
        native_reads.append({"network": network, "raw_amount": str(raw), "wallet_id": wallet_id})
    authorities = getattr(scanned, "authorities", None)
    for change in (() if authorities is None else authorities.changes):
        if change.change not in {"new", "increased", "replaced", "first_observed", "extended"}:
            continue
        if (wallet_id, network, "permissions.spl_delegate") not in set(baseline):
            candidates.append(Candidate(
                tenant_id=tenant,
                wallet_id=wallet_id,
                network=network,
                capability="permissions.spl_delegate",
                identity=f"authority:{change.change}:{change.field}",
                kind="permission",
                permission_change="first_observed" if change.change in {"first_observed", "replaced"} or change.previous is None else change.change,
                prior_known=change.previous is not None and change.change != "first_observed",
                explicit_risk=True,
            ))


def _history_external(public_config, private_inputs, wallet_id, network, observations) -> None:
    source_id = _routed(public_config, network, "history.native_external")
    if source_id is None:
        observations[(wallet_id, network, "history.native_external")] = _blocked("endpoint_missing")
        return
    status = private_inputs.source_status.get(source_id)
    source = public_config.document["sources"].get(source_id, {})
    if isinstance(source.get("credential_env"), str) and (status is None or not status.credential_present):
        observations[(wallet_id, network, "history.native_external")] = _blocked("credential_missing")


def _fill_gaps(public_config, private_inputs, catalog, wallet_id, network, observations) -> None:
    privacy = public_config.document.get("privacy", {}).get("third_party_wallet_queries")
    for capability in (
        "address_risk",
        "asset_risk",
        "sanctions",
        "positions",
        "withdrawals",
        "bridges",
        "exposure_news",
    ):
        key = (wallet_id, network, capability)
        if key in observations:
            continue
        support, _probe = resolve_support(catalog, network, capability)
        if capability in {"address_risk", "asset_risk"} and support in _APPLICABLE:
            if privacy != "operator_approved_sources_only":
                observations[key] = _blocked("third_party_denied")
            else:
                observations[key] = _blocked("not_run")
            continue
        if capability == "sanctions" and support in _APPLICABLE:
            observations[key] = _blocked("feed_not_supplied")
            continue
        if support == "unsupported":
            observations[key] = _unsupported()
        elif support in _APPLICABLE:
            source_id = _routed(public_config, network, capability)
            status = None if source_id is None else private_inputs.source_status.get(source_id)
            if status is not None and status.blocked_reason:
                observations[key] = _blocked(status.blocked_reason)


def _family_source(public_config, private_inputs, network: str, adapter: str) -> str | None:
    for name, source in public_config.document["sources"].items():
        if source.get("adapter") != adapter or source.get("enabled") is not True:
            continue
        if network not in name and not _routed_has(public_config, network, name):
            continue
        status = private_inputs.source_status.get(name)
        if status is not None and status.blocked_reason:
            continue
        return name
    return None


def _routed(public_config, network: str, capability: str) -> str | None:
    for route in public_config.document["routing"]:
        if route.get("network") == network and route.get("capability") == capability:
            ids = route.get("source_ids") or []
            return ids[0] if ids else None
    return None


def _routed_has(public_config, network: str, source_id: str) -> bool:
    for route in public_config.document["routing"]:
        if route.get("network") == network and source_id in (route.get("source_ids") or []):
            return True
    return False


def _head(transport, source_id: str) -> int | None:
    try:
        result = transport.request(source_id, evm_rpc("eth_blockNumber", []))
    except Exception:
        return None
    if not getattr(result, "ok", False) or not isinstance(result.body, dict):
        return None
    return hex_quantity(result.body.get("result"))


def _probe_chain(transport, source_id: str, network: str | None, registry) -> dict[str, Any]:
    if network is None or network not in registry:
        return {"probe": "untested", "status": "blocked", "reason": "unknown_network"}
    expected = registry[network].get("evm_chain_id")
    try:
        result = transport.request(source_id, chain_id_operation())
    except Exception:
        return {"probe": "untested", "status": "blocked", "reason": "identity_unreadable"}
    got = hex_quantity(result.body.get("result")) if getattr(result, "ok", False) and isinstance(result.body, dict) else None
    if got is None:
        return {"probe": "untested", "status": "blocked", "reason": "identity_unreadable"}
    return {
        "expected_chain_id": expected,
        "observed_chain_id": got,
        "probe": "untested",
        "status": "matched" if got == expected else "mismatch",
    }


def _probe_genesis(transport, source_id: str, network: str | None, registry) -> dict[str, Any]:
    if network is None or network not in registry:
        return {"probe": "untested", "status": "blocked", "reason": "unknown_network"}
    identity = registry[network].get("identity")
    expected = identity.get("expected_value") if isinstance(identity, dict) else None
    try:
        result = transport.request(source_id, genesis_hash_operation())
    except Exception:
        return {"probe": "untested", "status": "blocked", "reason": "identity_unreadable"}
    got = result.body.get("result") if getattr(result, "ok", False) and isinstance(result.body, dict) else None
    if not isinstance(got, str) or not got:
        return {"probe": "untested", "status": "blocked", "reason": "identity_unreadable"}
    return {
        "probe": "untested",
        "status": "matched" if got == expected else "mismatch",
    }


def _network_of(source_id: str, registry: Mapping[str, Any]) -> str | None:
    if source_id.startswith("rpc-"):
        network = source_id[4:]
        if network in registry:
            return network
    return None


def _block_caps(wallet_id, network, observations, capabilities, reason: str) -> None:
    for capability in capabilities:
        observations[(wallet_id, network, capability)] = _blocked(reason)


def _block_family(wallet_id, network, observations, reason: str) -> None:
    observations[(wallet_id, network, "balances.native")] = _blocked(reason)


def _blocked(reason: str) -> CoverageObservation:
    return CoverageObservation(
        category="error",
        pagination_complete=False,
        gap_detection_complete=False,
        declared="not_read",
        reason_codes=(reason, "blocked"),
        trust="unverified",
        price_quality="unknown",
    )


def _unsupported() -> CoverageObservation:
    return CoverageObservation(
        category="empty",
        pagination_complete=False,
        gap_detection_complete=False,
        declared="unsupported",
        reason_codes=("unsupported",),
        trust="unverified",
    )


def _baseline(wallet_id, network, capability, seen, baseline, candidates, tenant) -> None:
    scope = (wallet_id, network, capability)
    if scope in seen or scope in baseline:
        return
    baseline.append(scope)
    candidates.append(Candidate(
        tenant_id=tenant,
        wallet_id=wallet_id,
        network=network,
        capability=capability,
        identity="baseline",
        kind="baseline",
    ))


def _seen(database: Database, tenant: str) -> set[tuple[str, str, str]]:
    found = set()
    for row in database.capability_state_for(tenant):
        if row.get("last_success_at"):
            found.add((row["wallet_id"], row["network"], row["capability"]))
    return found


def _cursors(observations: Mapping[tuple[str, str, str], CoverageObservation]) -> list[dict[str, Any]]:
    rows = []
    for (wallet_id, network, capability), observation in observations.items():
        if observation.category not in {"ok", "empty"}:
            continue
        if not observation.pagination_complete or not observation.gap_detection_complete:
            continue
        if not observation.cursor:
            continue
        rows.append({
            "block_number": observation.checkpoint_number,
            "capability": capability,
            "cursor": observation.cursor,
            "network": network,
            "wallet_id": wallet_id,
        })
    return rows


def _incoming(native_reads: list[dict[str, str]], registry) -> list[dict[str, Any]]:
    items = []
    for read in native_reads:
        entry = registry[read["network"]]
        chain_id = entry.get("evm_chain_id")
        if not isinstance(chain_id, int):
            chain_id = read["network"]
        items.append({
            "capability": "balances.native",
            "chain_id": chain_id,
            "network": read["network"],
            "position_id": f"{read['network']}:native",
            "raw_amount": read["raw_amount"],
            "wallet_id": read["wallet_id"],
        })
    return items


def _result_map(native_reads: list[dict[str, str]], registry) -> dict[tuple[Any, Any, str], str]:
    results = {}
    for read in native_reads:
        entry = registry[read["network"]]
        chain_id = entry.get("evm_chain_id")
        if not isinstance(chain_id, int):
            chain_id = read["network"]
        results[(read["wallet_id"], chain_id, "balances.native")] = "ok"
    return results


def enabled_pairs(enabled: list[tuple[Any, str]]) -> list[tuple[str, str]]:
    return [(account.wallet_id, network) for account, network in enabled]


def _required_value(row: Mapping[str, Any]) -> str:
    reasons = row.get("reason_codes") or []
    if "blocked" in reasons or "credential_missing" in reasons:
        return "blocked"
    quality = str(row.get("quality") or "unknown")
    return quality


def _req_key(wallet_id: str, network: str, capability: str) -> str:
    return f"{wallet_id}|{network}|{capability}"


def _raw_int(value: Any) -> int | None:
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    if isinstance(value, str) and value.isdigit():
        return int(value)
    return None


def _iso(moment: float) -> str:
    return datetime.fromtimestamp(moment, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
