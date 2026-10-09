"""News-source config. This module records URLs. It does not fetch them.

The only catalog is ``config/news-sources/``. ``config/news/sources.json`` is not
a second copy. X accounts stay manual. Allowlisted GET is limited to the RSS
URLs a public check actually returned. There is no API key and no paid provider.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from guardian.news.feeds import _PROTOCOL_ALIASES
from guardian.resources import resource_path

NEWS_DIR = resource_path("config", "news-sources")
_DATE = re.compile(r"^20[0-9]{2}-[01][0-9]-[0-3][0-9]$")
_KINDS = frozenset({"announcements", "blog", "rss", "status", "x_account"})
_FETCH_MANUAL = "manual_not_fetched_by_monitor"
_FETCH_NONE = "not_fetched_by_monitor"
_FETCH_GET = "allowlisted_get"
_FETCHES = frozenset({_FETCH_GET, _FETCH_MANUAL, _FETCH_NONE})
_FEED_KEYS = frozenset({"fetch", "kind", "max_bytes", "url", "verified"})
_MAX_FEED_BYTES = 1_048_576
_AAVE_FEED = "https://governance.aave.com/latest.rss?order=created"
_AAVE_MIN_BYTES = 512 * 1024
# Public GETs on 2026-10-09. Do not add a guessed /feed or /rss URL.
ALLOWLISTED_FEED_URLS = frozenset({
    _AAVE_FEED,
    "https://medium.com/feed/@kinzafinance",
    "https://status.solana.com/history.rss",
})
_FORBIDDEN = frozenset({
    "__import__",
    "api_key",
    "apikey",
    "class",
    "class_path",
    "credential",
    "entrypoint",
    "eval",
    "exec",
    "import",
    "importlib",
    "module",
    "module_path",
    "token",
})


class NewsConfigError(ValueError):
    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


@dataclass(frozen=True)
class NewsFeed:
    url: str
    kind: str
    fetch: str
    verified: str | None = None
    max_bytes: int | None = None


@dataclass(frozen=True)
class NewsSource:
    source_id: str
    protocols: tuple[str, ...]
    official: bool
    retrieved: str
    feeds: tuple[NewsFeed, ...]
    notes: str


def load_news_sources(directory: Path | None = None) -> dict[str, NewsSource]:
    root = Path(directory) if directory is not None else NEWS_DIR
    found: dict[str, NewsSource] = {}
    for path in sorted(root.glob("*.json")):
        document = json.loads(path.read_text(encoding="utf-8"))
        source = load_news_source(document)
        if source.source_id in found:
            raise NewsConfigError("duplicate_source")
        found[source.source_id] = source
    return found


def load_news_source(document: Mapping[str, Any]) -> NewsSource:
    if not isinstance(document, Mapping):
        raise NewsConfigError("not_object")
    _reject(document)
    source_id = document.get("id")
    if not isinstance(source_id, str) or not source_id:
        raise NewsConfigError("id")
    if document.get("official") is not True:
        raise NewsConfigError("official")
    retrieved = document.get("retrieved")
    if not isinstance(retrieved, str) or _DATE.fullmatch(retrieved) is None:
        raise NewsConfigError("retrieved")
    protocols = document.get("protocols")
    if not isinstance(protocols, list) or not protocols:
        raise NewsConfigError("protocols")
    if any(not isinstance(item, str) or item.strip().lower() not in _PROTOCOL_ALIASES for item in protocols):
        raise NewsConfigError("protocols")
    feeds_raw = document.get("feeds")
    if not isinstance(feeds_raw, list) or not feeds_raw:
        raise NewsConfigError("feeds")
    feeds = tuple(_feed(item) for item in feeds_raw)
    notes = document.get("notes", "")
    if not isinstance(notes, str):
        raise NewsConfigError("notes")
    return NewsSource(source_id, tuple(protocols), True, retrieved, feeds, notes)


def _feed(item: Any) -> NewsFeed:
    if not isinstance(item, Mapping):
        raise NewsConfigError("feed")
    if any(key not in _FEED_KEYS for key in item):
        raise NewsConfigError("feed_field")
    url = item.get("url")
    kind = item.get("kind")
    fetch = item.get("fetch")
    if not isinstance(url, str) or not url.startswith("https://"):
        raise NewsConfigError("feed_url")
    if kind not in _KINDS:
        raise NewsConfigError("feed_kind")
    if fetch not in _FETCHES:
        raise NewsConfigError("fetch")
    if kind == "x_account":
        if fetch != _FETCH_MANUAL:
            raise NewsConfigError("x_fetch")
        _reject_fetch_fields(item)
        return NewsFeed(url, str(kind), str(fetch))
    if fetch == _FETCH_NONE:
        _reject_fetch_fields(item)
        return NewsFeed(url, str(kind), str(fetch))
    if fetch != _FETCH_GET or kind != "rss" or url not in ALLOWLISTED_FEED_URLS:
        raise NewsConfigError("fetch")
    verified = item.get("verified")
    if not isinstance(verified, str) or _DATE.fullmatch(verified) is None:
        raise NewsConfigError("verified")
    max_bytes = item.get("max_bytes")
    if isinstance(max_bytes, bool) or not isinstance(max_bytes, int):
        raise NewsConfigError("max_bytes")
    if max_bytes < 1 or max_bytes > _MAX_FEED_BYTES:
        raise NewsConfigError("max_bytes")
    if url == _AAVE_FEED and max_bytes < _AAVE_MIN_BYTES:
        raise NewsConfigError("max_bytes")
    return NewsFeed(url, str(kind), str(fetch), verified, max_bytes)


def _reject_fetch_fields(item: Mapping[str, Any]) -> None:
    if "verified" in item or "max_bytes" in item:
        raise NewsConfigError("feed_field")


def _reject(value: Any) -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            folded = str(key).strip().lower()
            if folded in _FORBIDDEN or folded == "enabled":
                raise NewsConfigError(f"forbidden_key:{key}")
            _reject(item)
        return
    if isinstance(value, list):
        for item in value:
            _reject(item)
        return
    if isinstance(value, str) and value.strip().lower() == "live_pass":
        raise NewsConfigError("live_pass")
