"""RSS and Atom parsing from caller-supplied bytes. This module does not fetch.

DOCTYPE and ENTITY declarations are rejected before the parser runs, the same
rule as guardian.risk.ofac. Titles and links are data: they are not imported,
evaluated, or executed.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Mapping, Sequence
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from guardian.security.untrusted import sanitize_untrusted

_MAX_BYTES = 1_000_000
_KINDS = frozenset({"governance", "incident", "other", "upgrade"})
_PROTOCOL_ALIASES = {
    "aave": "aave_v3",
    "aave_v3": "aave_v3",
    "erc4626": "erc4626",
    "kinza": "kinza",
    "lido": "lido",
    "lista": "lista_slisbnb",
    "lista_slisbnb": "lista_slisbnb",
    "solana": "solana_stake",
    "solana_stake": "solana_stake",
    "uxlink": "uxlink",
}
_TITLE_PREFIX = re.compile(r"^(?:correction|update|repost|re|fwd)\s*:\s*", re.IGNORECASE)
_CORRECTION_PREFIX = re.compile(r"^(?:correction|update)\b", re.IGNORECASE)
_NOISE = re.compile(r"^(?:bep|eip|erc)\d+$")


@dataclass(frozen=True)
class NewsItem:
    source_id: str
    url: str
    title: str
    normalized_title: str
    published_at: datetime | None
    protocols: tuple[str, ...]
    kind: str
    correction_of: str | None
    is_correction: bool
    body: str


@dataclass(frozen=True)
class FeedParse:
    ok: bool
    items: tuple[NewsItem, ...]
    reason: str
    quarantined: int = 0


def canonicalize_url(url: str) -> str | None:
    """Lowercase the host, drop the fragment, and drop utm_* and ref query keys."""
    if not isinstance(url, str) or not url.strip():
        return None
    try:
        parts = urlsplit(url.strip())
        port = parts.port
    except ValueError:
        return None
    if parts.username or parts.password:
        return None
    scheme = parts.scheme.lower()
    if scheme not in {"http", "https"} or not parts.hostname:
        return None
    host = parts.hostname.lower().rstrip(".")
    netloc = host if port is None else f"{host}:{port}"
    query = []
    for key, value in parse_qsl(parts.query, keep_blank_values=True):
        folded = key.lower()
        if folded == "ref" or folded.startswith("utm_"):
            continue
        query.append((key, value))
    query.sort()
    return urlunsplit((scheme, netloc, parts.path, urlencode(query), ""))


def parse_feed(data: bytes, *, source_id: str, source_protocols: Sequence[str] = ()) -> FeedParse:
    """Parse RSS 2.0 or Atom bytes.

    A document that is truncated, compressed, or carries a DOCTYPE is rejected
    as a whole. One malformed item is quarantined; valid siblings are kept.
    """
    text, reason = _decode(data)
    if text is None:
        return FeedParse(False, (), reason)
    if not isinstance(source_id, str) or not source_id:
        return FeedParse(False, (), "malformed_item")
    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        return FeedParse(False, (), "unparseable")
    if _local(root.tag) not in {"rss", "feed"}:
        return FeedParse(False, (), "unparseable")
    elements = _entries(root)
    if len(elements) > 100:
        return FeedParse(False, (), "item_cap")
    items: list[NewsItem] = []
    quarantined = 0
    for element in elements:
        item = _from_xml(element, source_id, source_protocols)
        if item is None:
            quarantined += 1
            continue
        items.append(item)
        if len(items) > 100:
            return FeedParse(False, (), "item_cap", quarantined)
    if elements and not items:
        return FeedParse(False, (), "malformed_item", quarantined)
    return FeedParse(True, tuple(items), "", quarantined)


def parse_structured(
    rows: Sequence[Mapping[str, Any]],
    *,
    source_id: str | None = None,
    source_protocols: Sequence[str] = (),
) -> FeedParse:
    """Structured rows for manual X entries and fixtures. Text is not executed."""
    if not isinstance(rows, Sequence) or isinstance(rows, (str, bytes)):
        return FeedParse(False, (), "malformed_item")
    if len(rows) > 100:
        return FeedParse(False, (), "item_cap")
    items: list[NewsItem] = []
    quarantined = 0
    for row in rows:
        if not isinstance(row, Mapping):
            quarantined += 1
            continue
        item_source = row.get("source_id", source_id)
        item = _from_mapping(row, item_source, source_protocols)
        if item is None:
            quarantined += 1
            continue
        items.append(item)
    if rows and not items:
        return FeedParse(False, (), "malformed_item", quarantined)
    return FeedParse(True, tuple(items), "", quarantined)


def normalize_title(title: str) -> str:
    text = " ".join(title.split()).strip()
    while True:
        stripped = _TITLE_PREFIX.sub("", text)
        if stripped == text:
            break
        text = stripped.strip()
    return " ".join(text.casefold().split())


def resolve_protocols(
    title: str,
    body: str,
    declared: Sequence[str],
    source_protocols: Sequence[str],
) -> tuple[str, ...]:
    """Registered protocol names only. An unrelated subject such as 79thVault suppresses passing mentions."""
    declared_names = [item.strip() for item in declared if isinstance(item, str) and item.strip()]
    title_tokens = _tokens(title)
    if _unrelated_subject(declared_names, title_tokens):
        return ()
    found: set[str] = set()
    for name in list(declared_names) + list(source_protocols) + title_tokens + _tokens(body):
        canon = _PROTOCOL_ALIASES.get(str(name).strip().lower())
        if canon:
            found.add(canon)
    return tuple(sorted(found))


def _decode(data: bytes) -> tuple[str | None, str]:
    if not isinstance(data, (bytes, bytearray)):
        return None, "unparseable"
    if len(data) > _MAX_BYTES:
        return None, "unparseable"
    # Never inflate a compressed body. A bomb stays a rejected document.
    if data.startswith(b"\x1f\x8b") or data.startswith(b"PK\x03\x04") or data[:2] in {b"\x78\x01", b"\x78\x9c", b"\x78\xda"}:
        return None, "compressed_rejected"
    if data.startswith(b"\xff\xfe") or data.startswith(b"\xfe\xff"):
        return None, "unparseable"
    try:
        text = bytes(data).decode("utf-8-sig")
    except UnicodeError:
        return None, "unparseable"
    lowered = text.lower()
    if "<!doctype" in lowered or "<!entity" in lowered:
        return None, "dtd_or_entity_rejected"
    return text, ""


def _entries(root: ET.Element) -> list[ET.Element]:
    name = _local(root.tag)
    if name == "rss":
        found: list[ET.Element] = []
        for channel in list(root):
            if _local(channel.tag) != "channel":
                continue
            found.extend(child for child in list(channel) if _local(child.tag) == "item")
        return found
    if name == "feed":
        return [child for child in list(root) if _local(child.tag) == "entry"]
    return []


def _from_xml(element: ET.Element, source_id: str, source_protocols: Sequence[str]) -> NewsItem | None:
    title = _child_text(element, "title")
    url = canonicalize_url(_child_text(element, "link") or _link_href(element))
    if not title or url is None:
        return None
    published = _child_text(element, "pubDate") or _child_text(element, "updated") or _child_text(element, "published")
    body = _child_text(element, "description") or _child_text(element, "summary") or _child_text(element, "content")
    category = _child_text(element, "category")
    return _build(
        source_id=source_id,
        url=url,
        title=title,
        body=body,
        published=published,
        kind=category or None,
        declared=(),
        correction_of=None,
        source_protocols=source_protocols,
    )


def _from_mapping(row: Mapping[str, Any], source_id: Any, source_protocols: Sequence[str]) -> NewsItem | None:
    if not isinstance(source_id, str) or not source_id:
        return None
    title = row.get("title")
    url = canonicalize_url(row.get("url") if isinstance(row.get("url"), str) else "")
    if not isinstance(title, str) or not title.strip() or url is None:
        return None
    declared = row.get("protocols") or ()
    if not isinstance(declared, (list, tuple)) or any(not isinstance(item, str) for item in declared):
        return None
    body = row.get("body") or row.get("text") or ""
    if not isinstance(body, str):
        return None
    published = row.get("published_at")
    if published is not None and not isinstance(published, str):
        return None
    correction = row.get("correction_of")
    if correction is not None and not isinstance(correction, str):
        return None
    kind = row.get("kind")
    if kind is not None and not isinstance(kind, str):
        return None
    return _build(
        source_id=source_id,
        url=url,
        title=title.strip(),
        body=body,
        published=published,
        kind=kind,
        declared=tuple(declared),
        correction_of=correction,
        source_protocols=source_protocols,
    )


def _build(
    *,
    source_id: str,
    url: str,
    title: str,
    body: str,
    published: str | None,
    kind: str | None,
    declared: Sequence[str],
    correction_of: str | None,
    source_protocols: Sequence[str],
) -> NewsItem:
    title = sanitize_untrusted(title)
    body = sanitize_untrusted(body)
    canonical_correction = canonicalize_url(correction_of) if isinstance(correction_of, str) else None
    is_correction = canonical_correction is not None or _CORRECTION_PREFIX.match(title) is not None
    return NewsItem(
        source_id=source_id,
        url=url,
        title=title,
        normalized_title=normalize_title(title),
        published_at=parse_timestamp(published),
        protocols=resolve_protocols(title, body, declared, source_protocols),
        kind=_kind(kind, title),
        correction_of=canonical_correction,
        is_correction=is_correction,
        body=body,
    )


def parse_timestamp(value: str | None) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    iso = text[:-1] + "+00:00" if text.endswith("Z") else text
    parsed: datetime | None
    try:
        parsed = datetime.fromisoformat(iso)
    except ValueError:
        parsed = None
    if parsed is None:
        try:
            parsed = parsedate_to_datetime(text)
        except (IndexError, TypeError, ValueError, OverflowError):
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _kind(value: str | None, title: str) -> str:
    if isinstance(value, str) and value.strip().lower() in _KINDS:
        return value.strip().lower()
    folded = title.casefold()
    if any(word in folded for word in ("drained", "exploit", "hack", "incident")):
        return "incident"
    if "governance" in folded or "proposal" in folded:
        return "governance"
    if "upgrade" in folded:
        return "upgrade"
    return "other"


def _unrelated_subject(declared: Sequence[str], title_tokens: Sequence[str]) -> bool:
    for name in declared:
        if name.strip().lower() not in _PROTOCOL_ALIASES:
            return True
    for token in title_tokens:
        if token in _PROTOCOL_ALIASES or _NOISE.fullmatch(token):
            continue
        if re.search(r"\d", token) and re.search(r"[a-z]", token):
            return True
    return False


def _tokens(text: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", text.casefold())


def _local(tag: str) -> str:
    if not isinstance(tag, str):
        return ""
    if tag.startswith("{"):
        return tag.rsplit("}", 1)[-1].lower()
    return tag.lower()


def _child_text(element: ET.Element, name: str) -> str:
    for child in list(element):
        if _local(child.tag) == name.lower() and child.text and child.text.strip():
            return _display_text(child.text)
    return ""


def _display_text(value: str) -> str:
    """Collapse ordinary wrapping spaces. Keep newlines and other controls as data."""
    pieces: list[str] = []
    buffer: list[str] = []

    def flush() -> None:
        if buffer:
            pieces.append(" ".join("".join(buffer).split()))
            buffer.clear()

    for char in value.strip():
        if char in "\n\r" or (ord(char) < 32 and char != "\t") or char in "\u202a\u202b\u202c\u202d\u202e\u2066\u2067\u2068\u2069":
            flush()
            pieces.append(char)
            continue
        buffer.append(char)
    flush()
    return "".join(pieces)


def _link_href(element: ET.Element) -> str:
    for child in list(element):
        if _local(child.tag) != "link":
            continue
        href = child.attrib.get("href")
        if isinstance(href, str) and href.strip():
            return href.strip()
    return ""
