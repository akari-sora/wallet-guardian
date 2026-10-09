"""Persisted yield comparisons. Fixture transport and a caller clock only.

Nothing in this module opens a socket, constructs an HTTP client, follows an
article link, or hands a candidate to a sender. The once-a-day gate and
conditional validators are here so a fake clock can exercise them. They are not
a scheduler, and ``enabled`` in the stored document stays false.
"""

from __future__ import annotations

import copy
import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Callable, Mapping

from guardian.errors import GuardianError, SecurityError
from guardian.jsonschema_lite import validate
from guardian.sources.budget import BudgetBook
from guardian.sources.fixtures import FixtureMiss
from guardian.sources.transport import EndpointRegistry, ReadOnlyTransport, RegisteredSource, TransportError
from guardian.storage import Database
from guardian.yields.matcher import match_record
from guardian.yields.models import (
    ENABLED,
    LABEL_BASE,
    LABEL_INCENTIVE,
    LABEL_TOTAL,
    SCHEMA_VERSION,
    STATE_CAPABILITY,
    STATE_NETWORK,
    STATE_WALLET,
    BLOCKED_SOURCES,
    Holdings,
    NoisePolicy,
    PoolMapping,
    canonical,
    json_number,
    load_pool_mapping,
    policy_document,
    state_schema,
)
from guardian.yields.parsers import DECOMPRESSED_CAP, ParseResult, ParsedRecord, parse_source

Clock = Callable[[], float]
FETCH_URLS = (
    ("defillama_pools", "https://yields.invalid/pools"),
    ("binance_catalog_93", "https://promotions.invalid/binance/catalog/93"),
)
USER_AGENT = "WalletGuardianYieldFixtures/1"
_ACCEPT = "application/json"
_BUDGET = "yield-fixtures"
_FIELDS = (
    ("reported_base_yield", "reported_base_yield_canonical", LABEL_BASE, "base"),
    ("reported_incentive_reward_yield", "reported_incentive_reward_yield_canonical", LABEL_INCENTIVE, "reward"),
    ("reported_total", "reported_total_canonical", LABEL_TOTAL, "total"),
)
_OK = frozenset({"ok", "not_modified"})


@dataclass(frozen=True)
class _Step:
    comparison: str
    previous: dict[str, Any]
    pending: dict[str, Any] | None
    changes: tuple[dict[str, str], ...]
    semantic: str | None


def evaluate(
    database: Database,
    tenant: str,
    *,
    clock: Clock,
    backend: Any,
    resolve: Any,
    holdings: Holdings | None = None,
    promotions: bytes | None = None,
    policy: NoisePolicy | None = None,
    mapping: PoolMapping | None = None,
    sender: Any = None,
) -> dict[str, Any]:
    """Compare one offline observation. A sender argument is refused before any read."""
    if sender is not None:
        raise SecurityError("send_disabled", reason="send_disabled")
    _refuse_live(backend)
    if not callable(clock):
        raise GuardianError("clock_required")
    if not callable(resolve):
        raise GuardianError("fixture_resolve_required")
    chosen = policy or NoisePolicy()
    books = holdings or Holdings()
    pools = mapping or load_pool_mapping()
    observed_at = _stamp(clock)
    day = observed_at[:10]
    prior = _load(database, tenant)
    if prior is not None and prior.get("last_fetch_day") == day:
        return _report("not_due", prior, (), (), (), ())
    state = copy.deepcopy(prior) if prior is not None else _empty(chosen)
    state["policy"] = policy_document(chosen)
    state["enabled"] = False
    state["live_fetch"] = False
    state["sent"] = False
    if not isinstance(state.get("sources"), dict):
        state["sources"] = {}
    cooldown = state.setdefault("cooldown", {"alerted_slots": {}})
    if not isinstance(cooldown.get("alerted_slots"), dict):
        cooldown["alerted_slots"] = {}
    index = {
        (row["identity"]["source"], row["identity"]["record_id"]): row
        for row in state.get("records", [])
        if isinstance(row, dict)
    }
    errors: list[dict[str, str]] = []
    matches: list[dict[str, Any]] = []
    candidates: list[dict[str, Any]] = []
    fetched: list[str] = []
    outcomes = _fetch(backend, resolve, clock, state, day, fetched)
    if promotions is not None:
        outcomes.append(("explicit_body", _local_promotions(promotions, day)))
    for source_id, status in outcomes:
        state["sources"][source_id] = status["source"]
        parsed = status.get("parsed")
        if status["source"]["read_status"] not in _OK:
            errors.append({
                "source": source_id,
                "read_status": status["source"]["read_status"],
                "reason": status["source"]["reason"],
            })
        if not isinstance(parsed, ParseResult) or not parsed.ok:
            continue
        for record in parsed.records:
            stored, record_matches, record_candidates = _apply(
                record, index.get((record.source, record.record_id)), pools, books,
                observed_at, day, chosen, cooldown,
            )
            index[(record.source, record.record_id)] = stored
            matches.extend(record_matches)
            candidates.extend(record_candidates)
    state["records"] = [index[key] for key in sorted(index)]
    state["last_fetch_day"] = day
    state["schema_version"] = SCHEMA_VERSION
    validate(state, state_schema())
    _save(database, tenant, state, observed_at, errors)
    failed = [item for _source, item in outcomes if item["source"]["read_status"] not in _OK]
    if outcomes and len(failed) == len(outcomes):
        status = "error"
    elif errors:
        status = "partial"
    else:
        status = "ok"
    return _report(status, state, tuple(candidates), tuple(matches), tuple(errors), tuple(fetched))


