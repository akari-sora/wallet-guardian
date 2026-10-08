"""Local SDN XML. Exact identifier match only. A missing list is unknown, not a clear screen.

Documents that contain a DOCTYPE or ENTITY declaration are rejected before parsing.
A removal between two list versions is kept as delisted evidence.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

from guardian.identities import IdentityError, NormalizedAddress, parse_address
from guardian.pricing.catalog import RiskSources

_PREFIX = "digital currency address - "
_DATE_FORMATS = ("%Y-%m-%d", "%m/%d/%Y", "%Y%m%d")


class OfacParseError(ValueError):
    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


@dataclass(frozen=True)
class DigitalIdentifier:
    entry_uid: str
    id_uid: str
    ticker: str
    family: str | None
    raw: str
    comparison_key: str | None
    display: str
    networks: tuple[str, ...]
    parsed: bool


@dataclass(frozen=True)
class SdnSnapshot:
    status: str
    reason: str
    version: str
    publish_date: str | None
    record_count: str | None
    identifiers: tuple[DigitalIdentifier, ...]
    retrieved_at: str | None

    def __post_init__(self) -> None:
        if self.status not in {"parsed", "unknown"}:
            raise ValueError("status")
        object.__setattr__(self, "identifiers", tuple(self.identifiers))


@dataclass(frozen=True)
class SanctionScreen:
    status: str
    reason: str
    stale: bool
    version: str | None
    publish_date: str | None
    entry_uid: str | None
    id_uid: str | None
    ticker: str | None
    matched_identifier: str | None
    comparison_key: str | None
    evidence_ref: str

    def __post_init__(self) -> None:
        if self.status not in {"hit", "no_hit", "unknown", "stale"}:
            raise ValueError("status")
        if self.status == "no_hit" and self.stale:
            raise ValueError("stale_list_is_not_a_clear_screen")


@dataclass(frozen=True)
class Delisting:
    status: str
    identifier: DigitalIdentifier
    present_in_version: str
    removed_in_version: str
    evidence: tuple[str, ...]

    def __post_init__(self) -> None:
        if self.status != "delisted":
            raise ValueError("status")
        if len(self.evidence) != 2 or self.evidence[0] == self.evidence[1]:
            raise ValueError("delisting_evidence")
        if self.present_in_version not in self.evidence or self.removed_in_version not in self.evidence:
            raise ValueError("delisting_evidence")
        object.__setattr__(self, "evidence", tuple(self.evidence))


@dataclass(frozen=True)
class ListDiff:
    added: tuple[DigitalIdentifier, ...]
    delisted: tuple[Delisting, ...]
    comparable: bool

    def __post_init__(self) -> None:
        object.__setattr__(self, "added", tuple(self.added))
        object.__setattr__(self, "delisted", tuple(self.delisted))


def ingest(source: bytes | bytearray | Path, catalog: RiskSources, *, retrieved_at: str | None = None) -> SdnSnapshot:
    """Parse SDN XML from bytes or a local path. This does not download the list."""
    if isinstance(source, Path):
        try:
            data = source.read_bytes()
        except OSError:
            return _unknown_snapshot("list_missing", retrieved_at)
    elif isinstance(source, (bytes, bytearray)):
        data = bytes(source)
    else:
        return _unknown_snapshot("unparseable", retrieved_at)
    try:
        text = _reject_dtd(data)
        root = ET.fromstring(text)
    except OfacParseError as exc:
        return _unknown_snapshot(exc.reason, retrieved_at)
    except ET.ParseError:
        return _unknown_snapshot("unparseable", retrieved_at)
    publish_date, record_count = _header(root)
    version = ""
    if publish_date and record_count:
        version = f"{publish_date}:{record_count}"
    elif publish_date:
        version = publish_date
    identifiers = tuple(_identifiers(root, catalog))
    return SdnSnapshot(
        status="parsed",
        reason="",
        version=version,
        publish_date=publish_date,
        record_count=record_count,
        identifiers=identifiers,
        retrieved_at=retrieved_at,
    )


def screen(
    snapshot: SdnSnapshot | None,
    *,
    network_id: str,
    address: str,
    catalog: RiskSources,
    now: date,
) -> SanctionScreen:
    """Exact match on one network. An old or missing list is not a no-hit."""
    if snapshot is None:
        return _screen("unknown", "list_missing", False, None, None, None)
    if snapshot.status != "parsed":
        return _screen("unknown", snapshot.reason or "unparseable", False, snapshot.version or None, snapshot.publish_date, None)
    freshness = _freshness(snapshot, now, catalog.ofac_max_age_days)
    if freshness == "unknown":
        return _screen("unknown", "undated", False, snapshot.version or None, snapshot.publish_date, None)
    family = catalog.family_for_network(network_id)
    if family is None:
        return _screen("unknown", "unsupported_network", freshness == "stale", snapshot.version, snapshot.publish_date, None)
    try:
        parsed = parse_address(family, address)
    except IdentityError:
        if freshness == "stale":
            return _screen("stale", "bad_address", True, snapshot.version, snapshot.publish_date, None)
        return _screen("unknown", "bad_address", False, snapshot.version, snapshot.publish_date, None)
    key = _comparison_key(family, parsed)
    hits = [
        item
        for item in snapshot.identifiers
        if item.parsed
        and item.comparison_key == key
        and network_id in item.networks
    ]
    if hits:
        hit = hits[0]
        evidence = f"ofac:{snapshot.version}:{hit.entry_uid}:{hit.comparison_key}"
        return SanctionScreen(
            status="hit",
            reason="stale" if freshness == "stale" else "exact",
            stale=freshness == "stale",
            version=snapshot.version,
            publish_date=snapshot.publish_date,
            entry_uid=hit.entry_uid,
            id_uid=hit.id_uid,
            ticker=hit.ticker,
            matched_identifier=hit.display,
            comparison_key=hit.comparison_key,
            evidence_ref=evidence,
        )
    if freshness == "stale":
        return _screen("stale", "stale_list", True, snapshot.version, snapshot.publish_date, None)
    return _screen("no_hit", "no_exact_match", False, snapshot.version, snapshot.publish_date, key)


def diff_lists(older: SdnSnapshot | None, newer: SdnSnapshot | None) -> ListDiff:
    """Added and delisted identifiers. A removal keeps both versions as evidence."""
    if older is None or newer is None or older.status != "parsed" or newer.status != "parsed":
        return ListDiff((), (), False)
    if not older.version or not newer.version or older.version == newer.version:
        return ListDiff((), (), False)
    old_map = {_diff_key(item): item for item in older.identifiers}
    new_map = {_diff_key(item): item for item in newer.identifiers}
    added = tuple(item for key, item in new_map.items() if key not in old_map)
    delisted = tuple(
        Delisting(
            status="delisted",
            identifier=item,
            present_in_version=older.version,
            removed_in_version=newer.version,
            evidence=(older.version, newer.version),
        )
        for key, item in old_map.items()
        if key not in new_map
    )
    return ListDiff(added, delisted, True)


def _identifiers(root: ET.Element, catalog: RiskSources) -> Iterable[DigitalIdentifier]:
    found: list[DigitalIdentifier] = []
    for ancestors, element in _walk(root, ()):
        if _local(element.tag) != "idType":
            continue
        ticker = _ticker(_text(element))
        if ticker is None or not ancestors:
            continue
        parent = ancestors[-1]
        raw = _child_text(parent, "idNumber")
        if not raw:
            continue
        entry = _ancestor(ancestors, "sdnEntry")
        entry_uid = _child_text(entry, "uid") if entry is not None else ""
        id_uid = _child_text(parent, "uid")
        found.append(_identifier(catalog, ticker, raw, entry_uid, id_uid))
    return found


def _identifier(catalog: RiskSources, ticker: str, raw: str, entry_uid: str, id_uid: str) -> DigitalIdentifier:
    rule = catalog.ticker(ticker)
    if rule is None:
        return DigitalIdentifier(entry_uid, id_uid, ticker, None, raw, None, raw, (), False)
    try:
        parsed = parse_address(rule.family, raw)
    except IdentityError:
        return DigitalIdentifier(entry_uid, id_uid, ticker, rule.family, raw, None, raw, rule.networks, False)
    return DigitalIdentifier(
        entry_uid,
        id_uid,
        ticker,
        rule.family,
        raw,
        _comparison_key(rule.family, parsed),
        parsed.display,
        rule.networks,
        True,
    )


def _comparison_key(family: str, parsed: NormalizedAddress) -> str:
    if family == "evm":
        return parsed.identity_key
    return parsed.canonical


def _diff_key(item: DigitalIdentifier) -> tuple[str, str, str]:
    if item.comparison_key:
        return (item.ticker, item.family or "", item.comparison_key)
    return (item.ticker, "raw", item.raw)


def _freshness(snapshot: SdnSnapshot, now: date, max_age_days: int) -> str:
    if snapshot.publish_date is None:
        return "unknown"
    try:
        published = date.fromisoformat(snapshot.publish_date)
    except ValueError:
        return "unknown"
    age = (now - published).days
    if age < 0:
        return "unknown"
    if age > max_age_days:
        return "stale"
    return "current"


def _header(root: ET.Element) -> tuple[str | None, str | None]:
    info = None
    for _ancestors, element in _walk(root, ()):
        if _local(element.tag) in {"publshInformation", "publishInformation"}:
            info = element
            break
    container = info if info is not None else root
    raw_date = _descendant_text(container, {"Publish_Date", "PublishDate"})
    record_count = _descendant_text(container, {"Record_Count", "RecordCount"}) or None
    return _parse_date(raw_date), record_count


def _descendant_text(element: ET.Element, names: set[str]) -> str:
    for node in element.iter():
        if _local(node.tag) in names:
            text = _text(node)
            if text:
                return text
    return ""


def _parse_date(text: str) -> str | None:
    if not text:
        return None
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt).date().isoformat()
        except ValueError:
            continue
    return None


def _ticker(id_type: str) -> str | None:
    folded = " ".join(id_type.split())
    if not folded.lower().startswith(_PREFIX):
        return None
    ticker = folded[len(_PREFIX):].strip().upper()
    if not ticker or any(char.isspace() for char in ticker):
        return None
    return ticker


def _reject_dtd(data: bytes) -> str:
    if data.startswith(b"\xff\xfe") or data.startswith(b"\xfe\xff"):
        raise OfacParseError("unparseable")
    try:
        text = data.decode("utf-8-sig")
    except UnicodeError as exc:
        raise OfacParseError("unparseable") from exc
    lowered = text.lower()
    if "<!doctype" in lowered or "<!entity" in lowered:
        raise OfacParseError("dtd_or_entity_rejected")
    return text


def _walk(element: ET.Element, ancestors: tuple[ET.Element, ...]):
    yield ancestors, element
    for child in list(element):
        yield from _walk(child, ancestors + (element,))


def _ancestor(ancestors: tuple[ET.Element, ...], name: str) -> ET.Element | None:
    for element in reversed(ancestors):
        if _local(element.tag) == name:
            return element
    return None


def _child_text(element: ET.Element | None, name: str) -> str:
    if element is None:
        return ""
    for child in list(element):
        if _local(child.tag) == name:
            return _text(child)
    return ""


def _text(element: ET.Element) -> str:
    if element.text is None:
        return ""
    return element.text.strip()


def _local(tag: str) -> str:
    if "}" in tag:
        return tag.rsplit("}", 1)[-1]
    return tag


def _unknown_snapshot(reason: str, retrieved_at: str | None) -> SdnSnapshot:
    return SdnSnapshot("unknown", reason, "", None, None, (), retrieved_at)


def _screen(
    status: str,
    reason: str,
    stale: bool,
    version: str | None,
    publish_date: str | None,
    comparison_key: str | None,
) -> SanctionScreen:
    return SanctionScreen(
        status=status,
        reason=reason,
        stale=stale,
        version=version,
        publish_date=publish_date,
        entry_uid=None,
        id_uid=None,
        ticker=None,
        matched_identifier=None,
        comparison_key=comparison_key,
        evidence_ref="",
    )
