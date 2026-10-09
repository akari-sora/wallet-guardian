"""Coverage denominator. One row per enabled wallet, network, and capability.

A missing row is unknown. Empty gaps are not a completed gap check. A native
balance alone is not a complete portfolio. Nothing here is a live result.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from guardian.jsonschema_lite import validate
from guardian.resources import resource_path
from guardian.sources.capabilities import CAPABILITIES

_SCHEMA: dict[str, Any] | None = None
_APPLICABLE = frozenset({"implemented", "partial"})
_QUALITIES = ("complete_in_declared_scope", "partial", "stale", "error", "unknown")
_SUPPORT_RANK = {"implemented": 3, "partial": 2, "unsupported": 1}
_PROBES = frozenset({"blocked", "documented", "untested"})
_TRUST = frozenset({"single_provider", "cross_checked_independent", "unverified"})
_PRICE = frozenset({"not_applicable", "available", "stale"})
_SOURCE = re.compile(r"^[a-z][a-z0-9_-]{0,95}$")
_CHECKPOINT_KIND = {
    "evm": "block",
    "solana": "slot",
    "bitcoin": "height",
    "tron": "block",
    "ton": "lt",
    "sui": "checkpoint",
    "aptos": "ledger_version",
    "cosmos": "height",
}
_CHECKPOINT_STATUS = frozenset({"unknown", "observed", "confirmed", "finalized", "orphaned"})


@dataclass(frozen=True)
class CoverageObservation:
    category: str
    pagination_complete: bool = False
    gap_detection_complete: bool = False
    gaps: tuple[str, ...] = ()
    cursor: str | None = None
    declared: str | None = None
    scope_from: str | None = None
    scope_to: str | None = None
    checkpoint_kind: str | None = None
    checkpoint_number: str | None = None
    checkpoint_hash: str | None = None
    checkpoint_status: str | None = None
    last_success_at: str | None = None
    valid_until: str | None = None
    sources: tuple[str, ...] = ()
    trust: str | None = None
    price_quality: str | None = None
    evidence_refs: tuple[str, ...] = ()
    reason_codes: tuple[str, ...] = ()
    credential_hint: str = ""

    def __post_init__(self) -> None:
        if self.category not in {"ok", "empty", "partial", "error", "stale"}:
            raise ValueError("unknown observation category")
        object.__setattr__(self, "gaps", tuple(self.gaps))
        object.__setattr__(self, "sources", tuple(self.sources))
        object.__setattr__(self, "evidence_refs", tuple(self.evidence_refs))
        object.__setattr__(self, "reason_codes", tuple(self.reason_codes))
        for name in ("gaps", "sources", "evidence_refs", "reason_codes"):
            values = getattr(self, name)
            if any(not isinstance(item, str) for item in values):
                raise ValueError(f"{name} must be strings")


def coverage_schema() -> dict[str, Any]:
    global _SCHEMA
    if _SCHEMA is None:
        _SCHEMA = json.loads(resource_path("schemas", "coverage.schema.json").read_text(encoding="utf-8"))
    return _SCHEMA


def build_coverage(
    enabled: Sequence[tuple[str, str]],
    results: Mapping[tuple[str, str, str], CoverageObservation | Mapping[str, Any] | None],
    catalog: Any,
    registry: Mapping[str, Mapping[str, Any]],
    now: str,
) -> list[dict[str, Any]]:
    """One row per enabled wallet, enabled network, and catalog capability."""
    rows: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for wallet_id, network in enabled:
        pair = (wallet_id, network)
        if pair in seen:
            continue
        seen.add(pair)
        if not isinstance(wallet_id, str) or not wallet_id:
            raise ValueError("wallet_id is required")
        if network not in registry:
            raise ValueError(f"unknown network {network}")
        entry = registry[network]
        for capability in CAPABILITIES:
            observation = _observation(results.get((wallet_id, network, capability)))
            support, probe = resolve_support(catalog, network, capability)
            rows.append(_row(wallet_id, network, capability, support, probe, observation, entry, now))
    schema = coverage_schema()
    for row in rows:
        validate(row, schema)
    return rows


def resolve_support(catalog: Any, network: str, capability: str) -> tuple[str, str]:
    """Best declared support for one network. permits.offchain_unused stays not_observable."""
    if capability == "permits.offchain_unused":
        return "not_observable", "documented"
    statuses = list(_statuses(catalog, network, capability))
    if not statuses:
        return "unsupported", "untested"
    if all(status.support == "not_applicable" for status in statuses):
        return "not_applicable", _probe(statuses)
    if all(status.support == "not_observable" for status in statuses):
        return "not_observable", _probe(statuses)
    ranked = [status for status in statuses if status.support in _SUPPORT_RANK]
    if not ranked:
        if any(status.support == "not_observable" for status in statuses):
            return "not_observable", _probe(statuses)
        return "not_applicable", _probe(statuses)
    best = max(ranked, key=lambda status: _SUPPORT_RANK[status.support]).support
    winners = [status for status in ranked if status.support == best]
    return best, _probe(winners)


def portfolio_summary(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Complete only when every applicable capability is complete, and not native-only."""
    counts = {quality: 0 for quality in _QUALITIES}
    for row in rows:
        quality = str(row.get("quality"))
        if quality in counts:
            counts[quality] += 1
    reasons: list[str] = []
    wallets: dict[str, list[Mapping[str, Any]]] = {}
    for row in rows:
        wallets.setdefault(str(row.get("wallet_id")), []).append(row)
    complete_wallets: list[bool] = []
    for group in wallets.values():
        applicable = [row for row in group if row.get("support") in _APPLICABLE]
        complete_rows = [row for row in applicable if row.get("quality") == "complete_in_declared_scope"]
        all_complete = bool(applicable) and len(complete_rows) == len(applicable)
        native_complete = any(
            row.get("capability") == "balances.native" and row.get("quality") == "complete_in_declared_scope"
            for row in group
        )
        other_complete = any(
            row.get("capability") != "balances.native" and row.get("quality") == "complete_in_declared_scope"
            for row in applicable
        )
        if native_complete and not other_complete:
            reasons.append("native_only_not_portfolio")
            complete_wallets.append(False)
        else:
            complete_wallets.append(all_complete)
    return {
        "complete": bool(complete_wallets) and all(complete_wallets),
        "counts": counts,
        "reason_codes": sorted(set(reasons)),
    }