def _apply(
    parsed: ParsedRecord,
    prior: dict[str, Any] | None,
    mapping: PoolMapping,
    holdings: Holdings,
    observed_at: str,
    day: str,
    policy: NoisePolicy,
    cooldown: dict[str, Any],
) -> tuple[dict[str, Any], tuple[dict[str, Any], ...], tuple[dict[str, Any], ...]]:
    identity, matches = match_record(parsed, mapping, holdings)
    step = _advance(prior, parsed, observed_at, day, policy)
    last_alert = _alert(prior)
    dedupe = _dedupe(prior)
    comparison = step.comparison
    if step.comparison == "sustained" and step.semantic and identity.get("pool_id"):
        emitted: list[dict[str, Any]] = []
        for match in matches:
            if match["eligibility"] != "held_position" or not match.get("wallet_id"):
                continue
            slot = f"{match['wallet_id']}|{identity['pool_id']}|{day}"
            if slot in cooldown["alerted_slots"]:
                continue
            if isinstance(last_alert, dict) and last_alert.get("wallet_id") == match["wallet_id"] and last_alert.get("semantic") == step.semantic:
                continue
            candidate = _candidate(parsed, match, identity["pool_id"], day, step, policy)
            cooldown["alerted_slots"][slot] = candidate["ref"]
            emitted.append(candidate)
            last_alert = {
                "ref": candidate["ref"],
                "kind": "yield_change",
                "wallet_id": match["wallet_id"],
                "pool_id": identity["pool_id"],
                "day": day,
                "semantic": step.semantic,
                "dedup_key": candidate["dedup_key"],
            }
            dedupe = {"last_semantic": step.semantic, "cooldown_day": day}
        if not emitted and any(item["eligibility"] == "held_position" for item in matches):
            comparison = "duplicate"
            dedupe = {"last_semantic": step.semantic, "cooldown_day": day}
        candidates = tuple(emitted)
    else:
        candidates = ()
    record = _record(parsed, identity, observed_at, step, comparison, last_alert, dedupe)
    return record, matches, candidates


