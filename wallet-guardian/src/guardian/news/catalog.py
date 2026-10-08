"""News-source config. URLs are recorded. This package does not fetch them.

X accounts are manual. There is no API key and no paid news provider.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from guardian.news.feeds import _PROTOCOL_ALIASES

_ROOT = Path(__file__).resolve().parents[3]
NEWS_DIR = _ROOT / "config" / "news-sources"
_DATE = re.compile(r"^20[0-9]{2}-[01][0-9]-[0-3][0-9]$")
_KINDS = frozenset({"announcements", "blog", "rss", "status", "x_account"})
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
    url = item.get("url")
    kind = item.get("kind")
    fetch = item.get("fetch")
    if not isinstance(url, str) or not url.startswith("https://"):
        raise NewsConfigError("feed_url")
    if kind not in _KINDS:
        raise NewsConfigError("feed_kind")
    if kind == "x_account":
        if fetch != "manual_not_fetched_by_monitor":
            raise NewsConfigError("x_fetch")
    elif fetch != "not_fetched_by_monitor":
        raise NewsConfigError("fetch")
    return NewsFeed(url, str(kind), str(fetch))


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
