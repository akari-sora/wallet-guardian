"""One offline-capable read of the accounts in a public monitor profile.

The backend is injected. Tests pass a fixture backend. The CLI default is the
HTTP backend. This module does not sign, encode, or broadcast a transaction.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any
from uuid import uuid4

from guardian.chains.evm.accounting import TRANSFER_TOPIC, account_logs, pad_address
from guardian.chains.evm.adapter import LOG_RANGE_MAX, EvmAdapter, chain_id_operation, evm_rpc, hex_quantity, to_observation
from guardian.chains.evm.permissions import APPROVAL_LOG_TOPICS, _PERMISSION_CAPABILITIES, approval_log_operation, scan_approvals
from guardian.chains.evm.permits import PERMIT2_PERMIT_TOPIC
from guardian.chains.solana.adapter import SolanaAdapter, genesis_hash_operation
from guardian.configuration import (
    AccountsDocument,
    PrivateInputs,
    PublicConfig,
    _env_path,
    _load_secret_profile,
)
from guardian.coverage import CoverageObservation, build_coverage, resolve_support
from guardian.sources.key_guide import credential_hint, hint_for_source
from guardian.delivery import CANARY_STATUS, deliver, load_handoff
from guardian.events import ChainEvent
from guardian.errors import GuardianError, SecurityError
from guardian.monitoring import CapabilityCycle
from guardian.network_registry import load_registry
from guardian.news.exposure import (
    ExposureNotice,
    WalletExposure,
    exposed_protocols,
    exposure_alert,
    link_exposure,
    load_caller_incidents,
)
from guardian.news.fetch import FetchReport, fetch_allowlisted_news, news_state_row
from guardian.protocols.kinza_cache import KinzaMetadataCache
from guardian.protocols.models import unknown_position
from guardian.protocols.registry import PLUGIN_REGISTRY
from guardian.protocols.runner import run_plugin
from guardian.protocols.uxlink import AssetManifestError, CallerQuote, parse_price_input, public_leg
from guardian.rules import AlertPolicy, Candidate
from guardian.runtime_binding import assert_worktree
from guardian.scan.block_time import lookback_blocks
from guardian.scan.window import plan_chunks, read_logs, reject_wallet_emitter, window_covers
from guardian.security.transport_policy import Operation
from guardian.sources.blockscout import BlockscoutClient
from guardian.sources.budget import BudgetBook, BudgetExhausted
from guardian.sources.esplora import bitcoin_gap_observations, read_bitcoin_address
from guardian.resources import resource_path
from guardian.sources.capabilities import load_source_capabilities
from guardian.sources.etherscan import EtherscanClient
from guardian.sources.nodereal import NodeRealClient
from guardian.sources.plan_cache import PlanFailureCache, database_file
from guardian.sources.provenance import CallLedger
from guardian.sources.transport import HttpxBackend, ReadOnlyTransport, TransportError, build_endpoint_registry
from guardian.storage import Database

Clock = Callable[[], float]
_APPLICABLE = frozenset({"implemented", "partial"})
_CURSOR_OVERLAP = 2
_INCLUSION_LAG = 12
_INDEXER_ADAPTERS = frozenset({"alchemy_transfers", "etherscan_v2", "nodereal_transfers", "blockscout"})
_FALLBACK_INDEXERS = {
    "base-mainnet": "blockscout",
    "bsc-mainnet": "nodereal_transfers",
    "optimism-mainnet": "blockscout",
}
_APPROVAL_ALERTS = frozenset({"first_observed", "increased", "new"})
_PLUGIN_IDS = {
    "aave-v3": "aave_v3",
    "aave_v3": "aave_v3",
    "erc4626": "erc4626",
    "erc4626-registered": "erc4626",
    "lista-slisbnb": "lista_slisbnb",
    "lista_slisbnb": "lista_slisbnb",
    "kinza": "kinza",
    "lido": "lido",
    "solana-stake": "solana_stake",
    "solana_stake": "solana_stake",
    "uxlink-holdings": "uxlink_holdings",
    "uxlink_holdings": "uxlink_holdings",
}
_PLUGIN_NEWS = {
    "aave-v3": "aave",
    "aave_v3": "aave",
    "kinza": "kinza",
    "lido": "lido",
    "lista-slisbnb": "lista",
    "lista_slisbnb": "lista",
    "solana-stake": "solana",
    "solana_stake": "solana",
    "uxlink-holdings": "uxlink",
    "uxlink_holdings": "uxlink",
}
_CANON_NEWS = {
    "aave_v3": "aave",
    "kinza": "kinza",
    "lido": "lido",
    "lista_slisbnb": "lista",
    "solana_stake": "solana",
    "uxlink": "uxlink",
}


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
    positions: Any
    required: dict[str, str]
    native_reads: list[dict[str, str]]
    delivery_status: str
    misconfigured: bool
    baseline: bool = False
    budget: dict[str, Any] | None = None
    checkpoints: list[dict[str, Any]] | None = None
    log_evidence: list[dict[str, Any]] | None = None
    permission_notes: list[dict[str, Any]] | None = None
    runtime: dict[str, Any] | None = None
    indexer_disclosures: list[dict[str, Any]] | None = None
    egress: dict[str, Any] | None = None
    provider_message_ids: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "baseline": self.baseline,
            "budget": self.budget or {},
            "canary": CANARY_STATUS,
            "checkpoints": self.checkpoints or [],
            "coverage": self.coverage,
            "decisions": self.decisions,
            "delivery_status": self.delivery_status,
            "egress": self.egress or {},
            "finished": self.finished,
            "indexer_disclosures": self.indexer_disclosures or [],
            "live_pass": False,
            "live_verified": False,
            "log_evidence": self.log_evidence or [],
            "misconfigured": self.misconfigured,
            "native_reads": self.native_reads,
            "outbox_written": self.outbox_written,
            "permission_notes": self.permission_notes or [],
            "positions": self.positions,
            "provider_message_ids": list(self.provider_message_ids),
            "required": self.required,
            "run_id": self.run_id,
            "runtime": self.runtime or {},
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
    news_input: bytes | None = None,
    price_input: bytes | None = None,
    notification_backend: Any = None,
    notification_resolve: Any = None,
) -> RunReport:
    """Read configured wallets and commit one capability cycle."""
    runtime = assert_worktree()
    started_at = clock()
    started = _iso(started_at)
    tenant = accounts.tenant_id
    database.upsert_tenant(tenant, tenant)
    transport = _transport(public_config, backend, clock)
    ledger = CallLedger()
    transport.provenance = ledger
    disclosures: list[dict[str, Any]] = []
    transport.indexer_disclosures = disclosures
    kinza_cache = KinzaMetadataCache()
    plan_cache = _plan_cache(database, clock)
    checkpoints: list[dict[str, Any]] = []
    log_evidence: list[dict[str, Any]] = []
    permission_notes: list[dict[str, Any]] = []
    polling = public_config.document.get("polling") if isinstance(public_config.document.get("polling"), dict) else {}
    transport.bootstrap_request_cap = _bootstrap_request_cap(polling)
    transport.bootstrap_requests = {}
    bootstrap_origin = polling.get("bootstrap_origin") if isinstance(polling.get("bootstrap_origin"), int) and not isinstance(polling.get("bootstrap_origin"), bool) else None
    deadline = polling.get("round_deadline_seconds") if isinstance(polling.get("round_deadline_seconds"), int) else None
    registry = load_registry(resource_path("config", "chains"))
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
    stored_cursors = _stored_cursors(database, tenant)
    position_prior = _load_position_state(database, tenant)
    approval_state = _load_approval_state(database, tenant)
    previous_native = _previous_native(database, tenant)
    events: list[ChainEvent] = []
    seen_transfers: set[tuple[str, ...]] = set()
    explicit_cursors: list[dict[str, Any]] = []
    held_cursors: set[tuple[str, str, str]] = set()
    position_state: dict[tuple[str, str], list[dict[str, Any]]] = {}
    position_summaries: list[dict[str, Any]] = []
    position_rows: list[Any] = []
    position_labels: dict[str, list[dict[str, Any]]] = {}
    news_book = None
    news_reason = "news_not_supplied"
    if news_input is not None:
        news_book, news_reason = load_caller_incidents(news_input)
    fetched = None
    if _news_fetch_enabled(public_config):
        fetched = fetch_allowlisted_news(
            database, tenant, backend, resolve=_resolve, now=started, clock=clock,
        )
    quotes = _price_quotes(price_input)
    news_coverage = _news_observation(news_input is not None, news_reason)
    peers: dict[str, list[tuple[str, str]]] = {}
    for account, network in enabled:
        peers.setdefault(network, []).append((account.wallet_id, account.normalized.canonical))
    log_done: set[str] = set()
    for account, network in enabled:
        entry = registry[network]
        family = entry.get("family")
        if family == "evm":
            _read_evm(
                public_config, private_inputs, transport, account, network, entry,
                observations, native_reads, seen, baseline, candidates, tenant,
                stored_cursors=stored_cursors,
                previous_native=previous_native,
                events=events,
                seen_transfers=seen_transfers,
                explicit_cursors=explicit_cursors,
                held_cursors=held_cursors,
                started=started,
                active_approvals=approval_state,
                database=database,
                clock=clock,
                started_at=started_at,
                peers=peers.get(network, []),
                log_done=log_done,
                plan_cache=plan_cache,
                checkpoints=checkpoints,
                log_evidence=log_evidence,
                permission_notes=permission_notes,
                bootstrap_origin=bootstrap_origin,
                deadline=deadline,
            )
        elif family == "solana":
            _read_solana(
                public_config, private_inputs, transport, account, network, entry,
                observations, native_reads, seen, baseline, candidates, tenant, started,
            )
        elif family == "bitcoin":
            _read_bitcoin(
                public_config, private_inputs, transport, account, network, entry,
                observations, native_reads, seen, baseline, candidates, tenant, started,
                events, explicit_cursors, held_cursors,
            )
        else:
            _block_family(account.wallet_id, network, observations, "unsupported_family")
        _read_positions(
            public_config, private_inputs, transport, account, network, entry, clock,
            observations, candidates, position_prior, position_state, position_summaries, tenant, started,
            position_rows=position_rows,
            position_labels=position_labels,
            quotes=quotes,
            kinza_cache=kinza_cache,
        )
        observations[(account.wallet_id, network, "exposure_news")] = news_coverage
        _fill_gaps(public_config, private_inputs, catalog, account.wallet_id, network, observations, transport)
    if fetched is not None:
        _apply_fetched_coverage(
            observations, enabled, fetched, public_config, position_rows, approval_state,
            news_input is not None, news_reason,
        )
    rows = build_coverage(enabled_pairs(enabled), observations, catalog, registry, started)
    required = _required_map(enabled, rows, observations, position_summaries, public_config, private_inputs, registry)
    decisions = policy.apply(candidates, baseline_scopes=baseline)
    alerts = []
    for item in decisions:
        if not item.alert:
            continue
        payload = item.as_dict()
        labels = position_labels.get(item.identity)
        if labels:
            payload["labels"] = labels
        alerts.append(payload)
    if fetched is not None or news_book is not None:
        active_approvals = _active_from_state(approval_state, enabled)
        wallets = _wallet_exposures(position_rows, active_approvals)
        if fetched is not None:
            # Only incidents that are new or revised in this fetch can notify; a
            # feed's first fetch is a quiet baseline apart from recent items.
            fresh = [item for item in fetched.book.incidents if item.incident_id in fetched.linkable]
            for notice in link_exposure(fresh, wallets):
                alerts.append(_fetched_alert(tenant, notice, position_rows, active_approvals, fetched))
        if news_book is not None:
            for notice in link_exposure(news_book.incidents, wallets):
                alerts.append(exposure_alert(tenant, notice, _notice_network(notice.wallet_id, position_rows, active_approvals)))
    cursors = _merge_cursors(observations, explicit_cursors, held_cursors)
    cycle = CapabilityCycle(database, tenant)
    capability_rows = _storage_rows(rows, position_state, approval_state)
    if fetched is not None:
        capability_rows.append(news_state_row(fetched.state, started))
    finished_cycle = cycle.finish(
        incoming=_incoming(native_reads, registry),
        results=_result_map(native_reads, registry),
        events=events,
        capability_rows=capability_rows,
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
    active_sender = sender
    if (
        not no_send
        and active_sender is None
        and handoff.channel == "telegram"
        and handoff.opt_in
        and handoff.reason == "ready"
    ):
        from guardian.senders.telegram import TelegramSender

        active_sender = TelegramSender.from_public(
            public_config,
            os.environ,
            backend=notification_backend,
            resolve=notification_resolve,
        )
    delivery = deliver(
        database,
        tenant,
        mode="no_send" if no_send else "send",
        sender=active_sender,
        handoff=handoff,
        clock=clock,
    )
    unknown = [
        _req_key(row["wallet_id"], row["network"], row["capability"])
        for row in rows
        if row.get("price_quality") == "unknown" and row.get("support") in _APPLICABLE and row["capability"] == "balances.native"
    ]
    for item in decisions:
        if item.valuation_unknown and item.identity not in unknown:
            unknown.append(item.identity)
    for summary in position_summaries:
        for leg in summary.get("legs") or []:
            if not isinstance(leg, dict) or leg.get("valuation") != "valuation_unknown":
                continue
            identity = leg.get("identity")
            if isinstance(identity, str) and identity not in unknown:
                unknown.append(identity)
    run_id = f"mcm-{int(started_at)}-{uuid4().hex[:8]}"
    provenance = ledger.as_dict()
    report = RunReport(
        run_id=run_id,
        started=started,
        finished=finished,
        status=status,
        coverage=rows,
        decisions=[item.as_dict() for item in decisions if item.alert or item.valuation_unknown],
        outbox_written=len(emitted),
        sent=0 if no_send else delivery.sent,
        valuation_unknown=unknown,
        positions=position_summaries,
        required=required,
        native_reads=native_reads,
        delivery_status=delivery.status,
        misconfigured=delivery.misconfigured,
        baseline=bool(baseline),
        budget=_budget_evidence(provenance, kinza_cache, transport),
        egress=provenance.get("egress"),
        checkpoints=checkpoints,
        log_evidence=log_evidence,
        permission_notes=permission_notes,
        runtime=runtime,
        indexer_disclosures=disclosures,
        provider_message_ids=delivery.message_ids,
    )
    database.save_run(tenant, run_id, {
        "baseline": report.baseline,
        "finished": finished,
        "live_pass": False,
        "live_verified": False,
        "required": required,
        "run_id": run_id,
        "started": started,
        "status": status,
    })
    return report


def probe_sources(
    public_config: PublicConfig,
    private_inputs: PrivateInputs,
    *,
    backend: Any,
    clock: Clock,
) -> dict[str, Any]:
    """Per-source configuration and, for RPC, a chain-id or genesis read."""
    transport = _transport(public_config, backend, clock)
    registry = load_registry(resource_path("config", "chains"))
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
        row = {
            "adapter": source.get("adapter"),
            "configured": True,
            "credential_present": present,
            "credential_required": credential_required,
            "enabled": enabled,
            "id": name,
            "identity": identity,
        }
        hint = _credential_hint(source, blocked if isinstance(blocked, str) else None)
        if hint:
            row["credential_hint"] = hint
        sources.append(row)
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


_BUDGET_WAIT_MAX_SECONDS = 10.0


def _budget_sleeper(backend: Any):
    """Really wait for the max_rps token bucket on a live backend.

    A test backend may carry a ``budget_sleeper``. Any other fake backend does not wait.
    Without this the local limiter never slowed anything down and the provider
    answered 429, which an identity read then reported as ``identity_unreadable``.
    """
    sleeper = getattr(backend, "budget_sleeper", None)
    if callable(sleeper):
        return sleeper
    if isinstance(backend, HttpxBackend):
        import time

        return lambda seconds: time.sleep(min(max(float(seconds), 0.0), _BUDGET_WAIT_MAX_SECONDS))
    return lambda _seconds: None


def _transport(public_config: PublicConfig, backend: Any, clock: Clock) -> ReadOnlyTransport:
    from guardian.sources.resolver import HostnameResolver, load_resolver_settings

    secrets = _secrets(public_config)
    book = BudgetBook.from_document(public_config.document["budgets"], clock=clock, sleep=_budget_sleeper(backend))
    endpoints = build_endpoint_registry(public_config.document, secrets=secrets, environ=os.environ)
    resolver = HostnameResolver(load_resolver_settings(public_config.document, os.environ))

    def resolve(host: str) -> list[str]:
        name = host.lower().rstrip(".")
        if name.endswith(".invalid"):
            return ["1.1.1.1"]
        return resolver.resolve(name)

    transport = ReadOnlyTransport(endpoints, book, backend, resolve=resolve)
    transport.egress_resolver = resolver
    return transport


def _secrets(public_config: PublicConfig) -> dict[str, str]:
    path = _env_path(os.environ, public_config.document["private_inputs"]["secret_profile_file_env"])
    return _load_secret_profile(path)


def _resolve(host: str) -> list[str]:
    """``.invalid`` stays a fixture address. Every other name goes through the resolver."""
    name = host.lower().rstrip(".")
    if name.endswith(".invalid"):
        return ["1.1.1.1"]
    from guardian.sources.resolver import HostnameResolver, load_resolver_settings

    return HostnameResolver(load_resolver_settings(None, os.environ)).resolve(name)


def _price_quotes(price_input: bytes | None) -> tuple[CallerQuote, ...]:
    if price_input is None:
        return ()
    try:
        return parse_price_input(price_input)
    except AssetManifestError as exc:
        raise GuardianError("price input is not readable") from exc


def _news_fetch_enabled(public_config: PublicConfig) -> bool:
    """Public config opts in. Absent or false keeps the caller-supplied path unchanged."""
    news = public_config.document.get("news")
    return isinstance(news, dict) and news.get("fetch") is True


def _apply_fetched_coverage(
    observations,
    enabled,
    fetched: FetchReport,
    public_config,
    position_rows,
    approval_state,
    supplied: bool,
    caller_reason: str,
) -> None:
    plugins = [item for item in public_config.document.get("plugins") or [] if isinstance(item, str)]
    active = _active_from_state(approval_state, enabled)
    by_wallet = {item.wallet_id: item for item in _wallet_exposures(position_rows, active)}
    scoped: dict[str, set[str]] = {}
    for account, _network in enabled:
        if account.wallet_id not in scoped:
            scoped[account.wallet_id] = _scope_news(plugins, by_wallet.get(account.wallet_id))
    for account, network in enabled:
        gaps = tuple(sorted(scoped[account.wallet_id] - set(fetched.fetchable)))
        observations[(account.wallet_id, network, "exposure_news")] = _fetched_observation(
            fetched, gaps, supplied, caller_reason,
        )


def _scope_news(plugins: Sequence[str], exposure: WalletExposure | None) -> set[str]:
    """Protocols the wallet is exposed to, plus protocols of enabled plugins."""
    found: set[str] = set()
    for plugin in plugins:
        name = _PLUGIN_NEWS.get(plugin)
        if name:
            found.add(name)
    if exposure is None:
        return found
    for protocol in exposed_protocols(exposure):
        name = _CANON_NEWS.get(protocol)
        if name:
            found.add(name)
    return found


def _fetched_observation(
    fetched: FetchReport,
    gaps: tuple[str, ...],
    supplied: bool,
    caller_reason: str,
) -> CoverageObservation:
    """Fetched news is allowlisted_get. A missing feed or a failed read stays a gap."""
    reasons = ["allowlisted_get"]
    sources = ["allowlisted_get"]
    if supplied:
        sources.append("caller_supplied")
        reasons.append("caller_supplied")
        reasons.append(caller_reason or "not_fetched")
    statuses = {feed.status for feed in fetched.feeds}
    if "not_modified" in statuses:
        reasons.append("not_modified")
    if "fetch_error" in statuses:
        reasons.append("fetch_error")
    if "budget_exhausted" in statuses:
        reasons.append("budget_exhausted")
    if gaps:
        reasons.append("no_allowlisted_feed")
        reasons.extend(gaps)
    failed = frozenset({"budget_exhausted", "fetch_error"})
    all_failed = bool(fetched.feeds) and statuses <= failed
    category = "error" if all_failed or (supplied and bool(caller_reason)) else "partial"
    return CoverageObservation(
        category=category,
        pagination_complete=False,
        gap_detection_complete=False,
        gaps=gaps,
        declared="caller_supplied" if supplied and not fetched.feeds else "allowlisted_get",
        sources=tuple(sources),
        reason_codes=tuple(dict.fromkeys(reasons)),
        trust="unverified",
        price_quality="unknown",
        evidence_refs=tuple(f"{feed.source_id}:{feed.status}" for feed in fetched.feeds),
    )


def _fetched_alert(tenant, notice, position_rows, approvals, fetched: FetchReport):
    if notice.kind == "exposure" and notice.incident_id in fetched.updated_ids:
        notice = ExposureNotice(
            wallet_id=notice.wallet_id,
            kind="exposure_update",
            protocols=notice.protocols,
            incident_id=notice.incident_id,
            loss_alert=False,
            via=notice.via,
            content_hash=fetched.content_hashes.get(notice.incident_id or "", ""),
        )
    return exposure_alert(
        tenant,
        notice,
        _notice_network(notice.wallet_id, position_rows, approvals),
        source="allowlisted_get",
    )


def _news_observation(supplied: bool, reason: str) -> CoverageObservation:
    """Caller news is a partial input. A missing file stays an explicit gap."""
    if not supplied:
        return CoverageObservation(
            category="partial",
            pagination_complete=False,
            gap_detection_complete=False,
            declared="not_supplied",
            reason_codes=("news_not_supplied",),
            trust="unverified",
            price_quality="unknown",
        )
    if reason:
        return CoverageObservation(
            category="error",
            pagination_complete=False,
            gap_detection_complete=False,
            declared="caller_supplied",
            sources=("caller_supplied",),
            reason_codes=("caller_supplied", reason),
            trust="unverified",
            price_quality="unknown",
        )
    return CoverageObservation(
        category="partial",
        pagination_complete=False,
        gap_detection_complete=False,
        declared="caller_supplied",
        sources=("caller_supplied",),
        reason_codes=("caller_supplied", "not_fetched"),
        trust="unverified",
        price_quality="unknown",
    )


def _wallet_exposures(observations: Sequence[Any], approvals: Sequence[Mapping[str, Any]]) -> tuple[WalletExposure, ...]:
    by_wallet: dict[str, list[Any]] = {}
    for item in observations:
        by_wallet.setdefault(item.wallet_id, []).append(item)
    approval_by: dict[str, list[Mapping[str, Any]]] = {}
    for row in approvals:
        wallet_id = row.get("wallet_id")
        if isinstance(wallet_id, str):
            approval_by.setdefault(wallet_id, []).append(row)
    wallets = []
    for wallet_id in sorted(set(by_wallet) | set(approval_by)):
        wallets.append(WalletExposure(
            wallet_id,
            positions=tuple(by_wallet.get(wallet_id, ())),
            approvals=tuple(approval_by.get(wallet_id, ())),
        ))
    return tuple(wallets)


def _notice_network(wallet_id: str, observations: Sequence[Any], approvals: Sequence[Mapping[str, Any]]) -> str:
    found = [item.network for item in observations if item.wallet_id == wallet_id and isinstance(item.network, str)]
    found.extend(
        row["network"] for row in approvals
        if row.get("wallet_id") == wallet_id and isinstance(row.get("network"), str)
    )
    if not found:
        return "unspecified"
    return sorted(set(found))[0]


def _read_evm(
    public_config, private_inputs, transport, account, network, entry, observations, native_reads,
    seen, baseline, candidates, tenant, *, stored_cursors, previous_native, events, seen_transfers,
    explicit_cursors, held_cursors, started, active_approvals, database, clock, started_at, peers,
    log_done, plan_cache, checkpoints, log_evidence, permission_notes, bootstrap_origin, deadline,
) -> None:
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
    address = account.normalized.canonical
    balance = session.native_balance(address)
    if balance.observation is not None:
        observations[(wallet_id, network, "balances.native")] = balance.observation
    if balance.raw_wei:
        native_reads.append({"network": network, "raw_amount": balance.raw_wei, "wallet_id": wallet_id})
        _baseline(wallet_id, network, "balances.native", seen, baseline, candidates, tenant)
    head = _head(transport, source_id)
    origin = _effective_bootstrap_origin(public_config, network, head, bootstrap_origin)
    chain_id = entry.get("evm_chain_id") if isinstance(entry.get("evm_chain_id"), int) else network
    native_asset = _native_asset(entry, network)
    checkpoint = _checkpoint(transport, source_id, network, head, str(entry.get("finality_policy") or ""))
    checkpoints.append(checkpoint)
    scan = _Scan(
        public_config, transport, database, tenant, clock, started_at, source_id, network, chain_id,
        native_asset, head, checkpoint, origin, deadline, events, seen_transfers, candidates,
        observations, seen, baseline, explicit_cursors, held_cursors, started, log_evidence, peers,
    )
    fungible_cursor = _cursor_block(stored_cursors, wallet_id, network, "history.fungible")
    _read_token_logs(scan, address, wallet_id, fungible_cursor, log_done)
    approval_cursor = _cursor_block(stored_cursors, wallet_id, network, "permissions.erc20")
    _read_approvals(
        scan, address, wallet_id, approval_cursor, active_approvals, permission_notes,
    )
    external_rows = _history_external(
        scan, public_config, private_inputs, wallet_id, address, stored_cursors, plan_cache,
    )
    _native_delta(
        wallet_id, network, balance.raw_wei, previous_native.get((wallet_id, network)), head, external_rows,
        candidates, tenant, native_asset,
    )


def _read_bitcoin(
    public_config, private_inputs, transport, account, network, entry, observations, native_reads,
    seen, baseline, candidates, tenant, started, events, explicit_cursors, held_cursors,
) -> None:
    """Watch-only Esplora read. Confirmed sats stay out of native_reads when the source did not answer."""
    wallet_id = account.wallet_id
    gaps = bitcoin_gap_observations()
    source_id = _family_source(public_config, private_inputs, network, "esplora")
    if source_id is None:
        _block_caps(wallet_id, network, observations, ("balances.native", "history.native_external"), "endpoint_missing")
        _stamp_gaps(observations, wallet_id, network, gaps)
        return
    try:
        read = read_bitcoin_address(
            transport,
            source_id,
            account.normalized.canonical,
            entry,
            tenant_id=tenant,
            wallet_id=wallet_id,
            observed_at=started,
        )
    except (BudgetExhausted, TransportError, SecurityError) as exc:
        reason = getattr(exc, "reason", "") or "read_error"
        _block_caps(wallet_id, network, observations, ("balances.native", "history.native_external"), reason)
        _stamp_gaps(observations, wallet_id, network, gaps)
        return
    observations[(wallet_id, network, "balances.native")] = read.balance
    observations[(wallet_id, network, "history.native_external")] = read.history
    if read.confirmed_sats is not None:
        native_reads.append({"network": network, "raw_amount": str(read.confirmed_sats), "wallet_id": wallet_id})
        if read.balance.category in {"ok", "empty"}:
            _baseline(wallet_id, network, "balances.native", seen, baseline, candidates, tenant)
    if read.history.category in {"ok", "empty", "partial"}:
        _baseline(wallet_id, network, "history.native_external", seen, baseline, candidates, tenant)
    for event in read.events:
        if any(item.identity == event.identity for item in events):
            continue
        events.append(event)
        if event.finality != "confirmed" or event.tx_status != "success":
            continue
        if event.kind not in {"asset_in", "asset_out"}:
            continue
        candidates.append(Candidate(
            tenant_id=tenant,
            wallet_id=wallet_id,
            network=network,
            capability="history.native_external",
            identity=event.identity,
            kind="asset",
            raw_amount=int(event.raw_amount),
            is_native=True,
            direction="in" if event.kind == "asset_in" else "out",
            tx_id=event.tx_id,
            symbol="BTC",
        ))
    if read.cursor is not None:
        explicit_cursors.append(read.cursor)
        held_cursors.add((wallet_id, network, "history.native_external"))
    _stamp_gaps(observations, wallet_id, network, gaps)


def _stamp_gaps(observations, wallet_id: str, network: str, gaps) -> None:
    for capability, observation in gaps.items():
        observations.setdefault((wallet_id, network, capability), observation)


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


def _history_external(scan, public_config, private_inputs, wallet_id, address, stored_cursors, plan_cache) -> list[dict[str, Any]]:
    """Call a credentialed indexer. A missing key stays blocked and does not read."""
    from guardian.scan.drive import history_external

    del public_config
    stored = _cursor_block(stored_cursors, wallet_id, scan.network, "history.native_external")
    return history_external(scan, private_inputs, wallet_id, address, stored, plan_cache)


def _credential_hint(source: Mapping[str, Any], blocked_reason: str | None) -> str | None:
    return hint_for_source(source, blocked_reason)


def _risk_gap(public_config, private_inputs, privacy: Any) -> CoverageObservation:
    """GoPlus stays uncalled while privacy denies it. A missing key is still credential_missing."""
    reasons = []
    hint = ""
    source_id, source = _adapter_source(public_config, "goplus")
    if source is not None:
        status = private_inputs.source_status.get(source_id)
        if status is not None and status.blocked_reason == "credential_missing":
            reasons.append("credential_missing")
            env_name = source.get("credential_env")
            if isinstance(env_name, str):
                hint = credential_hint(env_name)
    if privacy != "operator_approved_sources_only":
        reasons.append("third_party_denied")
    else:
        reasons.append("not_run")
    return CoverageObservation(
        category="error",
        pagination_complete=False,
        gap_detection_complete=False,
        declared="not_read",
        reason_codes=tuple(reasons + ["blocked"]),
        trust="unverified",
        price_quality="unknown",
        credential_hint=hint,
    )


def _adapter_source(public_config, adapter: str) -> tuple[str | None, Mapping[str, Any] | None]:
    for name, source in public_config.document["sources"].items():
        if source.get("adapter") == adapter:
            return name, source
    return None, None


def _adapter_endpoint(public_config, transport, adapter: str) -> bool:
    if transport is None:
        return False
    name, _source = _adapter_source(public_config, adapter)
    if name is None:
        return False
    try:
        registered = transport.endpoints.get(name)
    except Exception:
        return False
    return bool(getattr(registered, "endpoint", ""))


def _fill_gaps(public_config, private_inputs, catalog, wallet_id, network, observations, transport=None) -> None:
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
            observations[key] = _risk_gap(public_config, private_inputs, privacy)
            continue
        if capability == "sanctions" and support in _APPLICABLE:
            if not _adapter_endpoint(public_config, transport, "ofac_sls"):
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


def _hold_cursor(held: set[tuple[str, str, str]], wallet: str, network: str, capability: str) -> None:
    held.add((wallet, network, capability))


def _advance_cursor(explicit: list[dict[str, Any]], held: set[tuple[str, str, str]], wallet: str, network: str, capability: str, block: int) -> None:
    held.add((wallet, network, capability))
    explicit.append({
        "block_number": str(block),
        "capability": capability,
        "cursor": str(block),
        "network": network,
        "wallet_id": wallet,
    })


def _native_asset(entry: Mapping[str, Any], network: str) -> str:
    native = entry.get("native_asset")
    if isinstance(native, Mapping) and isinstance(native.get("identity"), str):
        return native["identity"]
    return f"native:{network}"


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
    """A stored capability row means that scope already had its quiet baseline."""
    found = set()
    for row in database.capability_state_for(tenant):
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
            "position_id": f"{read['wallet_id']}:{read['network']}:native",
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


def _stored_cursors(database: Database, tenant: str) -> dict[tuple[str, str, str], dict[str, Any]]:
    found = {}
    for row in database.capability_cursors_for(tenant):
        found[(row["wallet_id"], row["network"], row["capability"])] = row
    return found


def _cursor_block(stored: Mapping[tuple[str, str, str], Mapping[str, Any]], wallet: str, network: str, capability: str) -> int | None:
    row = stored.get((wallet, network, capability))
    if not isinstance(row, Mapping):
        return None
    return _raw_int(row.get("block_number")) if _raw_int(row.get("block_number")) is not None else _raw_int(row.get("cursor"))


def _plan_cache(database, clock) -> PlanFailureCache | None:
    file = database_file(database)
    if file is None:
        return None
    return PlanFailureCache(file.parent / "capability-failures.json", clock=clock)


def _bootstrap_request_cap(polling: Mapping[str, Any]) -> int:
    value = polling.get("bootstrap_request_cap")
    if isinstance(value, int) and not isinstance(value, bool) and value >= 1:
        return value
    return 2000


def _effective_bootstrap_origin(public_config, network: str, head: int | None, explicit: int | None) -> int | None:
    """An explicit origin wins, including 0. Otherwise the lookback window ends at head."""
    if isinstance(explicit, int) and not isinstance(explicit, bool):
        return max(0, explicit)
    if not isinstance(head, int) or isinstance(head, bool):
        return None
    polling = public_config.document.get("polling") if isinstance(public_config.document.get("polling"), dict) else {}
    days = polling.get("bootstrap_lookback_days")
    if not isinstance(days, int) or isinstance(days, bool):
        days = 30
    overrides = polling.get("seconds_per_block")
    if not isinstance(overrides, dict):
        overrides = None
    return max(0, head - lookback_blocks(network, days, overrides))


def _budget_evidence(provenance: Mapping[str, Any], kinza_cache, transport) -> dict[str, Any]:
    budget = {**provenance, "kinza": kinza_cache.snapshot()}
    cap = getattr(transport, "bootstrap_request_cap", None)
    used = getattr(transport, "bootstrap_requests", None)
    if isinstance(cap, int) and not isinstance(cap, bool):
        budget["bootstrap_request_cap"] = cap
    if isinstance(used, dict):
        budget["bootstrap_requests"] = {key: value for key, value in used.items() if isinstance(key, str)}
    return budget


def _checkpoint(transport, source_id: str, network: str, head: int | None, policy: str) -> dict[str, Any]:
    found: dict[str, Any] = {
        "hash": None,
        "height": head,
        "network": network,
        "policy": policy or "round_start_head",
        "timestamp": None,
    }
    if head is None:
        return found
    try:
        result = transport.request(source_id, evm_rpc("eth_getBlockByNumber", [hex(head), False]))
    except Exception:
        return found
    body = getattr(result, "body", None)
    block = body.get("result") if isinstance(body, dict) else None
    if not isinstance(block, dict):
        return found
    digest = block.get("hash")
    if isinstance(digest, str) and digest.startswith("0x") and len(digest) == 66:
        found["hash"] = digest
    found["timestamp"] = _flexible_int(block.get("timestamp"))
    number = _flexible_int(block.get("number"))
    if isinstance(number, int):
        found["height"] = number
    return found


def _Scan(
    public_config, transport, database, tenant, clock, started_at, source_id, network, chain_id,
    native_asset, head, checkpoint, bootstrap_origin, deadline, events, seen_transfers, candidates,
    observations, seen, baseline, explicit_cursors, held_cursors, started, log_evidence, peers,
):
    from guardian.scan.drive import ScanContext

    return ScanContext(
        public_config=public_config,
        transport=transport,
        database=database,
        tenant=tenant,
        clock=clock,
        started_at=started_at,
        source_id=source_id,
        network=network,
        chain_id=chain_id,
        native_asset=native_asset,
        head=head,
        checkpoint=checkpoint,
        bootstrap_origin=bootstrap_origin,
        deadline=deadline,
        events=events,
        seen_transfers=seen_transfers,
        candidates=candidates,
        observations=observations,
        seen=seen,
        baseline=baseline,
        explicit_cursors=explicit_cursors,
        held_cursors=held_cursors,
        started=started,
        log_evidence=log_evidence,
        peers=list(peers),
        emit=_emit_transfers,
        baseline_cb=_baseline,
        advance=_advance_cursor,
        hold=_hold_cursor,
        movement_rows=_movement_transfers,
        indexer_rows=_etherscan_transfers,
        remember_approval=_remember_approval,
        overlap=_CURSOR_OVERLAP,
    )


def _read_token_logs(scan, address, wallet_id, stored, log_done) -> None:
    from guardian.scan.drive import read_token_logs

    del address, wallet_id
    read_token_logs(scan, stored, log_done)


def _read_approvals(scan, address, wallet_id, stored, active_approvals, permission_notes) -> None:
    from guardian.scan.drive import read_approvals

    read_approvals(scan, address, wallet_id, stored, active_approvals, permission_notes)


def _remember_approval(scan, wallet_id, permission_scan, active_approvals, _new_events) -> None:
    del _new_events
    seen_rows = getattr(scan, "seen_approvals", None)
    if seen_rows is None:
        seen_rows = set()
        scan.seen_approvals = seen_rows
    merged = {
        (str(item.get("token")), str(item.get("spender"))): dict(item)
        for item in active_approvals.get((wallet_id, scan.network), [])
        if isinstance(item, dict)
    }
    for row in permission_scan.rows:
        pair = (str(row.token).lower(), str(row.spender).lower())
        if row.status == "active":
            merged[pair] = {
                "network": scan.network,
                "spender": pair[1],
                "status": "active",
                "token": pair[0],
                "wallet_id": wallet_id,
            }
        else:
            merged.pop(pair, None)
    active_approvals[(wallet_id, scan.network)] = [merged[key] for key in sorted(merged)]
    for row in permission_scan.rows:
        if row.status != "active":
            continue
        if "unlimited" not in row.flags and row.change not in _APPROVAL_ALERTS:
            continue
        marker = (row.tx_id, row.log_index, row.token, row.spender, row.change)
        if marker in seen_rows:
            continue
        seen_rows.add(marker)
        position = "0" if row.log_index is None else str(row.log_index)
        tx = row.tx_id if isinstance(row.tx_id, str) else ""
        identity = f"{scan.network}:{tx}:{position}:approval:{row.token}:{row.spender}"
        scan.candidates.append(Candidate(
            tenant_id=scan.tenant,
            wallet_id=wallet_id,
            network=scan.network,
            capability=row.capability,
            identity=identity,
            kind="permission",
            raw_amount=_raw_int(row.raw_amount),
            permission_change=row.change,
            prior_known=row.change != "first_observed",
            explicit_risk=True,
            tx_id=tx or None,
        ))
        amount = _raw_int(row.raw_amount)
        if tx and amount is not None:
            _remember_event(
                scan.events,
                tenant_id=scan.tenant,
                wallet_id=wallet_id,
                network=scan.network,
                chain_id=scan.chain_id,
                tx_id=tx,
                sub_index=position,
                kind="approval_set",
                asset=row.token,
                raw_amount=amount,
                block_number=None,
                head=scan.head,
                source_id=scan.source_id,
                observed_at=scan.started,
            )


def _party(value: Any) -> str:
    if isinstance(value, str):
        return value.lower()
    if isinstance(value, dict):
        inner = value.get("hash") or value.get("address")
        if isinstance(inner, str):
            return inner.lower()
    return ""


def _transfer_amount(item: Mapping[str, Any]) -> int | None:
    parsed = _flexible_int(item.get("value"))
    if parsed is not None:
        return parsed
    raw = item.get("rawContract")
    if isinstance(raw, dict):
        return _flexible_int(raw.get("value"))
    return None


def _transfer_block(item: Mapping[str, Any]) -> int | None:
    for key in ("blockNumber", "blockNum", "block_number", "block"):
        parsed = _flexible_int(item.get(key))
        if parsed is not None:
            return parsed
    return None


def _flexible_int(value: Any) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        text = value.strip()
        try:
            if text.startswith(("0x", "0X")):
                return int(text, 16)
            if text.isdigit():
                return int(text)
        except ValueError:
            return None
    return None


def _indexer_page(client: EtherscanClient, chain_id: int, action: str, address: str, start: int, end: int, stored: int | None):
    try:
        return client.account_history(
            chain_id=chain_id,
            action=action,
            address=address,
            start_block=start,
            end_block=end,
            previous_cursor=None if stored is None else str(stored),
            request_cap=8,
        )
    except (BudgetExhausted, TransportError, Exception):
        return None


def _movement_transfers(ledger, wallet_id: str, network: str, source_id: str) -> list[dict[str, Any]]:
    if ledger is None:
        return []
    rows = []
    for movement in ledger.movements:
        if movement.raw_amount is None or movement.direction in {"self", "unknown"}:
            continue
        if movement.direction in {"in", "mint"}:
            kind, direction = "asset_in", "in"
        elif movement.direction in {"out", "burn"}:
            kind, direction = "asset_out", "out"
        else:
            continue
        amount = _raw_int(movement.raw_amount)
        if amount is None:
            continue
        rows.append({
            "asset": movement.token,
            "block": None,
            "capability": "history.fungible",
            "direction": direction,
            "is_native": False,
            "kind": kind,
            "network": network,
            "position": str(movement.log_index),
            "raw_amount": amount,
            "source": source_id,
            "tx": movement.tx_id,
            "wallet_id": wallet_id,
        })
    return rows


def _etherscan_transfers(items, address: str, wallet_id: str, network: str, native_asset: str, native: bool, source_id: str) -> list[dict[str, Any]]:
    owner = address.lower()
    rows = []
    for item in items:
        if not isinstance(item, dict):
            continue
        tx = item.get("hash") or item.get("tx_hash")
        if not isinstance(tx, str) or not tx.startswith("0x") or len(tx) != 66:
            continue
        if str(item.get("isError", "0")) not in {"0", "false", "False"}:
            continue
        amount = _transfer_amount(item)
        if amount is None:
            continue
        sender = _party(item.get("from"))
        receiver = _party(item.get("to"))
        if receiver == owner:
            kind, direction = "asset_in", "in"
        elif sender == owner:
            kind, direction = "asset_out", "out"
        else:
            continue
        if native:
            asset = native_asset
            position = str(item.get("traceId") or item.get("transactionIndex") or item.get("nonce") or "0")
            capability = "history.native_external"
        else:
            token = item.get("contractAddress") or item.get("tokenAddress")
            raw_contract = item.get("rawContract")
            if not isinstance(token, str) and isinstance(raw_contract, dict):
                token = raw_contract.get("address")
            if not isinstance(token, str) or not token.startswith("0x"):
                continue
            asset = token.lower()
            position = str(item.get("logIndex") if item.get("logIndex") not in (None, "") else item.get("transactionIndex") or "0")
            capability = "history.fungible"
        block_hash = item.get("blockHash") if isinstance(item.get("blockHash"), str) else None
        rows.append({
            "asset": asset,
            "block": _transfer_block(item),
            "block_hash": block_hash,
            "capability": capability,
            "direction": direction,
            "is_native": native,
            "kind": kind,
            "network": network,
            "position": position,
            "raw_amount": amount,
            "source": source_id,
            "tx": tx.lower(),
            "wallet_id": wallet_id,
        })
    return rows


def _emit_transfers(rows, candidates, events, seen_transfers, tenant, chain_id, started, head, source_id) -> None:
    for row in rows:
        tx = str(row["tx"]).lower()
        asset = str(row["asset"]).lower()
        key = (row["wallet_id"], row["network"], tx, str(row["position"]), row["kind"], asset)
        if key in seen_transfers:
            continue
        seen_transfers.add(key)
        identity = f"{row['network']}:{tx}:{row['position']}:{row['kind']}:{asset}"
        candidates.append(Candidate(
            tenant_id=tenant,
            wallet_id=row["wallet_id"],
            network=row["network"],
            capability=row["capability"],
            identity=identity,
            kind="asset",
            raw_amount=row["raw_amount"],
            is_native=bool(row["is_native"]),
            direction=row["direction"],
            tx_id=tx,
        ))
        block = row.get("block")
        _remember_event(
            events,
            tenant_id=tenant,
            wallet_id=row["wallet_id"],
            network=row["network"],
            chain_id=chain_id,
            tx_id=tx,
            sub_index=str(row["position"]),
            kind=row["kind"],
            asset=asset,
            raw_amount=row["raw_amount"],
            block_number=None if block is None else str(block),
            block_hash=row.get("block_hash"),
            head=head,
            source_id=row.get("source") or source_id,
            observed_at=started,
        )


def _native_delta(wallet_id, network, raw_wei, previous, head, external_rows, candidates, tenant, native_asset) -> None:
    if any(row.get("is_native") for row in external_rows):
        return
    current = _raw_int(raw_wei)
    if current is None or previous is None or current == previous or head is None:
        return
    direction = "in" if current > previous else "out"
    amount = abs(current - previous)
    candidates.append(Candidate(
        tenant_id=tenant,
        wallet_id=wallet_id,
        network=network,
        capability="balances.native",
        identity=f"native-delta:{network}:{head}:{direction}:{native_asset}",
        kind="asset",
        raw_amount=amount,
        is_native=True,
        direction=direction,
    ))


def _remember_event(events: list[ChainEvent], **kwargs: Any) -> None:
    number = _raw_int(kwargs.get("block_number"))
    head = kwargs.get("head")
    finality = "observed"
    if isinstance(number, int) and isinstance(head, int) and head - number >= _INCLUSION_LAG:
        finality = "confirmed"
    chain_id = kwargs.get("chain_id")
    chain_uid = f"eip155:{chain_id}" if isinstance(chain_id, int) else str(kwargs.get("network"))
    try:
        event = ChainEvent(
            tenant_id=kwargs["tenant_id"],
            wallet_id=kwargs["wallet_id"],
            network=kwargs["network"],
            chain_uid=chain_uid,
            tx_id=kwargs["tx_id"],
            sub_index=str(kwargs["sub_index"]),
            kind=kwargs["kind"],
            asset=kwargs["asset"],
            raw_amount=kwargs["raw_amount"],
            block_number=kwargs.get("block_number"),
            block_hash=kwargs.get("block_hash") if isinstance(kwargs.get("block_hash"), str) else None,
            tx_status="success",
            finality=finality,
            source_ids=(str(kwargs.get("source_id") or "evm-rpc"),),
            observed_at=kwargs["observed_at"],
        )
    except ValueError:
        return
    if any(item.identity == event.identity for item in events):
        return
    events.append(event)


def _read_positions(
    public_config, private_inputs, transport, account, network, entry, clock, observations, candidates,
    position_prior, position_state, position_summaries, tenant, started, *,
    position_rows, position_labels, quotes, kinza_cache=None,
) -> None:
    del started
    family = entry.get("family")
    adapter = "evm_rpc" if family == "evm" else "solana_rpc" if family == "solana" else ""
    source_id = _family_source(public_config, private_inputs, network, adapter) if adapter else None
    sources = {network: source_id} if source_id else {}
    wallet_id = account.wallet_id
    merged_reasons: list[str] = []
    any_unknown = False
    for public_id in public_config.document.get("plugins") or []:
        plugin_id = _PLUGIN_IDS.get(public_id)
        plugin = None if plugin_id is None else PLUGIN_REGISTRY.get(plugin_id)
        if plugin is None or network not in plugin.networks or adapter not in plugin.adapters:
            continue
        prior = position_prior.get((wallet_id, network, plugin_id))
        plugin_transport = transport
        if plugin_id == "kinza" and kinza_cache is not None:
            plugin_transport = kinza_cache.wrap(transport)
        if not source_id:
            observation = unknown_position(
                plugin_id=plugin_id, wallet_id=wallet_id, network=network, prior=prior, reason="missing_source",
            )
        else:
            try:
                observation = run_plugin(
                    plugin_id,
                    transport=plugin_transport,
                    sources=sources,
                    wallet_id=wallet_id,
                    wallet=account.normalized.canonical,
                    network=network,
                    clock=int(clock()),
                    prior=prior if isinstance(prior, dict) else None,
                )
            except (BudgetExhausted, TransportError, Exception):
                observation = unknown_position(
                    plugin_id=plugin_id, wallet_id=wallet_id, network=network, prior=prior,
                    reason="read_failed", detail="plugin_error",
                )
        health = "unknown" if observation.status == "unknown" else "complete_in_declared_scope"
        reason = observation.reason_codes[0] if observation.reason_codes else ""
        cell_required = True
        if plugin_id == "erc4626" and observation.status == "unknown" and "vaults_not_configured" in observation.reason_codes:
            if not _prior_position(prior):
                cell_required = False
        legs = [public_leg(leg, quotes) for leg in observation.legs]
        position_rows.append(observation)
        position_summaries.append({
            "health": health,
            "legs": legs,
            "network": network,
            "plugin_id": plugin_id,
            "public_id": public_id,
            "reason": reason,
            "reason_codes": list(observation.reason_codes),
            "required": cell_required,
            "status": observation.status,
            "wallet_id": wallet_id,
        })
        change = _position_change(prior, observation)
        if isinstance(prior, dict) and (
            observation.status == "unknown"
            or (change is None and prior.get("status") == observation.status)
        ):
            stored = dict(prior)
        else:
            stored = _position_record(plugin_id, observation)
        position_state.setdefault((wallet_id, network), []).append(stored)
        if change:
            identity = f"position:{plugin_id}:{change}:{_position_fingerprint(observation)}"
            candidates.append(Candidate(
                tenant_id=tenant,
                wallet_id=wallet_id,
                network=network,
                capability="positions",
                identity=identity,
                kind="position",
                explicit_risk=True,
            ))
            labeled = [leg for leg in legs if leg.get("labels")]
            if labeled:
                position_labels[identity] = labeled
        merged_reasons.extend(observation.reason_codes)
        any_unknown = any_unknown or observation.status == "unknown"
    if (wallet_id, network) in position_state:
        observations[(wallet_id, network, "positions")] = CoverageObservation(
            category="partial",
            pagination_complete=not any_unknown,
            gap_detection_complete=not any_unknown,
            reason_codes=tuple(dict.fromkeys(merged_reasons)),
            declared="protocol_plugin",
            trust="unverified",
            price_quality="unknown",
        )


def _prior_position(prior: Any) -> bool:
    if not isinstance(prior, dict):
        return False
    if prior.get("status") == "observed":
        return True
    legs = prior.get("legs")
    return isinstance(legs, list) and bool(legs)


def _position_record(plugin_id: str, observation) -> dict[str, Any]:
    return {
        "legs": [
            {"identity": leg.identity, "kind": leg.kind, "raw_amount": leg.raw_amount}
            for leg in observation.legs
        ],
        "metrics": {key: value for key, value in observation.metrics},
        "plugin_id": plugin_id,
        "reason_codes": list(observation.reason_codes),
        "status": observation.status,
    }


def _position_fingerprint(observation) -> str:
    parts = [observation.status]
    for key, value in observation.metrics:
        if key in {"collateral_base", "debt_base", "health_factor"}:
            parts.append(f"{key}={value}")
    for leg in observation.legs:
        parts.append(f"{leg.kind}={leg.identity}={leg.raw_amount}")
    return "|".join(parts)


_POSITION_RELATIVE = Decimal("0.10")
_HEALTH_BANDS = (Decimal("1.1"), Decimal("1.25"), Decimal("1.5"), Decimal("2"))
_WAD = Decimal(10) ** 18


def _position_change(prior: Any, observation) -> str | None:
    """Material change only. Interest drift inside the bands is not an event.

    The stored prior is the last reference state (baseline or last alert), so slow drift
    still alerts once it adds up to a material change.
    """
    if observation.status == "unknown":
        return None
    if not isinstance(prior, dict) or prior.get("status") in {None, "unknown"}:
        return None
    old_status = prior.get("status")
    if old_status == "none_observed" and observation.status == "observed":
        return "appeared"
    if old_status == "observed" and observation.status == "none_observed":
        return "disappeared"
    old_metrics = prior.get("metrics") if isinstance(prior.get("metrics"), dict) else {}
    new_metrics = {key: value for key, value in observation.metrics}
    for key in ("collateral_base", "debt_base"):
        if _material(old_metrics.get(key), new_metrics.get(key)):
            return "changed"
    if _health_band(old_metrics.get("health_factor")) != _health_band(new_metrics.get("health_factor")):
        return "changed"
    old_legs = {
        (item.get("kind"), item.get("identity")): item.get("raw_amount")
        for item in prior.get("legs") or []
        if isinstance(item, dict)
    }
    new_legs = {(leg.kind, leg.identity): leg.raw_amount for leg in observation.legs}
    if set(old_legs) != set(new_legs):
        return "changed"
    for key, value in new_legs.items():
        if _material(old_legs.get(key), value):
            return "changed"
    return None


def _material(old: Any, new: Any) -> bool:
    left = _decimal_or_none(old)
    right = _decimal_or_none(new)
    if left is None and right is None:
        return False
    if left is None or right is None:
        return True
    if left == 0:
        return right != 0
    return abs(right - left) / abs(left) >= _POSITION_RELATIVE


def _health_band(value: Any) -> int | None:
    number = _decimal_or_none(value)
    if number is None:
        return None
    ratio = number / _WAD
    return sum(1 for band in _HEALTH_BANDS if ratio >= band)


def _decimal_or_none(value: Any) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def _load_approval_state(database: Database, tenant: str) -> dict[tuple[str, str], list[dict[str, Any]]]:
    """Active approvals persisted by earlier runs, keyed by (wallet, network)."""
    found: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for row in database.capability_state_for(tenant):
        if row["capability"] != "permissions.erc20":
            continue
        payload = row.get("payload") if isinstance(row.get("payload"), dict) else {}
        state = payload.get("active_approvals")
        if isinstance(state, list):
            found[(row["wallet_id"], row["network"])] = [item for item in state if isinstance(item, dict)]
    return found


def _active_from_state(state: Mapping[tuple[str, str], list[dict[str, Any]]], enabled) -> list[dict[str, Any]]:
    pairs = {(account.wallet_id, network) for account, network in enabled}
    return [item for key in sorted(state) if key in pairs for item in state[key]]


def _load_position_state(database: Database, tenant: str) -> dict[tuple[str, str, str], dict[str, Any]]:
    found: dict[tuple[str, str, str], dict[str, Any]] = {}
    for row in database.capability_state_for(tenant):
        if row["capability"] != "positions":
            continue
        payload = row.get("payload") if isinstance(row.get("payload"), dict) else {}
        state = payload.get("position_state")
        if not isinstance(state, list):
            continue
        for item in state:
            if isinstance(item, dict) and isinstance(item.get("plugin_id"), str):
                found[(row["wallet_id"], row["network"], item["plugin_id"])] = item
    return found


def _previous_native(database: Database, tenant: str) -> dict[tuple[str, str], int]:
    found: dict[tuple[str, str], int] = {}
    for item in database.capability_items(tenant):
        if item.get("capability") != "balances.native":
            continue
        wallet = item.get("wallet_id")
        network = item.get("network")
        amount = _raw_int(item.get("raw_amount"))
        if isinstance(wallet, str) and isinstance(network, str) and amount is not None:
            found[(wallet, network)] = amount
    return found


def _required_map(enabled, rows, observations, position_summaries, public_config, private_inputs, registry) -> dict[str, str]:
    required: dict[str, str] = {}
    indexed = {(row["wallet_id"], row["network"], row["capability"]): row for row in rows}
    for account, network in enabled:
        wallet = account.wallet_id
        native = indexed.get((wallet, network, "balances.native"))
        if native is not None:
            required[_req_key(wallet, network, "balances.native")] = _required_value(native)
        if registry[network].get("family") != "evm":
            continue
        for capability in ("history.fungible", "permissions.erc20"):
            required[_req_key(wallet, network, capability)] = _health_from_observation(
                observations.get((wallet, network, capability)),
            )
        if _indexer_ready(public_config, private_inputs, network):
            required[_req_key(wallet, network, "history.native_external")] = _health_from_observation(
                observations.get((wallet, network, "history.native_external")),
            )
    for summary in position_summaries:
        if summary.get("required") is False:
            continue
        required[_req_key(summary["wallet_id"], summary["network"], f"positions:{summary['plugin_id']}")] = summary["health"]
    return required


def _indexer_ready(public_config, private_inputs, network: str) -> bool:
    if network == "avalanche-c":
        return False
    # Base stays required while any enabled indexer is on the route. A missing
    # preferred key must not move the capability out of the required set.
    if network == "base-mainnet":
        return _base_history_required(public_config)
    source_id = _routed(public_config, network, "history.native_external")
    if source_id is None:
        return False
    source = public_config.document["sources"].get(source_id, {})
    if source.get("adapter") not in _INDEXER_ADAPTERS:
        return False
    status = private_inputs.source_status.get(source_id)
    credential = source.get("credential_env")
    return isinstance(credential, str) and status is not None and status.credential_present


def _base_history_required(public_config) -> bool:
    for route in public_config.document["routing"]:
        if route.get("network") != "base-mainnet" or route.get("capability") != "history.native_external":
            continue
        for source_id in route.get("source_ids") or []:
            if not isinstance(source_id, str):
                continue
            source = public_config.document["sources"].get(source_id, {})
            if source.get("enabled") is True and source.get("adapter") in _INDEXER_ADAPTERS:
                return True
    return False


def _health_from_observation(observation: CoverageObservation | None) -> str:
    if observation is None:
        return "unknown"
    if "blocked" in observation.reason_codes or "credential_missing" in observation.reason_codes:
        return "blocked"
    if observation.category == "error":
        return "error"
    if (
        observation.category == "partial"
        or observation.gaps
        or not observation.pagination_complete
        or not observation.gap_detection_complete
    ):
        return "partial"
    if observation.category in {"ok", "empty"}:
        return "complete_in_declared_scope"
    return "unknown"


def _storage_rows(
    rows: list[dict[str, Any]],
    position_state: Mapping[tuple[str, str], list[dict[str, Any]]],
    approval_state: Mapping[tuple[str, str], list[dict[str, Any]]] | None = None,
) -> list[dict[str, Any]]:
    stored = []
    for row in rows:
        scope = row.get("scope") if isinstance(row.get("scope"), dict) else {}
        payload: dict[str, Any] = {"coverage": row}
        key = (row["wallet_id"], row["network"])
        if row["capability"] == "positions" and key in position_state:
            payload["position_state"] = position_state[key]
        if row["capability"] == "permissions.erc20" and approval_state is not None and key in approval_state:
            payload["active_approvals"] = approval_state[key]
        stored.append({
            "capability": row["capability"],
            "cursor": scope.get("cursor"),
            "last_success_at": row.get("last_success_at"),
            "network": row["network"],
            "payload": payload,
            "quality": row["quality"],
            "valid_until": row.get("valid_until"),
            "wallet_id": row["wallet_id"],
        })
    return stored


def _merge_cursors(observations, explicit: list[dict[str, Any]], held: set[tuple[str, str, str]]) -> list[dict[str, Any]]:
    rows = []
    seen: set[tuple[str, str, str]] = set()
    for row in explicit:
        key = (row["wallet_id"], row["network"], row["capability"])
        if key in seen:
            continue
        seen.add(key)
        rows.append(row)
    for row in _cursors(observations):
        key = (row["wallet_id"], row["network"], row["capability"])
        if key in seen or key in held:
            continue
        seen.add(key)
        rows.append(row)
    return rows
