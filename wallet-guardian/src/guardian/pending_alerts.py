"""Candidates stored with a leaf commit and not yet judged by the round policy.

A leaf writes events and the cursor before the round's alert decision. If that
round stops before the final commit, the next round loads these rows, judges
them, and writes the alerts. A judged identity is removed in the same
transaction as its alert.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from guardian.rules import Candidate

_SCHEMA = "pending_alert_candidate.v1"
_FIELDS = (
    "tenant_id",
    "wallet_id",
    "network",
    "capability",
    "identity",
    "kind",
    "raw_amount",
    "is_native",
    "direction",
    "counterparty_wallet_id",
    "quote_usd",
    "quote_valuation",
    "quote_quality",
    "permission_change",
    "prior_known",
    "explicit_risk",
    "tx_id",
    "symbol",
    "net_amount",
    "transfer_origin",
    "attribution",
    "execution_evidence",
    "counterparty",
    "block_number",
    "asset",
)


def snapshot_candidate(candidate: Candidate) -> dict[str, Any]:
    """JSON-ready copy of one candidate. Quotes stay decimal text, never 0."""
    payload: dict[str, Any] = {"schema": _SCHEMA}
    for name in _FIELDS:
        value = getattr(candidate, name)
        if isinstance(value, Decimal):
            value = format(value, "f")
        payload[name] = value
    return payload


def candidate_from_snapshot(payload: dict[str, Any]) -> Candidate:
    """Rebuild a candidate. A missing quote stays missing."""
    if payload.get("schema") != _SCHEMA:
        raise ValueError("pending alert candidate schema")
    data: dict[str, Any] = {}
    for name in _FIELDS:
        if name not in payload:
            raise ValueError("pending alert candidate field")
        data[name] = payload[name]
    quote = data.get("quote_usd")
    if isinstance(quote, str) and quote:
        data["quote_usd"] = Decimal(quote)
    elif quote in {None, ""}:
        data["quote_usd"] = None
    return Candidate(**data)


def snapshots_for(candidates: list[Any]) -> list[dict[str, Any]]:
    """Leaf candidates only. Baseline summaries are not replayed as transfers."""
    found: dict[str, dict[str, Any]] = {}
    for item in candidates:
        if getattr(item, "kind", None) == "baseline":
            continue
        identity = getattr(item, "identity", None)
        if not isinstance(identity, str) or not identity:
            continue
        found[identity] = snapshot_candidate(item)
    return [found[key] for key in sorted(found)]