def _advance(
    prior: dict[str, Any] | None,
    parsed: ParsedRecord,
    observed_at: str,
    day: str,
    policy: NoisePolicy,
) -> _Step:
    current = _snapshot(parsed, observed_at, day, None, None)
    baseline = None if prior is None else prior.get("comparison_state", {}).get("previous_valid_sample")
    if not isinstance(baseline, dict):
        return _Step("baseline", current, None, (), None)
    changes = tuple(_diff(baseline, parsed, policy))
    if not changes:
        return _Step("not_material", current, None, (), None)
    sustain_key = ",".join(f"{item['name']}:{item['direction']}" for item in changes)
    semantic = sustain_key + "@" + _yield_text(parsed)
    pending = prior.get("comparison_state", {}).get("pending_sample") if prior else None
    if isinstance(pending, dict) and pending.get("day") not in {None, day} and pending.get("sustain_key") == sustain_key:
        confirmed = _snapshot(parsed, observed_at, day, semantic, sustain_key)
        return _Step("sustained", confirmed, None, changes, semantic)
    waiting = _snapshot(parsed, observed_at, day, semantic, sustain_key)
    return _Step("pending", copy.deepcopy(baseline), waiting, changes, semantic)


def _diff(baseline: Mapping[str, Any], parsed: ParsedRecord, policy: NoisePolicy) -> list[dict[str, str]]:
    changes: list[dict[str, str]] = []
    for name, key, label, attr in _FIELDS:
        old = baseline.get(key)
        new = canonical(getattr(parsed, attr))
        direction = _direction(old if isinstance(old, str) else None, new, policy)
        if direction is None or new is None or not isinstance(old, str):
            continue
        changes.append({
            "name": name,
            "label": label,
            "direction": direction,
            "previous": old,
            "observed": new,
            "unit": parsed.unit,
        })
    return changes


def _direction(old: str | None, new: str | None, policy: NoisePolicy) -> str | None:
    if old is None or new is None:
        return None
    left = Decimal(old)
    right = Decimal(new)
    if left == right:
        return None
    delta = right - left
    absolute = abs(delta)
    if left == 0:
        if absolute >= policy.min_absolute_points:
            return "up" if delta > 0 else "down"
        return None
    relative = absolute / abs(left)
    if absolute >= policy.min_absolute_points and relative >= policy.min_relative:
        return "up" if delta > 0 else "down"
    return None


def _record(
    parsed: ParsedRecord,
    identity: dict[str, Any],
    observed_at: str,
    step: _Step,
    comparison: str,
    last_alert: dict[str, Any] | None,
    dedupe: dict[str, Any],
) -> dict[str, Any]:
    if parsed.read_coverage == "not_observable":
        coverage = "not_observable"
    elif identity["mapping_status"] in {"spoofed", "unsupported"}:
        coverage = "unsupported"
    elif identity["mapping_status"] in {"unmapped", "campaign_required"}:
        coverage = "unknown"
    else:
        coverage = parsed.read_coverage
    base = canonical(parsed.base)
    reward = canonical(parsed.reward)
    total = canonical(parsed.total)
    return {
        "schema_version": "yield-record.v1",
        "identity": {
            "source": parsed.source,
            "record_id": parsed.record_id,
            "pool_id": identity["pool_id"],
            "chain": identity["chain"],
            "protocol": identity["protocol"],
            "contracts": identity["contracts"],
            "mapping_status": identity["mapping_status"],
            "claimed_contracts": identity["claimed_contracts"],
        },
        "observation": {
            "published_at": parsed.published_at,
            "observed_at": observed_at,
            "freshness": "fresh",
            "content_hash": _content_hash(parsed),
        },
        "yields": {
            "reported_base_yield": json_number(base),
            "reported_base_yield_canonical": base,
            "reported_incentive_reward_yield": json_number(reward),
            "reported_incentive_reward_yield_canonical": reward,
            "reported_total": json_number(total),
            "reported_total_canonical": total,
            "base_label": LABEL_BASE,
            "incentive_label": LABEL_INCENTIVE,
            "total_label": LABEL_TOTAL,
            "unit": parsed.unit,
            "apr_converted_to_apy": False,
            "compounding_model": None,
            "reward_tokens": list(parsed.reward_tokens),
            "reward_token_status": parsed.reward_token_status,
            "reward_token_count_known": parsed.reward_token_count_known,
            "described_as_temporary": parsed.described_as_temporary,
            "duration_stated": parsed.duration_stated,
            "temporary_language": parsed.temporary_language,
        },
        "terms": {
            "conditions": parsed.conditions,
            "deadlines": [dict(item) for item in parsed.deadlines],
            "deadline_status": parsed.deadline_status,
            "excerpt": parsed.excerpt,
            "extraction_status": parsed.extraction_status,
            "provenance": [dict(item) for item in parsed.provenance],
            "residual_conditions": parsed.residual_conditions,
            "article_detail": parsed.article_detail,
            "catalog_status": parsed.catalog_status,
            "links_not_followed": parsed.links_not_followed,
        },
        "comparison_state": {
            "previous_valid_sample": step.previous,
            "pending_sample": step.pending,
            "last_alert": last_alert,
            "dedupe": dedupe,
            "comparison": comparison,
        },
        "read_coverage": coverage,
    }