def render_coverage_md(rows: Sequence[Mapping[str, Any]]) -> str:
    """Short table. Missing rows are unknown, not safe and not zero."""
    lines = [
        "沒有列出來的能力是 unknown，不是完整，也不是零。",
        "",
        "| wallet_id | network | capability | support | quality |",
        "|---|---|---|---|---|",
    ]
    for row in rows:
        lines.append(
            "| {wallet_id} | {network} | {capability} | {support} | {quality} |".format(
                wallet_id=row.get("wallet_id", ""),
                network=row.get("network", ""),
                capability=row.get("capability", ""),
                support=row.get("support", ""),
                quality=row.get("quality", "unknown"),
            )
        )
    if not rows:
        lines.append("| | | | | unknown |")
    return "\n".join(lines) + "\n"


def _statuses(catalog: Any, network: str, capability: str) -> list[Any]:
    found = []
    for adapter in getattr(catalog, "adapters", {}).values():
        support = adapter.declares_support(network, capability)
        if support is None:
            continue
        status = adapter.capabilities.get(capability)
        if status is None:
            continue
        found.append(status)
    return found


def _probe(statuses: Sequence[Any]) -> str:
    probes = [getattr(status, "probe", "untested") for status in statuses]
    for candidate in ("documented", "blocked", "untested"):
        if candidate in probes:
            return candidate
    return "untested"


def _observation(value: CoverageObservation | Mapping[str, Any] | None) -> CoverageObservation | None:
    if value is None:
        return None
    if isinstance(value, CoverageObservation):
        return value
    if isinstance(value, Mapping):
        return CoverageObservation(**dict(value))
    raise ValueError("observation is malformed")


def _row(
    wallet_id: str,
    network: str,
    capability: str,
    support: str,
    probe: str,
    observation: CoverageObservation | None,
    entry: Mapping[str, Any],
    now: str,
) -> dict[str, Any]:
    quality, reasons, success_at = _quality(support, observation, now)
    if probe not in _PROBES:
        probe = "untested"
    scope, checkpoint = _scope(support, observation, entry, quality)
    row = {
        "schema_version": "guardian.coverage.v1",
        "wallet_id": wallet_id,
        "network": network,
        "chain_uid": _chain_uid(entry, network),
        "capability": capability,
        "adapter_version": "none",
        "support": support,
        "probe": probe,
        "quality": quality,
        "scope": scope,
        "checkpoint": checkpoint,
        "last_success_at": success_at,
        "valid_until": observation.valid_until if observation is not None and quality == "error" else (
            observation.valid_until if observation is not None and quality in {"partial", "stale", "complete_in_declared_scope"} else None
        ),
        "sources": _sources(observation),
        "trust": _trust(observation),
        "price_quality": _price_quality(observation),
        "reason_codes": reasons,
        "evidence_refs": list(observation.evidence_refs) if observation is not None else [],
    }
    hint = observation.credential_hint if observation is not None else ""
    if hint and "credential_missing" in reasons:
        row["credential_hint"] = hint
    return row


