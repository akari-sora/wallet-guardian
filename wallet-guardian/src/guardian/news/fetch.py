"""Allowlisted GET of official RSS and Atom feeds.

Requests go through ReadOnlyTransport. This module does not call httpx or
urllib, does not send a wallet identifier, and does not attach a credential.
A rejected or unparsable body is a per-feed error, not an empty news result.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlsplit

from guardian.errors import GuardianError
from guardian.news.catalog import ALLOWLISTED_FEED_URLS, NewsFeed, NewsSource, load_news_sources
from guardian.news.feeds import NewsItem, parse_feed, parse_timestamp
from guardian.news.incidents import Incident, IncidentBook, SourceRef
from guardian.sources.budget import BudgetBook, BudgetExhausted
from guardian.sources.capabilities import news_fetch_capability
from guardian.sources.transport import EndpointRegistry, ReadOnlyTransport, RegisteredSource
from guardian.storage import Database

NEWS_GETS_PER_RUN = 8
# The feed state is not a wallet. Wallet ids are UUIDs, so this key cannot
# collide with a monitored wallet, and "news" is not a chain network.
NEWS_STATE_WALLET = "news-feeds"
NEWS_STATE_NETWORK = "news"
NEWS_STATE_CAPABILITY = "exposure_news"
_ACCEPT = "application/rss+xml, application/atom+xml, application/xml, text/xml"
_BUDGET = "news-feeds"
_KEPT = ("etag", "fingerprints", "last_modified", "last_ok_at")
_FRESH = frozenset({"empty", "not_modified", "ok"})
# Linkable incidents: published within this window (stored ones included, so a
# 304 still serves a newly exposed wallet), or new/revised in this fetch when the
# feed already had a successful fetch. A feed's first fetch is a quiet baseline
# for older or undated items, so enabling fetch cannot replay a feed's history.
_BASELINE_WINDOW_SECONDS = 72 * 3600
# Stored incidents are capped, newest first, so the state row stays bounded.
_MAX_INCIDENTS = 300
_MAX_REFS = 10

Clock = Callable[[], float]


@dataclass(frozen=True)
class FeedResult:
    source_id: str
    url: str
    status: str
    reason: str
    protocols: tuple[str, ...]


@dataclass
class FetchReport:
    book: IncidentBook
    feeds: tuple[FeedResult, ...]
    updated_ids: frozenset[str]
    content_hashes: dict[str, str]
    state: dict[str, Any]
    fetchable: frozenset[str]
    linkable: frozenset[str] = frozenset()


def fetch_allowlisted_news(
    database: Database,
    tenant: str,
    backend: Any,
    *,
    resolve,
    now: str,
    clock: Clock,
) -> FetchReport:
    """Fetch each allowlisted feed once. The endpoint is the committed URL."""
    sources = load_news_sources()
    capability = news_fetch_capability(sources)
    pairs = _pairs(sources)
    previous = _load_state(database, tenant)
    book, hashes = _restore(previous)
    updated: set[str] = set()
    linkable: set[str] = set()
    prior_feeds = previous.get("feeds") if isinstance(previous.get("feeds"), dict) else {}
    stored: dict[str, Any] = {}
    results: list[FeedResult] = []
    if not pairs:
        return _report(book, (), updated, hashes, stored, sources, linkable)
    registry = _registry(pairs)
    budgets = BudgetBook.from_document(
        {
            _BUDGET: {
                "max_concurrency": 1,
                "max_requests_per_day": NEWS_GETS_PER_RUN,
                "max_rps": 100,
                "retry_attempts": 0,
            }
        },
        clock=clock,
        sleep=lambda _seconds: None,
    )
    transport = ReadOnlyTransport(registry, budgets, backend, resolve=resolve, max_response_bytes=1_048_576)
    allowed_hosts = set(capability.hosts)
    for index, (source, feed) in enumerate(pairs):
        protocols = tuple(source.protocols)
        host = (urlsplit(feed.url).hostname or "").lower().rstrip(".")
        prior = _prior(prior_feeds.get(feed.url))
        if feed.url not in ALLOWLISTED_FEED_URLS or host not in allowed_hosts or capability.http_method != "GET":
            stored[feed.url] = _failed(prior, source.source_id, "host_not_allowlisted")
            results.append(FeedResult(source.source_id, feed.url, "fetch_error", "host_not_allowlisted", protocols))
            continue
        if capability.credential is not None:
            stored[feed.url] = _failed(prior, source.source_id, "news_credential_forbidden")
            results.append(FeedResult(source.source_id, feed.url, "fetch_error", "news_credential_forbidden", protocols))
            continue
        try:
            result = transport.request_allowlisted_get(
                f"news-{index}",
                conditional=_conditional(prior),
                accept=_ACCEPT,
                user_agent=capability.user_agent,
                max_response_bytes=feed.max_bytes if isinstance(feed.max_bytes, int) else 1_048_576,
            )
        except BudgetExhausted:
            stored[feed.url] = _failed(prior, source.source_id, "budget_exhausted")
            results.append(FeedResult(source.source_id, feed.url, "budget_exhausted", "budget_exhausted", protocols))
            continue
        except GuardianError as exc:
            reason = exc.reason if isinstance(getattr(exc, "reason", None), str) and exc.reason else "fetch_error"
            stored[feed.url] = _failed(prior, source.source_id, reason)
            results.append(FeedResult(source.source_id, feed.url, "fetch_error", reason, protocols))
            continue
        if result.status == 304:
            if "fingerprints" not in prior:
                stored[feed.url] = _failed(prior, source.source_id, "cache_missing")
                results.append(FeedResult(source.source_id, feed.url, "fetch_error", "cache_missing", protocols))
                continue
            record = dict(prior)
            record["status"] = "not_modified"
            record["reason"] = ""
            record["last_ok_at"] = now
            record["source_id"] = source.source_id
            etag = _validator(result.headers.get("etag"))
            modified = _validator(result.headers.get("last-modified"))
            if etag:
                record["etag"] = etag
            if modified:
                record["last_modified"] = modified
            stored[feed.url] = record
            results.append(FeedResult(source.source_id, feed.url, "not_modified", "", protocols))
            continue
        if result.status != 200 or not isinstance(result.body, (bytes, bytearray)):
            stored[feed.url] = _failed(prior, source.source_id, "http_status")
            results.append(FeedResult(source.source_id, feed.url, "fetch_error", "http_status", protocols))
            continue
        parsed = parse_feed(bytes(result.body), source_id=source.source_id, source_protocols=source.protocols)
        if not parsed.ok:
            reason = parsed.reason or "unparseable"
            stored[feed.url] = _failed(prior, source.source_id, reason)
            results.append(FeedResult(source.source_id, feed.url, "fetch_error", reason, protocols))
            continue
        baseline = "fingerprints" not in prior
        for incident_id in _absorb(book, hashes, updated, parsed.items):
            incident = next(item for item in book.incidents if item.incident_id == incident_id)
            if not baseline or _recent(incident.published_at, clock()):
                linkable.add(incident_id)
        stored[feed.url] = {
            "etag": _validator(result.headers.get("etag")),
            "fingerprints": [_fingerprint(item) for item in parsed.items],
            "last_modified": _validator(result.headers.get("last-modified")),
            "last_ok_at": now,
            "reason": "",
            "source_id": source.source_id,
            "status": "ok" if parsed.items else "empty",
        }
        status = "ok" if parsed.items else "empty"
        results.append(FeedResult(source.source_id, feed.url, status, "", protocols))
    moment = clock()
    for incident in book.incidents:
        if _recent(incident.published_at, moment):
            linkable.add(incident.incident_id)
    return _report(book, tuple(results), updated, hashes, stored, sources, linkable)


def news_state_row(state: Mapping[str, Any], now: str) -> dict[str, Any]:
    """One capability_state row. It is not a wallet and it has no coverage claim."""
    feeds = state.get("feeds") if isinstance(state.get("feeds"), Mapping) else {}
    fresh = any(isinstance(item, Mapping) and item.get("status") in _FRESH for item in feeds.values())
    return {
        "capability": NEWS_STATE_CAPABILITY,
        "cursor": None,
        "last_success_at": now if fresh else None,
        "network": NEWS_STATE_NETWORK,
        "payload": {"news_fetch": dict(state)},
        "quality": "partial",
        "valid_until": None,
        "wallet_id": NEWS_STATE_WALLET,
    }


def _report(
    book: IncidentBook,
    feeds: tuple[FeedResult, ...],
    updated: set[str],
    hashes: dict[str, str],
    stored: dict[str, Any],
    sources: Mapping[str, NewsSource],
    linkable: set[str] | None = None,
) -> FetchReport:
    fetchable = frozenset(
        protocol
        for source in sources.values()
        if any(feed.fetch == "allowlisted_get" for feed in source.feeds)
        for protocol in source.protocols
    )
    return FetchReport(
        book=book,
        feeds=feeds,
        updated_ids=frozenset(updated),
        linkable=frozenset(linkable or ()),
        content_hashes=dict(hashes),
        state={"feeds": stored, "incidents": [_dump(incident, hashes.get(incident.incident_id, "")) for incident in _bounded(book.incidents)]},
        fetchable=fetchable,
    )


def _pairs(sources: Mapping[str, NewsSource]) -> list[tuple[NewsSource, NewsFeed]]:
    found: list[tuple[NewsSource, NewsFeed]] = []
    for source in sorted(sources.values(), key=lambda item: item.source_id):
        for feed in source.feeds:
            if feed.fetch == "allowlisted_get":
                found.append((source, feed))
    return found


def _registry(pairs: list[tuple[NewsSource, NewsFeed]]) -> EndpointRegistry:
    registered: dict[str, RegisteredSource] = {}
    for index, (_source, feed) in enumerate(pairs):
        source_id = f"news-{index}"
        registered[source_id] = RegisteredSource(
            source_id=source_id,
            adapter="news_feed",
            endpoint=feed.url,
            budget_group=_BUDGET,
            allow_private_network=False,
            credential=None,
        )
    return EndpointRegistry(registered)


def _load_state(database: Database, tenant: str) -> dict[str, Any]:
    for row in database.capability_state_for(tenant):
        if row.get("wallet_id") != NEWS_STATE_WALLET or row.get("capability") != NEWS_STATE_CAPABILITY:
            continue
        payload = row.get("payload")
        if not isinstance(payload, dict):
            continue
        state = payload.get("news_fetch")
        if isinstance(state, dict):
            return state
    return {"feeds": {}, "incidents": []}


def _prior(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        return {}
    return {key: raw[key] for key in _KEPT if key in raw}


def _failed(prior: Mapping[str, Any], source_id: str, reason: str) -> dict[str, Any]:
    record = dict(prior)
    record["reason"] = reason
    record["source_id"] = source_id
    record["status"] = "budget_exhausted" if reason == "budget_exhausted" else "fetch_error"
    return record


def _conditional(prior: Mapping[str, Any]) -> dict[str, str]:
    headers: dict[str, str] = {}
    etag = prior.get("etag")
    modified = prior.get("last_modified")
    if isinstance(etag, str) and _validator(etag):
        headers["If-None-Match"] = etag.strip()
    if isinstance(modified, str) and _validator(modified):
        headers["If-Modified-Since"] = modified.strip()
    return headers


def _validator(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text or len(text) > 256 or any(mark in text for mark in "\r\n\x00"):
        return None
    return text


def _absorb(book: IncidentBook, hashes: dict[str, str], updated: set[str], items: tuple[NewsItem, ...]) -> list[str]:
    """Absorb items. Returns the incident ids that are new or changed in this fetch."""
    changed: list[str] = []
    for item in items:
        digest = _item_hash(item)
        match = book._find(item)
        if match is not None and hashes.get(match.incident_id) == digest:
            continue
        known = match is not None
        incident = book.add(item)
        incident.source_refs[:] = incident.source_refs[-_MAX_REFS:]
        previous = hashes.get(incident.incident_id)
        hashes[incident.incident_id] = digest
        if known or (previous is not None and previous != digest):
            updated.add(incident.incident_id)
        if incident.incident_id not in changed:
            changed.append(incident.incident_id)
    return changed


def _recent(published: Any, now: float) -> bool:
    if not isinstance(published, datetime):
        return False
    return published.timestamp() >= now - _BASELINE_WINDOW_SECONDS


def _bounded(incidents: list[Incident]) -> list[Incident]:
    if len(incidents) <= _MAX_INCIDENTS:
        return list(incidents)
    ranked = sorted(
        incidents,
        key=lambda item: item.published_at.timestamp() if isinstance(item.published_at, datetime) else float("-inf"),
        reverse=True,
    )
    keep = {item.incident_id for item in ranked[:_MAX_INCIDENTS]}
    return [item for item in incidents if item.incident_id in keep]


def _restore(state: Mapping[str, Any]) -> tuple[IncidentBook, dict[str, str]]:
    book = IncidentBook()
    hashes: dict[str, str] = {}
    rows = state.get("incidents")
    if not isinstance(rows, list):
        return book, hashes
    for row in rows:
        loaded = _load_incident(row)
        if loaded is None:
            continue
        incident, digest = loaded
        book.incidents.append(incident)
        book.alert_ids.append(incident.alert_id)
        if digest:
            hashes[incident.incident_id] = digest
    return book, hashes


def _load_incident(row: Any) -> tuple[Incident, str] | None:
    if not isinstance(row, Mapping):
        return None
    incident_id = row.get("incident_id")
    title = row.get("title")
    normalized = row.get("normalized_title")
    kind = row.get("kind")
    urls = row.get("urls")
    protocols = row.get("protocols")
    alert_id = row.get("alert_id")
    if not all(isinstance(item, str) and item for item in (incident_id, title, normalized, kind, alert_id)):
        return None
    if not isinstance(urls, list) or not urls or any(not isinstance(item, str) or not item for item in urls):
        return None
    if not isinstance(protocols, list) or any(not isinstance(item, str) for item in protocols):
        return None
    refs = _load_refs(row.get("source_refs"), urls)
    published = row.get("published_at")
    if published is not None and not isinstance(published, str):
        return None
    digest = row.get("content_hash")
    if digest is not None and not isinstance(digest, str):
        return None
    incident = Incident(
        incident_id=str(incident_id),
        protocols=tuple(protocols),
        title=str(title),
        normalized_title=str(normalized),
        kind=str(kind),
        urls=list(urls),
        source_refs=refs,
        published_at=parse_timestamp(published) if isinstance(published, str) else None,
        corrected=row.get("corrected") is True,
        alert_id=str(alert_id),
    )
    return incident, digest if isinstance(digest, str) else ""


def _load_refs(raw: Any, urls: list[str]) -> list[SourceRef]:
    refs: list[SourceRef] = []
    if isinstance(raw, list):
        for item in raw:
            if not isinstance(item, Mapping):
                continue
            source_id = item.get("source_id")
            url = item.get("url")
            if not isinstance(source_id, str) or not source_id or not isinstance(url, str) or not url:
                continue
            published = item.get("published_at")
            moment = parse_timestamp(published) if isinstance(published, str) else None
            refs.append(SourceRef(source_id, url, moment))
    if refs:
        return refs
    return [SourceRef("allowlisted_get", url, None) for url in urls]


def _dump(incident: Incident, digest: str) -> dict[str, Any]:
    return {
        "alert_id": incident.alert_id,
        "content_hash": digest,
        "corrected": incident.corrected,
        "incident_id": incident.incident_id,
        "kind": incident.kind,
        "normalized_title": incident.normalized_title,
        "protocols": list(incident.protocols),
        "published_at": _stamp(incident.published_at),
        "source_refs": [
            {"published_at": _stamp(ref.published_at), "source_id": ref.source_id, "url": ref.url}
            for ref in incident.source_refs
        ],
        "title": incident.title,
        "urls": list(incident.urls),
    }


def _stamp(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _item_hash(item: NewsItem) -> str:
    material = "\n".join((item.url, item.title, item.body)).encode("utf-8")
    return hashlib.sha256(material).hexdigest()


def _fingerprint(item: NewsItem) -> str:
    return _item_hash(item)
