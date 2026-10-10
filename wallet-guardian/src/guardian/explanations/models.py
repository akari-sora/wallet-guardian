"""Explanation documents. Amounts are integer base-unit strings. No safety verdict."""

from __future__ import annotations

import hashlib
import json
from typing import Any

SCHEMA_VERSION = "guardian.explanation.v1"
VERDICTS = frozenset({
    "MATCHES_OWN_REQUEST",
    "PARTIAL",
    "NO_MATCH",
    "UNVERIFIED",
    "NOT_APPLICABLE",
})
# A truncated window or a failed historical read is not a contradiction.
FALSE_NO_MATCH_GAPS = frozenset({
    "checkpoint_unverified",
    "claim_evidence_mismatch",
    "deadline",
    "discovery_incomplete",
    "empty_interval",
    "empty_request_array",
    "historical_state_unavailable",
    "logs_unavailable",
    "no_claim_in_interval",
    "provider_call_budget",
    "pruned_logs",
    "rate_limited",
    "read_error",
    "receipt_checkpoint_unverified",
    "receipt_status_unavailable",
    "request_index_mismatch",
    "truncated_logs",
})
CALL_LIMIT = 32
TIME_LIMIT_SECONDS = 30
LOG_SPAN_MAX = 2_000
ERC1967_IMPLEMENTATION_SLOT = "0x360894a13ba1a3210667c828492db98dca3e2076cc3735a920a3ca505d382bbc"
UPGRADED_TOPIC = "0xbc7cd75a20ee27fd9adebab32041f755214dbc6bffa90cc0225b39da2e5c2d3b"


def canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def fingerprint(material: Any) -> str:
    return sha256_text(canonical(material))


def default_facts() -> dict[str, str]:
    return {
        "beneficiary_payment": "unavailable",
        "burned_slis": "not_established",
        "caller_bot_role": "not_queried",
        "claim_time_rate": "not_queried",
        "implementation_at_execution": "unavailable",
        "request_linkage": "unavailable",
        "request_time_quote": "not_queried",
    }


def empty_amounts() -> dict[str, str | None]:
    return {
        "actual_claim_payment": None,
        "burned_slis_amount": None,
        "claim_time_conversion_quote": None,
        "contract_expected_claim_amount": None,
        "effective_settlement_ratio": None,
        "request_time_bnb_quote": None,
        "request_time_conversion_quote": None,
        "requested_slis_amount": None,
    }


def empty_parties() -> dict[str, str | None]:
    return {
        "effective_caller": None,
        "payout_recipient": None,
        "request_account": None,
        "tx_submitter": None,
    }


def settlement_ratio(payment: int | None, requested: int | None) -> str | None:
    if payment is None or requested is None or requested <= 0 or payment < 0:
        return None
    return f"{payment}/{requested}"


def guard_verdict(verdict: str, gaps: list[str], contradictions: list[str]) -> str:
    """A missing read is not a rejected explanation."""
    if verdict not in VERDICTS:
        return "UNVERIFIED"
    if verdict != "NO_MATCH":
        return verdict
    if not contradictions:
        return "UNVERIFIED"
    if FALSE_NO_MATCH_GAPS.intersection(gaps) and "failed_payment" not in contradictions and "beneficiary_contradiction" not in contradictions:
        return "UNVERIFIED"
    if "truncated_logs" in gaps or "pruned_logs" in gaps:
        return "UNVERIFIED"
    return verdict


def document(
    *,
    event_id: str,
    event_revision: int,
    revision: int,
    supersedes: int | None,
    explainer_id: str | None,
    explainer_version: str | None,
    manifest_hash: str | None,
    verdict: str,
    facts: dict[str, str],
    gaps: list[str],
    contradictions: list[str],
    established: list[str],
    parties: dict[str, str | None],
    amounts: dict[str, str | None],
    effects: list[dict[str, Any]],
    residual: dict[str, str],
    evidence_refs: list[str],
    checkpoints: dict[str, str],
    request_identity: str | None,
    attempt_state: str,
    retry_reason: str | None,
    calculation_version: str | None = None,
    request_uuid: str | None = None,
) -> dict[str, Any]:
    chosen = guard_verdict(verdict, gaps, contradictions)
    body = {
        "amounts": amounts,
        "attempt_state": attempt_state,
        "calculation_version": calculation_version,
        "checkpoint_hashes": checkpoints,
        "contradiction_reasons": sorted(set(contradictions)),
        "control_inference": "not_established",
        "established_facts": sorted(set(established)),
        "event_id": event_id,
        "event_revision": event_revision,
        "evidence_refs": sorted(set(evidence_refs)),
        "explained_effects": effects,
        "explainer_id": explainer_id,
        "explainer_version": explainer_version,
        "facts": facts,
        "funds_marked_safe": False,
        "gaps": sorted(set(gaps)),
        "labels": {
            "counterparty_identity": "unverified",
            "label_source": "static_local_registry",
            "safety_assessment": "not_established",
        },
        "manifest_hash": manifest_hash,
        "operator_control": "not_established",
        "parties": parties,
        "request_identity": request_identity,
        "request_uuid": request_uuid,
        "residual": residual,
        "retry_reason": retry_reason,
        "revision": revision,
        "schema_version": SCHEMA_VERSION,
        "severity_changed": False,
        "supersedes": supersedes,
        "verdict": chosen,
    }
    return body