def _quality(
    support: str,
    observation: CoverageObservation | None,
    now: str,
) -> tuple[str, list[str], str | None]:
    if support == "not_observable":
        return "unknown", ["not_observable"], _previous_success(observation)
    if support == "not_applicable":
        return "unknown", ["not_applicable"], _previous_success(observation)
    if observation is None:
        return "unknown", ["not_queried"], None
    extra = [code for code in observation.reason_codes if code not in {"not_queried", "not_applicable", "not_observable"}]
    if observation.category == "error":
        return "error", _unique(["read_error", *extra]), observation.last_success_at
    if observation.category == "stale":
        return "stale", _unique(["stale", *extra]), observation.last_success_at
    if support not in _APPLICABLE:
        return "unknown", _unique(["unsupported", *extra]), observation.last_success_at
    reasons = _incomplete_reasons(observation)
    if observation.category == "partial":
        if not reasons:
            reasons = ["partial_result"]
        return "partial", _unique([*reasons, *extra]), now
    if observation.category in {"ok", "empty"} and not reasons:
        # Completeness labels stay visible. They do not turn a covered window into a failure.
        kept = [code for code in extra if code in {"incremental_window_complete", "pre_origin_history_unknown"}]
        return "complete_in_declared_scope", _unique(kept), now
    if observation.category in {"ok", "empty"}:
        return "partial", _unique([*reasons, *extra]), now
    return "unknown", _unique(["unrecognized_category", *extra]), observation.last_success_at


def _incomplete_reasons(observation: CoverageObservation) -> list[str]:
    reasons = []
    if not observation.pagination_complete:
        reasons.append("pagination_incomplete")
    if not observation.gap_detection_complete:
        reasons.append("gap_detection_incomplete")
    if observation.gaps:
        reasons.append("gaps_present")
    return reasons


def _previous_success(observation: CoverageObservation | None) -> str | None:
    if observation is None:
        return None
    return observation.last_success_at


def _scope(
    support: str,
    observation: CoverageObservation | None,
    entry: Mapping[str, Any],
    quality: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    kind = _checkpoint_kind(entry, observation)
    if observation is None or (quality == "unknown" and support in {"not_observable", "not_applicable"}):
        declared = {
            "not_observable": "not_observable",
            "not_applicable": "not_applicable",
        }.get(support, "not_queried")
        scope = {
            "declared": declared,
            "from": None,
            "to": None,
            "pagination_complete": False,
            "gap_detection_complete": False,
            "gaps": [],
            "cursor": None,
        }
        checkpoint = {"kind": kind, "number": None, "hash": None, "status": "unknown"}
        return scope, checkpoint
    if observation is None:
        scope = {
            "declared": "not_queried",
            "from": None,
            "to": None,
            "pagination_complete": False,
            "gap_detection_complete": False,
            "gaps": [],
            "cursor": None,
        }
        return scope, {"kind": kind, "number": None, "hash": None, "status": "unknown"}
    status = observation.checkpoint_status if observation.checkpoint_status in _CHECKPOINT_STATUS else "unknown"
    scope = {
        "declared": observation.declared or "observed",
        "from": observation.scope_from,
        "to": observation.scope_to,
        "pagination_complete": bool(observation.pagination_complete),
        "gap_detection_complete": bool(observation.gap_detection_complete),
        "gaps": list(observation.gaps),
        "cursor": observation.cursor,
    }
    checkpoint = {
        "kind": kind,
        "number": _checkpoint_number(observation.checkpoint_number),
        "hash": observation.checkpoint_hash,
        "status": status,
    }
    return scope, checkpoint


def _checkpoint_kind(entry: Mapping[str, Any], observation: CoverageObservation | None) -> str:
    if observation is not None and observation.checkpoint_kind in _CHECKPOINT_KIND.values():
        return str(observation.checkpoint_kind)
    family = entry.get("family")
    if isinstance(family, str) and family in _CHECKPOINT_KIND:
        return _CHECKPOINT_KIND[family]
    return "block"


def _checkpoint_number(value: str | None) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        raise ValueError("checkpoint number must be a string")
    return value


def _chain_uid(entry: Mapping[str, Any], network: str) -> str:
    if entry.get("family") == "evm" and isinstance(entry.get("evm_chain_id"), int):
        return f"eip155:{entry['evm_chain_id']}"
    identity = entry.get("identity")
    if isinstance(identity, dict):
        expected = identity.get("expected_value")
        if isinstance(expected, str) and expected.strip():
            return expected
    return network


def _sources(observation: CoverageObservation | None) -> list[str]:
    if observation is None:
        return []
    sources = list(observation.sources)
    if any(_SOURCE.fullmatch(item) is None for item in sources):
        raise ValueError("source id is malformed")
    return sources


def _trust(observation: CoverageObservation | None) -> str:
    if observation is None or observation.trust is None:
        return "unverified"
    if observation.trust in _TRUST:
        return observation.trust
    return "unverified"


def _price_quality(observation: CoverageObservation | None) -> str:
    if observation is None or observation.price_quality is None:
        return "unknown"
    if observation.price_quality in _PRICE:
        return observation.price_quality
    return "unknown"


def _unique(values: Sequence[str]) -> list[str]:
    found: list[str] = []
    for value in values:
        if value not in found:
            found.append(value)
    return found