def _snapshot(
    parsed: ParsedRecord,
    observed_at: str,
    day: str,
    semantic: str | None,
    sustain_key: str | None,
) -> dict[str, Any]:
    base = canonical(parsed.base)
    reward = canonical(parsed.reward)
    total = canonical(parsed.total)
    return {
        "content_hash": _content_hash(parsed),
        "observed_at": observed_at,
        "freshness": "fresh",
        "day": day,
        "semantic": semantic,
        "sustain_key": sustain_key,
        "reported_base_yield": json_number(base),
        "reported_base_yield_canonical": base,
        "reported_incentive_reward_yield": json_number(reward),
        "reported_incentive_reward_yield_canonical": reward,
        "reported_total": json_number(total),
        "reported_total_canonical": total,
        "unit": parsed.unit,
    }


def _candidate(
    parsed: ParsedRecord,
    match: Mapping[str, Any],
    pool_id: str,
    day: str,
    step: _Step,
    policy: NoisePolicy,
) -> dict[str, Any]:
    semantic = step.semantic or ""
    material = "\x1f".join((parsed.source, parsed.record_id, str(match["wallet_id"]), "yield_change", semantic))
    dedup_key = hashlib.sha256(material.encode("utf-8")).hexdigest()
    ref_material = "|".join((parsed.source, parsed.record_id, str(match["wallet_id"]), day, semantic))
    ref = "yld-" + hashlib.sha256(ref_material.encode("utf-8")).hexdigest()[:16]
    return {
        "kind": "yield_change",
        "wallet_id": match["wallet_id"],
        "pool_id": pool_id,
        "source": parsed.source,
        "record_id": parsed.record_id,
        "eligibility": "held_position",
        "via": "position",
        "day": day,
        "ref": ref,
        "dedup_key": dedup_key,
        "components": [dict(item) for item in step.changes],
        "unit": parsed.unit,
        "apr_converted_to_apy": False,
        "compounding_model": None,
        "described_as_temporary": parsed.described_as_temporary,
        "policy": policy_document(policy),
        "read_coverage": "partial",
    }


