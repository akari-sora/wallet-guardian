"""Schedule and store inflow explanations. This does not change an alert."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, Callable

from guardian.explanations.lista import ListaExplainer
from guardian.explanations.models import document, empty_amounts, empty_parties, fingerprint, observed_raw_amount
from guardian.explanations.registry import ExplainerManifest, load_packaged
from guardian.explanations.rpc import ExplanationRpc
from guardian.explanations.discovery import log_from_record, log_record
from guardian.explanations.store import (
    account_for_wallet,
    claim_job,
    clear_enqueue_failure,
    complete_job,
    enqueue,
    enqueue_failures,
    force_pending,
    hold_token,
    job_for,
    jobs_for,
    latest_revision,
    load_interval,
    load_lookback,
    load_requests,
    record_enqueue_failure,
    release_attempt,
    save_interval,
    save_lookback,
    save_requests,
)

_PLUGINS = (ListaExplainer(),)
_INCOMPLETE = frozenset({
    "checkpoint_hash_mismatch",
    "checkpoint_unverified",
    "deadline",
    "discovery_incomplete",
    "historical_state_unavailable",
    "logs_unavailable",
    "provider_call_budget",
    "pruned_logs",
    "rate_limited",
    "read_error",
    "reorg",
    "truncated_logs",
})
_HOLD = frozenset({
    "checkpoint_hash_mismatch",
    "checkpoint_unverified",
    "historical_state_unavailable",
    "logs_unavailable",
    "pruned_logs",
    "rate_limited",
    "read_error",
    "reorg",
})


def note_increase(database: Any, tenant: str, item: Any, alert: dict[str, Any]) -> None:
    """Queue one native increase or an internal native credit. The account stays off the alert."""
    balance = alert.get("kind") == "balance_delta" and alert.get("direction") == "increase"
    if not balance and not any(plugin.matches(alert) for plugin in _PLUGINS):
        return
    account = item.get("account") if isinstance(item, dict) else None
    event_id = alert.get("dedup_key")
    network = str(alert.get("network") or "")
    wallet_id = str(alert.get("wallet_id") or "")
    if not isinstance(account, str) or not isinstance(event_id, str) or not event_id:
        return
    try:
        enqueue(
            database,
            tenant,
            event_id,
            account=account,
            network=network,
            wallet_id=wallet_id,
            payload=alert,
        )
    except Exception:
        record_enqueue_failure(
            database,
            tenant,
            event_id,
            account=account,
            network=network,
            wallet_id=wallet_id,
            payload=alert,
        )


def drain_pending(
    database: Any,
    tenant: str,
    transport: Any,
    clock: Callable[[], float],
    *,
    round_budget: Any | None,
    source_for: Callable[[str], str | None],
    only_event: str | None = None,
    force: bool = False,
    account_by_wallet: dict[str, str] | None = None,
) -> None:
    """Finish queued explanations inside the remaining round. One failure stays local."""
    recover_missing_jobs(database, tenant, account_by_wallet)
    manifests = load_packaged()
    for job in jobs_for(database, tenant):
        if only_event is not None and job["event_id"] != only_event:
            continue
        try:
            _drain_job(database, tenant, job, transport, clock, round_budget, source_for, manifests, force)
        except Exception:
            _fail_closed(database, tenant, job, "read_error")


def refresh_event(
    database: Any,
    tenant: str,
    event_id: str,
    transport: Any,
    clock: Callable[[], float],
    source_for: Callable[[str], str | None],
) -> dict[str, Any] | None:
    """Explicit read-only recompute. A live owner keeps the job; an abandoned one is reclaimed."""
    force_pending(database, tenant, event_id)
    drain_pending(
        database, tenant, transport, clock,
        round_budget=None, source_for=source_for, only_event=event_id, force=True,
    )
    return latest_revision(database, tenant, event_id)


def _drain_job(
    database: Any,
    tenant: str,
    job: dict[str, Any],
    transport: Any,
    clock: Callable[[], float],
    round_budget: Any | None,
    source_for: Callable[[str], str | None],
    manifests: list[ExplainerManifest],
    force: bool,
) -> None:
    event_id = str(job["event_id"])
    if job["state"] == "done" and not force:
        return
    if not force and round_budget is not None and getattr(round_budget, "expired", None) and round_budget.expired():
        return
    manifest = _manifest_for(job, manifests)
    if not force and job["state"] == "retry" and job.get("hold_token"):
        token = _token(database, tenant, job, manifest)
        if token == job["hold_token"]:
            return
    if not claim_job(database, tenant, event_id):
        return
    owned = job_for(database, tenant, event_id) or job
    attempt = int(owned["attempt"])
    generation = int(owned["event_revision"])
    try:
        _run_claimed(
            database, tenant, owned, transport, clock, round_budget, source_for, manifest, attempt, generation,
        )
    finally:
        release_attempt(tenant, event_id, attempt)


def _run_claimed(
    database: Any,
    tenant: str,
    job: dict[str, Any],
    transport: Any,
    clock: Callable[[], float],
    round_budget: Any | None,
    source_for: Callable[[str], str | None],
    manifest: ExplainerManifest | None,
    attempt: int,
    generation: int,
) -> None:
    event_id = str(job["event_id"])
    alert = job["payload"] if isinstance(job["payload"], dict) else {}
    if manifest is None:
        matched = any(plugin.matches(alert) for plugin in _PLUGINS)
        if matched:
            body = _unavailable(event_id, generation, "stale_abi", alert)
        else:
            body = _unavailable(event_id, generation, None, alert, verdict="NOT_APPLICABLE")
        _store(database, tenant, job, body, manifest, attempt, generation)
        return
    plugin = next((item for item in _PLUGINS if item.explainer_id == manifest.explainer_id), None)
    if plugin is None or not plugin.matches(alert):
        body = _unavailable(event_id, generation, None, alert, verdict="NOT_APPLICABLE")
        _store(database, tenant, job, body, manifest, attempt, generation)
        return
    source_id = source_for(str(job["network"]))
    if not source_id:
        body = _unavailable(event_id, generation, "endpoint_missing", alert, manifest)
        _store(database, tenant, job, body, manifest, attempt, generation)
        return
    rpc = ExplanationRpc(transport, source_id, clock, round_budget)
    ctx = SimpleNamespace(alert=alert, account=job["account"], manifest=manifest, rpc=rpc)
    _bind_discovery(ctx, database, tenant, event_id, manifest, str(job["account"]), str(job["network"]))
    try:
        body = plugin.explain(ctx)
    except Exception:
        body = _unavailable(event_id, generation, "read_error", alert, manifest)
    if not isinstance(body, dict):
        body = _unavailable(event_id, generation, "read_error", alert, manifest)
    _downgrade_if_late(body, rpc)
    body["event_revision"] = generation
    body["event_id"] = event_id
    _store(database, tenant, job, body, manifest, attempt, generation)


def _downgrade_if_late(body: dict[str, Any], rpc: ExplanationRpc) -> None:
    """A response that landed after the budget is not a verified match and does not touch the alert."""
    late = rpc.stopped == "deadline" or rpc.remaining_seconds() <= 0
    if not late:
        return
    gaps = [str(item) for item in body.get("gaps") or []]
    if "deadline" not in gaps:
        gaps.append("deadline")
    body["gaps"] = sorted(set(gaps))
    if body.get("verdict") == "MATCHES_OWN_REQUEST":
        body["verdict"] = "UNVERIFIED"
        facts = body.get("facts")
        if isinstance(facts, dict):
            facts["beneficiary_payment"] = "unavailable"
            facts["implementation_at_execution"] = "unverified"
            facts["request_linkage"] = "unavailable"
        body["established_facts"] = []
        body["explained_effects"] = []


def _bind_discovery(
    ctx: Any,
    database: Any,
    tenant: str,
    event_id: str,
    manifest: ExplainerManifest,
    account: str,
    network: str,
) -> None:
    cursor, rows = load_interval(database, tenant, event_id)
    ctx.interval_cursor = cursor
    ctx.saved_logs = [log_from_record(item) for item in rows]
    ctx.lookback_cursor = load_lookback(database, tenant, network, account, manifest.proxy())
    ctx.known_requests = lambda: [
        log_from_record({
            "account": account,
            "address": str(item.get("address") or manifest.proxy()),
            "block_hash": str(item.get("block_hash") or ""),
            "block_number": int(item["block_number"]),
            "conflict": bool(item.get("conflict")),
            "data_words": [int(word) for word in item.get("data_words") or [int(item["slis"])]],
            "kind": str(item.get("kind") or "request"),
            "log_index": int(item["log_index"]),
            "removed": bool(item.get("removed")),
            "topic": str(item.get("topic") or ""),
            "tx_id": str(item["tx_id"]),
            "tx_index": int(item["tx_index"]),
        })
        for item in load_requests(database, tenant, network, account, manifest.proxy())
    ]

    def save_interval_rows(next_cursor: int | None, logs: list[Any]) -> None:
        save_interval(database, tenant, event_id, next_cursor, [log_record(item) for item in logs])

    def save_request_rows(logs: list[Any]) -> None:
        save_requests(database, tenant, network, account, manifest.proxy(), [log_record(item) for item in logs])

    def save_lookback_cursor(next_cursor: int) -> None:
        save_lookback(database, tenant, network, account, manifest.proxy(), next_cursor)

    ctx.save_interval = save_interval_rows
    ctx.save_requests = save_request_rows
    ctx.save_lookback = save_lookback_cursor


def _store(
    database: Any,
    tenant: str,
    job: dict[str, Any],
    body: dict[str, Any],
    manifest: ExplainerManifest | None,
    attempt: int,
    generation: int,
) -> None:
    gaps = {str(item) for item in body.get("gaps") or []}
    reason = body.get("retry_reason") if isinstance(body.get("retry_reason"), str) else None
    incomplete = (reason in _INCOMPLETE) or bool(gaps & _INCOMPLETE)
    hold_reason = None
    if reason in _HOLD:
        hold_reason = reason
    elif gaps & _HOLD:
        hold_reason = sorted(gaps & _HOLD)[0]
    if incomplete:
        body["attempt_state"] = "retry"
        body["retry_reason"] = reason or hold_reason or "read_error"
    else:
        body["attempt_state"] = "complete"
        body["retry_reason"] = None
    body["funds_marked_safe"] = False
    body["severity_changed"] = False
    evidence = fingerprint(_evidence(body))
    token = _token(database, tenant, job, manifest) if hold_reason and incomplete else None
    complete_job(
        database,
        tenant,
        str(job["event_id"]),
        body,
        evidence,
        attempt=attempt,
        event_revision=generation,
        state="retry" if incomplete else "done",
        retry_reason=body.get("retry_reason") if incomplete else None,
        hold_token=token,
    )


def _token(database: Any, tenant: str, job: dict[str, Any], manifest: ExplainerManifest | None) -> str:
    payload = job["payload"] if isinstance(job["payload"], dict) else {}
    checkpoints = {
        "new": _hash_of(payload.get("new_checkpoint")),
        "old": _hash_of(payload.get("old_checkpoint")),
    }
    digest = "" if manifest is None else manifest.manifest_hash
    return hold_token(database, tenant, str(job["wallet_id"]), str(job["network"]), checkpoints, digest)


def _evidence(body: dict[str, Any]) -> dict[str, Any]:
    return {
        "amounts": body.get("amounts"),
        "attempt_state": body.get("attempt_state"),
        "calculation_version": body.get("calculation_version"),
        "checkpoint_hashes": body.get("checkpoint_hashes"),
        "contradiction_reasons": body.get("contradiction_reasons"),
        "event_id": body.get("event_id"),
        "event_revision": body.get("event_revision"),
        "evidence_refs": body.get("evidence_refs"),
        "explained_effects": body.get("explained_effects"),
        "explainer_id": body.get("explainer_id"),
        "facts": body.get("facts"),
        "gaps": body.get("gaps"),
        "manifest_hash": body.get("manifest_hash"),
        "parties": body.get("parties"),
        "request_identity": body.get("request_identity"),
        "residual": body.get("residual"),
        "retry_reason": body.get("retry_reason"),
        "verdict": body.get("verdict"),
    }


def _manifest_for(job: dict[str, Any], manifests: list[ExplainerManifest]) -> ExplainerManifest | None:
    alert = job["payload"] if isinstance(job["payload"], dict) else {}
    for manifest in manifests:
        plugin = next((item for item in _PLUGINS if item.explainer_id == manifest.explainer_id), None)
        if plugin is not None and plugin.matches(alert) and manifest.network() == job["network"]:
            return manifest
    return None


def _unavailable(
    event_id: str,
    event_revision: int,
    gap: str | None,
    alert: dict[str, Any],
    manifest: ExplainerManifest | None = None,
    verdict: str = "UNVERIFIED",
) -> dict[str, Any]:
    gaps = [] if gap is None else [gap]
    observed = observed_raw_amount(alert)
    old = _hash_of(alert.get("old_checkpoint") if isinstance(alert, dict) else None)
    new = _hash_of(alert.get("new_checkpoint") if isinstance(alert, dict) else None)
    return document(
        event_id=event_id,
        event_revision=event_revision,
        revision=0,
        supersedes=None,
        explainer_id=None if manifest is None else manifest.explainer_id,
        explainer_version=None if manifest is None else manifest.version,
        manifest_hash=None if manifest is None else manifest.manifest_hash,
        verdict=verdict,
        facts={
            "beneficiary_payment": "unavailable",
            "burned_slis": "not_established",
            "caller_bot_role": "not_queried",
            "claim_time_rate": "not_queried",
            "implementation_at_execution": "unavailable",
            "request_linkage": "unavailable",
            "request_time_quote": "not_queried",
        },
        gaps=gaps,
        contradictions=[],
        established=[],
        parties=empty_parties(),
        amounts=empty_amounts(),
        effects=[],
        residual={
            "asset": str(alert.get("asset") or "") if isinstance(alert, dict) else "",
            "explained_raw_amount": "0",
            "observed_raw_amount": str(observed),
            "raw_amount": str(observed),
        },
        evidence_refs=[],
        checkpoints={"new": new, "old": old},
        request_identity=None,
        attempt_state="complete" if verdict == "NOT_APPLICABLE" else "retry",
        retry_reason=None if verdict == "NOT_APPLICABLE" else gap,
    )


def _fail_closed(database: Any, tenant: str, job: dict[str, Any], gap: str) -> None:
    event_id = str(job["event_id"])
    current = job_for(database, tenant, event_id) or job
    if str(current.get("state")) != "running":
        if not claim_job(database, tenant, event_id):
            return
        current = job_for(database, tenant, event_id) or current
    attempt = int(current["attempt"])
    generation = int(current["event_revision"])
    try:
        body = _unavailable(event_id, generation, gap, current.get("payload") or {})
        _store(database, tenant, current, body, None, attempt, generation)
    finally:
        release_attempt(tenant, event_id, attempt)


def recover_missing_jobs(database: Any, tenant: str, account_by_wallet: dict[str, str] | None = None) -> None:
    """Enqueue investigations that committed without a job. A failure stays visible until it lands."""
    known = account_by_wallet or {}
    present = {str(item["event_id"]) for item in jobs_for(database, tenant)}
    for failed in enqueue_failures(database, tenant):
        event_id = str(failed["event_id"])
        if event_id in present:
            clear_enqueue_failure(database, tenant, event_id)
            continue
        account = failed.get("account")
        network = failed.get("network")
        wallet_id = failed.get("wallet_id")
        payload = failed.get("payload") if isinstance(failed.get("payload"), dict) else {}
        if not all(isinstance(item, str) and item for item in (account, network, wallet_id)):
            continue
        enqueue(
            database,
            tenant,
            event_id,
            account=str(account),
            network=str(network),
            wallet_id=str(wallet_id),
            payload=payload,
        )
        clear_enqueue_failure(database, tenant, event_id)
        present.add(event_id)
    try:
        alerts = database.alerts_for(tenant)
    except Exception:
        record_enqueue_failure(
            database,
            tenant,
            "__recover__",
            account="",
            network="",
            wallet_id="",
            payload={"reason": "alerts_unreadable"},
        )
        return
    for alert in alerts:
        if not isinstance(alert, dict) or not _should_queue(alert):
            continue
        event_id = alert.get("dedup_key")
        wallet_id = alert.get("wallet_id")
        if not isinstance(event_id, str) or not event_id or event_id in present:
            continue
        if not isinstance(wallet_id, str) or not wallet_id:
            continue
        account = known.get(wallet_id) or account_for_wallet(database, tenant, wallet_id)
        if account is None:
            record_enqueue_failure(
                database,
                tenant,
                event_id,
                account="",
                network=str(alert.get("network") or ""),
                wallet_id=wallet_id,
                payload=alert,
            )
            continue
        enqueue(
            database,
            tenant,
            event_id,
            account=account,
            network=str(alert.get("network") or ""),
            wallet_id=wallet_id,
            payload=alert,
        )
        clear_enqueue_failure(database, tenant, event_id)
        present.add(event_id)


def _should_queue(alert: dict[str, Any]) -> bool:
    if alert.get("kind") == "balance_delta" and alert.get("direction") == "increase":
        return True
    return any(plugin.matches(alert) for plugin in _PLUGINS)


def _hash_of(point: Any) -> str:
    if isinstance(point, dict) and isinstance(point.get("block_hash"), str):
        return str(point["block_hash"]).lower()
    return ""
