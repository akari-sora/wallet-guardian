"""Schedule and store inflow explanations. This does not change an alert."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, Callable

from guardian.explanations.lista import ListaExplainer
from guardian.explanations.models import document, empty_amounts, empty_parties, fingerprint
from guardian.explanations.registry import ExplainerManifest, load_packaged
from guardian.explanations.rpc import ExplanationRpc
from guardian.explanations.store import (
    claim_job,
    enqueue,
    finish_job,
    force_pending,
    hold_token,
    insert_revision,
    jobs_for,
    latest_revision,
)

_PLUGINS = (ListaExplainer(),)
_INCOMPLETE = frozenset({
    "checkpoint_hash_mismatch",
    "checkpoint_unverified",
    "deadline",
    "historical_state_unavailable",
    "logs_unavailable",
    "provider_call_budget",
    "pruned_logs",
    "rate_limited",
    "read_error",
    "reorg",
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
    """Queue one native increase. The account stays off the alert payload."""
    if alert.get("kind") != "balance_delta" or alert.get("direction") != "increase":
        return
    account = item.get("account") if isinstance(item, dict) else None
    event_id = alert.get("dedup_key")
    if not isinstance(account, str) or not isinstance(event_id, str) or not event_id:
        return
    enqueue(
        database,
        tenant,
        event_id,
        account=account,
        network=str(alert.get("network") or ""),
        wallet_id=str(alert.get("wallet_id") or ""),
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
) -> None:
    """Finish queued explanations inside the remaining round. One failure stays local."""
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
    """Explicit read-only recompute. A running job is left to its owner."""
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
    if job["state"] == "running":
        return
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
    alert = job["payload"] if isinstance(job["payload"], dict) else {}
    if manifest is None:
        matched = any(plugin.matches(alert) for plugin in _PLUGINS)
        if matched:
            body = _unavailable(event_id, int(job["event_revision"]), "stale_abi", alert)
        else:
            body = _unavailable(event_id, int(job["event_revision"]), None, alert, verdict="NOT_APPLICABLE")
        _store(database, tenant, job, body, manifest)
        return
    plugin = next((item for item in _PLUGINS if item.explainer_id == manifest.explainer_id), None)
    if plugin is None or not plugin.matches(alert):
        body = _unavailable(event_id, int(job["event_revision"]), None, alert, verdict="NOT_APPLICABLE")
        _store(database, tenant, job, body, manifest)
        return
    source_id = source_for(str(job["network"]))
    if not source_id:
        body = _unavailable(event_id, int(job["event_revision"]), "endpoint_missing", alert, manifest)
        _store(database, tenant, job, body, manifest)
        return
    rpc = ExplanationRpc(transport, source_id, clock, round_budget)
    ctx = SimpleNamespace(alert=alert, account=job["account"], manifest=manifest, rpc=rpc)
    try:
        body = plugin.explain(ctx)
    except Exception:
        body = _unavailable(event_id, int(job["event_revision"]), "read_error", alert, manifest)
    if not isinstance(body, dict):
        body = _unavailable(event_id, int(job["event_revision"]), "read_error", alert, manifest)
    body["event_revision"] = int(job["event_revision"])
    body["event_id"] = event_id
    _store(database, tenant, job, body, manifest)


def _store(database: Any, tenant: str, job: dict[str, Any], body: dict[str, Any], manifest: ExplainerManifest | None) -> None:
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
    insert_revision(database, tenant, str(job["event_id"]), body, evidence)
    token = _token(database, tenant, job, manifest) if hold_reason and incomplete else None
    finish_job(
        database,
        tenant,
        str(job["event_id"]),
        state="retry" if incomplete else "done",
        retry_reason=body.get("retry_reason") if incomplete else None,
        hold_token=token,
        event_revision=int(job["event_revision"]),
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
    signed = alert.get("signed_delta_raw") if isinstance(alert, dict) else None
    try:
        observed = int(signed)
    except (TypeError, ValueError):
        observed = 0
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
    if job["state"] == "running":
        body = _unavailable(str(job["event_id"]), int(job["event_revision"]), gap, job.get("payload") or {})
        _store(database, tenant, job, body, None)
        return
    try:
        if claim_job(database, tenant, str(job["event_id"])):
            body = _unavailable(str(job["event_id"]), int(job["event_revision"]), gap, job.get("payload") or {})
            _store(database, tenant, job, body, None)
    except Exception:
        return


def _hash_of(point: Any) -> str:
    if isinstance(point, dict) and isinstance(point.get("block_hash"), str):
        return str(point["block_hash"]).lower()
    return ""
