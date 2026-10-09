"""Feed health for configured news sources. A stale or missing feed is not a clear result."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import timezone
from typing import Any

from guardian.news.catalog import NewsSource
from guardian.news.feeds import parse_timestamp
from guardian.storage import Database

STALE_AFTER_DAYS = 180
_TYPES = frozenset({"rss", "atom", "json_status", "none"})


def detect_feed_type(body: bytes | str | None) -> str:
    """rss, atom, json_status, or none. Detection does not execute the body."""
    if body is None:
        return "none"
    if isinstance(body, bytes):
        text = body.decode("utf-8", "replace")
    elif isinstance(body, str):
        text = body
    else:
        return "none"
    stripped = text.lstrip("\ufeff \t\r\n")
    if not stripped:
        return "none"
    if stripped[0] in "{[":
        return "json_status"
    head = stripped[:2000].lower()
    if "<feed" in head:
        return "atom"
    if "<rss" in head or "<channel" in head:
        return "rss"
    return "none"


def feed_type_for(kind: str, body: bytes | str | None = None) -> str:
    detected = detect_feed_type(body) if body is not None else "none"
    if detected != "none":
        return detected
    if kind in _TYPES:
        return kind
    return "none"


def stale_reason_for(last_item_at: str | None, now: str | None, *, missing: bool) -> str | None:
    """Age is measured in whole days. A future item is not stale. A missing feed is feed_missing."""
    if missing:
        return "feed_missing"
    if not isinstance(last_item_at, str) or not last_item_at or not isinstance(now, str) or not now:
        return None
    item = parse_timestamp(last_item_at)
    current = parse_timestamp(now)
    if item is None or current is None:
        return None
    days = (current.astimezone(timezone.utc).date() - item.astimezone(timezone.utc).date()).days
    if days < 0 or days <= STALE_AFTER_DAYS:
        return None
    return f"no_item_since:{days}d"


def build_feed_health(
    sources: Mapping[str, NewsSource],
    stored: Mapping[str, Any],
    now: str | None,
) -> list[dict[str, Any]]:
    """One public row per catalog source. List bodies and item titles are not copied."""
    by_source: dict[str, list[dict[str, Any]]] = {}
    for record in stored.values():
        if not isinstance(record, dict):
            continue
        source_id = record.get("source_id")
        if isinstance(source_id, str) and source_id:
            by_source.setdefault(source_id, []).append(record)
    rows: list[dict[str, Any]] = []
    for source in sorted(sources.values(), key=lambda item: item.source_id):
        allowlisted = [feed for feed in source.feeds if feed.fetch == "allowlisted_get"]
        if not allowlisted:
            rows.append(_public(source.source_id, "none", None, None, "empty", "feed_missing"))
            continue
        records = by_source.get(source.source_id, [])
        if not records:
            kind = allowlisted[0].kind if allowlisted[0].kind in {"rss", "atom", "json_status"} else "rss"
            rows.append(_public(source.source_id, kind, None, None, "empty", None))
            continue
        rows.append(_from_records(source.source_id, records, now))
    return rows


def stored_feed_health(database: Database, tenant: str) -> list[dict[str, Any]]:
    """Read the health stored by the last fetch. This function does not open a socket."""
    from guardian.news.catalog import load_news_sources

    state = _load_news_state(database, tenant)
    health = state.get("health")
    if isinstance(health, list) and health and all(isinstance(row, dict) for row in health):
        return [_public_copy(row) for row in health]
    stored = state.get("feeds") if isinstance(state.get("feeds"), dict) else {}
    return build_feed_health(load_news_sources(), stored, None)


def _from_records(source_id: str, records: Sequence[Mapping[str, Any]], now: str | None) -> dict[str, Any]:
    feed_type = "none"
    for record in records:
        kind = record.get("feed_type")
        if isinstance(kind, str) and kind in {"rss", "atom", "json_status"}:
            feed_type = kind
            break
    last_fetch = _latest(record.get("last_fetch_at") for record in records)
    last_item = _latest(record.get("last_item_at") for record in records)
    parse_status = _parse_status(records)
    held = sum(r.get("quarantined_items") for r in records if isinstance(r.get("quarantined_items"), int) and not isinstance(r.get("quarantined_items"), bool))
    return _public(
        source_id,
        feed_type,
        last_fetch,
        last_item,
        parse_status,
        stale_reason_for(last_item, now, missing=False),
        held,
    )


def _parse_status(records: Sequence[Mapping[str, Any]]) -> str:
    statuses = [record.get("parse_status") for record in records if isinstance(record.get("parse_status"), str)]
    if "ok" in statuses:
        return "ok"
    if "empty" in statuses:
        return "empty"
    if "parse_error" in statuses:
        return "parse_error"
    if statuses:
        return "http_error"
    raw = [record.get("status") for record in records]
    if "ok" in raw:
        return "ok"
    if "empty" in raw:
        return "empty"
    reasons = [record.get("reason") for record in records]
    if any(reason in {"unparseable", "malformed_item", "dtd_or_entity_rejected", "item_cap"} for reason in reasons):
        return "parse_error"
    return "http_error"


def _latest(values) -> str | None:
    found = [value for value in values if isinstance(value, str) and value]
    if not found:
        return None
    return max(found)


def _public(
    source_id: str,
    feed_type: str,
    last_fetch_at: str | None,
    last_item_at: str | None,
    parse_status: str,
    stale_reason: str | None,
    quarantined_items: int = 0,
) -> dict[str, Any]:
    if feed_type not in _TYPES:
        feed_type = "none"
    if parse_status not in {"ok", "empty", "parse_error", "http_error"}:
        parse_status = "http_error"
    return {
        "feed_type": feed_type,
        "last_fetch_at": last_fetch_at,
        "last_item_at": last_item_at,
        "parse_status": parse_status,
        "quarantined_items": quarantined_items if isinstance(quarantined_items, int) and quarantined_items > 0 else 0,
        "source_id": source_id,
        "stale_reason": stale_reason,
    }


def _public_copy(row: Mapping[str, Any]) -> dict[str, Any]:
    return _public(
        str(row.get("source_id") or ""),
        str(row.get("feed_type") or "none"),
        row.get("last_fetch_at") if isinstance(row.get("last_fetch_at"), str) else None,
        row.get("last_item_at") if isinstance(row.get("last_item_at"), str) else None,
        str(row.get("parse_status") or "empty"),
        row.get("stale_reason") if isinstance(row.get("stale_reason"), str) else None,
        row.get("quarantined_items") if isinstance(row.get("quarantined_items"), int) else 0,
    )


def _load_news_state(database: Database, tenant: str) -> dict[str, Any]:
    from guardian.news.fetch import NEWS_STATE_CAPABILITY, NEWS_STATE_WALLET

    for row in database.capability_state_for(tenant):
        if row.get("wallet_id") != NEWS_STATE_WALLET or row.get("capability") != NEWS_STATE_CAPABILITY:
            continue
        payload = row.get("payload")
        if isinstance(payload, dict) and isinstance(payload.get("news_fetch"), dict):
            return payload["news_fetch"]
    return {}