def _fetch(
    backend: Any,
    resolve: Any,
    clock: Clock,
    state: dict[str, Any],
    day: str,
    fetched: list[str],
) -> list[dict[str, Any]]:
    registry = EndpointRegistry({
        source_id: RegisteredSource(source_id, "yield_fixture", url, _BUDGET, False, None)
        for source_id, url in FETCH_URLS
    })
    budgets = BudgetBook.from_document(
        {_BUDGET: {"max_concurrency": 1, "max_requests_per_day": 8, "max_rps": 100, "retry_attempts": 0}},
        clock=clock,
        sleep=lambda _seconds: None,
    )
    transport = ReadOnlyTransport(
        registry, budgets, backend, resolve=resolve, max_response_bytes=DECOMPRESSED_CAP,
    )
    outcomes: list[tuple[str, dict[str, Any]]] = []
    stored = state.get("sources") if isinstance(state.get("sources"), dict) else {}
    for source_id, url in FETCH_URLS:
        fetched.append(url)
        prior = stored.get(source_id) if isinstance(stored.get(source_id), dict) else {}
        try:
            result = transport.request_allowlisted_get(
                source_id,
                conditional=_conditional(prior),
                accept=_ACCEPT,
                user_agent=USER_AGENT,
                max_response_bytes=DECOMPRESSED_CAP,
            )
        except (GuardianError, TransportError, FixtureMiss) as exc:
            reason = getattr(exc, "reason", None) or "fetch_error"
            if reason == "response_too_large":
                outcomes.append((source_id, _failed("too_large", "decompressed_cap", day)))
            else:
                outcomes.append((source_id, _failed("error", _short(str(reason)), day)))
            continue
        if result.status == 304:
            if prior.get("cached") is not True:
                outcomes.append((source_id, _failed("cache_missing", "304_without_cache", day)))
                continue
            outcomes.append((source_id, {
                "source": {
                    "cached": True,
                    "etag": _validator(result.headers.get("etag")) or prior.get("etag"),
                    "last_modified": _validator(result.headers.get("last-modified")) or prior.get("last_modified"),
                    "content_hash": prior.get("content_hash"),
                    "read_status": "not_modified",
                    "reason": "",
                    "fetched_on": day,
                },
            }))
            continue
        if result.status != 200 or not isinstance(result.body, (bytes, bytearray)):
            outcomes.append((source_id, _failed("error", "http_status", day)))
            continue
        body = bytes(result.body)
        parsed = parse_source(source_id, body)
        if not parsed.ok:
            status_name = "too_large" if parsed.reason == "decompressed_cap" else "rejected"
            reason = "decompressed_cap" if parsed.reason == "decompressed_cap" else parsed.reason
            outcomes.append((source_id, _failed(status_name, reason, day)))
            continue
        outcomes.append((source_id, {
            "source": {
                "cached": True,
                "etag": _validator(result.headers.get("etag")),
                "last_modified": _validator(result.headers.get("last-modified")),
                "content_hash": hashlib.sha256(body).hexdigest(),
                "read_status": "ok",
                "reason": "",
                "fetched_on": day,
            },
            "parsed": parsed,
        }))
    return outcomes


def _local_promotions(data: bytes, day: str) -> dict[str, Any]:
    parsed = parse_source("explicit_body", data)
    if not parsed.ok:
        status_name = "too_large" if parsed.reason == "decompressed_cap" else "rejected"
        reason = "decompressed_cap" if parsed.reason == "decompressed_cap" else parsed.reason
        return _failed(status_name, reason, day)
    return {
        "source": {
            "cached": False,
            "etag": None,
            "last_modified": None,
            "content_hash": hashlib.sha256(data).hexdigest(),
            "read_status": "ok",
            "reason": "",
            "fetched_on": day,
        },
        "parsed": parsed,
    }


def _failed(read_status: str, reason: str, day: str) -> dict[str, Any]:
    return {
        "source": {
            "cached": False,
            "etag": None,
            "last_modified": None,
            "content_hash": None,
            "read_status": read_status,
            "reason": _short(reason),
            "fetched_on": day,
        },
    }


def _conditional(prior: Mapping[str, Any]) -> dict[str, str] | None:
    headers: dict[str, str] = {}
    etag = prior.get("etag")
    modified = prior.get("last_modified")
    if isinstance(etag, str) and _validator(etag):
        headers["If-None-Match"] = etag
    if isinstance(modified, str) and _validator(modified):
        headers["If-Modified-Since"] = modified
    return headers or None


def _validator(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text or len(text) > 256 or any(mark in text for mark in "\r\n\x00"):
        return None
    return text


def _empty(policy: NoisePolicy) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "enabled": False,
        "live_fetch": False,
        "sent": False,
        "last_fetch_day": None,
        "policy": policy_document(policy),
        "sources": {},
        "cooldown": {"alerted_slots": {}},
        "records": [],
    }


