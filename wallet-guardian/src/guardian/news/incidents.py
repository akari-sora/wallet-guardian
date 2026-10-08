"""One incident per canonical URL, title window, or correction.

A repost within 48 hours joins the existing incident. A correction updates that
incident and does not create a second alert.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Sequence

from guardian.news.feeds import NewsItem, canonicalize_url

_WINDOW = timedelta(hours=48)


@dataclass(frozen=True)
class SourceRef:
    source_id: str
    url: str
    published_at: datetime | None


@dataclass
class Incident:
    incident_id: str
    protocols: tuple[str, ...]
    title: str
    normalized_title: str
    kind: str
    urls: list[str]
    source_refs: list[SourceRef]
    published_at: datetime | None
    corrected: bool
    alert_id: str


@dataclass
class IncidentBook:
    incidents: list[Incident] = field(default_factory=list)
    alert_ids: list[str] = field(default_factory=list)

    def add(self, item: NewsItem) -> Incident:
        match = self._find(item)
        if match is None:
            created = _create(item)
            self.incidents.append(created)
            self.alert_ids.append(created.alert_id)
            return created
        _merge(match, item)
        return match

    def add_all(self, items: Sequence[NewsItem]) -> None:
        for item in items:
            self.add(item)

    def _find(self, item: NewsItem) -> Incident | None:
        correction = item.correction_of
        if correction:
            for incident in self.incidents:
                if correction in incident.urls:
                    return incident
        for incident in self.incidents:
            if item.url in incident.urls:
                return incident
        if item.published_at is None or not item.protocols:
            return None
        for incident in self.incidents:
            if incident.published_at is None:
                continue
            if item.normalized_title != incident.normalized_title:
                continue
            if not (set(item.protocols) & set(incident.protocols)):
                continue
            if abs(item.published_at - incident.published_at) <= _WINDOW:
                return incident
        return None


def _create(item: NewsItem) -> Incident:
    material = f"{item.url}\n{item.normalized_title}".encode("utf-8")
    identity = hashlib.sha256(material).hexdigest()[:32]
    return Incident(
        incident_id=identity,
        protocols=item.protocols,
        title=item.title,
        normalized_title=item.normalized_title,
        kind=item.kind,
        urls=[item.url],
        source_refs=[SourceRef(item.source_id, item.url, item.published_at)],
        published_at=item.published_at,
        corrected=item.is_correction,
        alert_id=identity,
    )


def _merge(incident: Incident, item: NewsItem) -> None:
    if item.url not in incident.urls:
        incident.urls.append(item.url)
    incident.source_refs.append(SourceRef(item.source_id, item.url, item.published_at))
    incident.protocols = tuple(sorted(set(incident.protocols) | set(item.protocols)))
    if item.is_correction:
        incident.corrected = True
        incident.title = item.title
    if incident.published_at is None:
        incident.published_at = item.published_at
    elif item.published_at is not None and item.published_at < incident.published_at:
        incident.published_at = item.published_at


def canonical_correction(url: str | None) -> str | None:
    if url is None:
        return None
    return canonicalize_url(url)