def _load(database: Database, tenant: str) -> dict[str, Any] | None:
    for row in database.capability_state_for(tenant):
        if row.get("wallet_id") != STATE_WALLET or row.get("capability") != STATE_CAPABILITY:
            continue
        payload = row.get("payload")
        if not isinstance(payload, dict):
            continue
        state = payload.get("yield_promotions")
        if isinstance(state, dict):
            return state
    return None


def _save(database: Database, tenant: str, state: dict[str, Any], observed_at: str, errors: list[dict[str, str]]) -> None:
    fresh = any(
        isinstance(item, dict) and item.get("read_status") in _OK
        for item in state.get("sources", {}).values()
    )
    database.upsert_tenant(tenant, tenant)
    database.commit_capability_cycle(
        tenant,
        events=[],
        capability_rows=[{
            "wallet_id": STATE_WALLET,
            "network": STATE_NETWORK,
            "capability": STATE_CAPABILITY,
            "quality": "unknown" if errors or not fresh else "partial",
            "last_success_at": observed_at if fresh else None,
            "valid_until": None,
            "cursor": None,
            "payload": {"yield_promotions": state},
        }],
        cursors=[],
        alerts=[],
    )


def _report(
    status: str,
    state: dict[str, Any],
    candidates: tuple[dict[str, Any], ...],
    matches: tuple[dict[str, Any], ...],
    errors: tuple[dict[str, str], ...],
    fetched: tuple[str, ...],
) -> dict[str, Any]:
    ordered = tuple(sorted(candidates, key=lambda item: (item["pool_id"], item["wallet_id"], item["record_id"])))
    ordered_matches = tuple(sorted(
        matches,
        key=lambda item: (item["source"], item["record_id"], item["kind"], item.get("wallet_id") or ""),
    ))
    return {
        "enabled": ENABLED,
        "live_fetch": False,
        "sent": False,
        "scheduler": False,
        "status": status,
        "candidates": ordered,
        "matches": ordered_matches,
        "errors": errors,
        "records": state.get("records", []),
        "blocked_sources": [dict(item) for item in BLOCKED_SOURCES],
        "fetched_urls": fetched,
        "state": state,
    }


def _content_hash(parsed: ParsedRecord) -> str:
    payload = {
        "base": canonical(parsed.base),
        "deadlines": [item["original"] for item in parsed.deadlines],
        "excerpt": parsed.excerpt,
        "extraction_status": parsed.extraction_status,
        "record_id": parsed.record_id,
        "reward": canonical(parsed.reward),
        "reward_tokens": list(parsed.reward_tokens),
        "source": parsed.source,
        "total": canonical(parsed.total),
        "unit": parsed.unit,
    }
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _yield_text(parsed: ParsedRecord) -> str:
    return "|".join((
        canonical(parsed.base) or "null",
        canonical(parsed.reward) or "null",
        canonical(parsed.total) or "null",
        parsed.unit,
    ))


def _alert(prior: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(prior, dict):
        return None
    alert = prior.get("comparison_state", {}).get("last_alert")
    return dict(alert) if isinstance(alert, dict) else None


def _dedupe(prior: dict[str, Any] | None) -> dict[str, Any]:
    if isinstance(prior, dict):
        raw = prior.get("comparison_state", {}).get("dedupe")
        if isinstance(raw, dict):
            return {
                "last_semantic": raw.get("last_semantic") if isinstance(raw.get("last_semantic"), str) else None,
                "cooldown_day": raw.get("cooldown_day") if isinstance(raw.get("cooldown_day"), str) else None,
            }
    return {"last_semantic": None, "cooldown_day": None}


def _stamp(clock: Clock) -> str:
    return datetime.fromtimestamp(clock(), timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _short(reason: str) -> str:
    text = reason.strip() or "fetch_error"
    return text[:80]


def _refuse_live(backend: Any) -> None:
    if backend is None:
        raise GuardianError("fixture_backend_required")
    if backend.__class__.__name__ != "FixtureBackend":
        raise SecurityError("live_fetch_disabled", reason="live_fetch_disabled")
