"""Separate label kinds. Higher kinds do not erase lower ones, and nothing here is 'safe'."""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Sequence

from guardian.identities import IdentityError, NormalizedAddress, parse_address
from guardian.risk.goplus import FunctionRead
from guardian.risk.ofac import SanctionScreen

KIND_RANK = {
    "official_sanction": 0,
    "official_exact": 1,
    "community_report": 2,
    "structural": 3,
    "heuristic": 4,
}
_BUCKETS = ("checked", "unknown", "unsupported", "blocked", "stale")


@dataclass(frozen=True)
class RiskLabel:
    kind: str
    source: str
    function: str
    list_version: str | None
    list_date: str | None
    retrieved_at: str | None
    evidence_ref: str
    matched_identifier: str
    stale: bool = False
    detail: str = ""

    def __post_init__(self) -> None:
        if self.kind not in KIND_RANK:
            raise ValueError("kind")
        if not self.source or not self.function or not self.evidence_ref or not self.matched_identifier:
            raise ValueError("label_evidence")


@dataclass(frozen=True)
class RiskSummary:
    status: str
    highest_kind: str | None
    labels: tuple[RiskLabel, ...]
    sources_checked: tuple[str, ...]
    sources_unknown: tuple[str, ...]
    sources_unsupported: tuple[str, ...]
    sources_blocked: tuple[str, ...]
    sources_stale: tuple[str, ...]

    def __post_init__(self) -> None:
        if self.status in {"clean", "safe"} or self.status not in {"labelled", "no_hit_in_checked_sources"}:
            raise ValueError("status")
        object.__setattr__(self, "labels", tuple(self.labels))
        for name in _BUCKETS:
            object.__setattr__(self, f"sources_{name}", tuple(getattr(self, f"sources_{name}")))
        if self.labels:
            if self.status != "labelled" or self.highest_kind != self.labels[0].kind:
                raise ValueError("precedence")
        elif self.status != "no_hit_in_checked_sources" or self.highest_kind is not None:
            raise ValueError("no_hit")


@dataclass(frozen=True)
class PoisoningAssessment:
    """Dust from a lookalike sender. The recipient and the real counterparty stay unlabelled."""

    sender: str
    recipient: str
    sender_labels: tuple[RiskLabel, ...]
    recipient_labels: tuple[RiskLabel, ...]
    counterparty_labels: tuple[RiskLabel, ...]
    usd_threshold_decision: str
    display_price_triggers_alert: bool

    def __post_init__(self) -> None:
        object.__setattr__(self, "sender_labels", tuple(self.sender_labels))
        object.__setattr__(self, "recipient_labels", tuple(self.recipient_labels))
        object.__setattr__(self, "counterparty_labels", tuple(self.counterparty_labels))
        if self.recipient_labels or self.counterparty_labels:
            raise ValueError("poisoning_must_not_label_recipient")
        if self.display_price_triggers_alert:
            raise ValueError("dust_price_cannot_alert")
        if self.sender_labels and self.usd_threshold_decision != "valuation_unknown":
            raise ValueError("dust_price_cannot_alert")


def summarize(
    labels: Sequence[RiskLabel],
    *,
    checked: Sequence[str] = (),
    unknown: Sequence[str] = (),
    unsupported: Sequence[str] = (),
    blocked: Sequence[str] = (),
    stale: Sequence[str] = (),
) -> RiskSummary:
    """Keep every label. The status names the highest kind, or the checked-source miss."""
    ordered = tuple(sorted(labels, key=lambda label: KIND_RANK[label.kind]))
    buckets = {
        "checked": _unique(checked),
        "unknown": _unique(unknown),
        "unsupported": _unique(unsupported),
        "blocked": _unique(blocked),
        "stale": _unique(stale),
    }
    if ordered:
        return RiskSummary(
            status="labelled",
            highest_kind=ordered[0].kind,
            labels=ordered,
            sources_checked=buckets["checked"],
            sources_unknown=buckets["unknown"],
            sources_unsupported=buckets["unsupported"],
            sources_blocked=buckets["blocked"],
            sources_stale=buckets["stale"],
        )
    return RiskSummary(
        status="no_hit_in_checked_sources",
        highest_kind=None,
        labels=(),
        sources_checked=buckets["checked"],
        sources_unknown=buckets["unknown"],
        sources_unsupported=buckets["unsupported"],
        sources_blocked=buckets["blocked"],
        sources_stale=buckets["stale"],
    )


def note_source_failure(summary: RiskSummary, *, source: str, reason: str) -> RiskSummary:
    """A later failure keeps the earlier label and marks it stale. ``reason`` is not a clearance."""
    del reason
    labels = tuple(replace(label, stale=True) if label.source == source else label for label in summary.labels)
    return summarize(
        labels,
        checked=tuple(name for name in summary.sources_checked if name != source),
        unknown=tuple(name for name in summary.sources_unknown if name != source),
        unsupported=tuple(name for name in summary.sources_unsupported if name != source),
        blocked=tuple(name for name in summary.sources_blocked if name != source),
        stale=tuple(dict.fromkeys((*summary.sources_stale, source))),
    )


def label_from_sanction(screen: SanctionScreen, *, retrieved_at: str | None) -> RiskLabel | None:
    if screen.status != "hit" or not screen.matched_identifier or not screen.evidence_ref:
        return None
    return RiskLabel(
        kind="official_sanction",
        source="ofac",
        function="sdn_xml",
        list_version=screen.version,
        list_date=screen.publish_date,
        retrieved_at=retrieved_at,
        evidence_ref=screen.evidence_ref,
        matched_identifier=screen.matched_identifier,
        stale=screen.stale,
        detail=screen.ticker or "",
    )


def labels_from_goplus(read: FunctionRead, *, retrieved_at: str | None) -> tuple[RiskLabel, ...]:
    """String flag ``1`` is structural. Flag ``0`` creates no label and is not safe."""
    if read.status != "flagged":
        return ()
    labels: list[RiskLabel] = []
    for name, state in read.flags:
        if state != "flagged":
            continue
        labels.append(
            RiskLabel(
                kind="structural",
                source="goplus",
                function=read.function,
                list_version=None,
                list_date=None,
                retrieved_at=retrieved_at,
                evidence_ref=f"goplus:{read.function}:{read.network_id}:{read.target}:{name}",
                matched_identifier=read.target,
                detail=name,
            )
        )
    return tuple(labels)


def official_exact_label(
    *,
    source: str,
    function: str,
    matched_identifier: str,
    evidence_ref: str,
    retrieved_at: str | None,
    list_version: str | None = None,
    list_date: str | None = None,
    detail: str = "",
) -> RiskLabel:
    """An exact hit on a documented official list other than the OFAC SDN parse."""
    return RiskLabel(
        kind="official_exact",
        source=source,
        function=function,
        list_version=list_version,
        list_date=list_date,
        retrieved_at=retrieved_at,
        evidence_ref=evidence_ref,
        matched_identifier=matched_identifier,
        detail=detail,
    )


def community_report_label(
    *,
    matched_identifier: str,
    evidence_ref: str,
    retrieved_at: str | None,
    source: str = "chainabuse",
    function: str = "reports",
    list_version: str | None = None,
    list_date: str | None = None,
    detail: str = "",
) -> RiskLabel:
    """A supplied report. This does not mean a Chainabuse lookup returned no reports."""
    return RiskLabel(
        kind="community_report",
        source=source,
        function=function,
        list_version=list_version,
        list_date=list_date,
        retrieved_at=retrieved_at,
        evidence_ref=evidence_ref,
        matched_identifier=matched_identifier,
        detail=detail,
    )


def sanction_bucket(screen: SanctionScreen) -> str:
    if screen.status == "unknown":
        return "unknown"
    if screen.stale or screen.status == "stale":
        return "stale"
    return "checked"


def goplus_bucket(read: FunctionRead) -> str:
    if read.status == "unsupported":
        return "unsupported"
    if read.status == "unknown":
        return "unknown"
    return "checked"


def assess_poisoning(
    *,
    sender: str,
    recipient: str,
    known_counterparties: Sequence[str],
    raw_amount: int | None,
    dust: bool = False,
    display_price: object | None = None,
    retrieved_at: str | None = None,
) -> PoisoningAssessment:
    """Label only the lookalike sender of a dust or zero-value transfer.

    ``display_price`` is ignored. It cannot trigger a USD alert.
    """
    del display_price
    try:
        sender_parsed = parse_address("evm", sender)
        recipient_parsed = parse_address("evm", recipient)
    except IdentityError:
        return _poisoning(sender, recipient, (), "not_applicable")
    if sender_parsed.identity_key == recipient_parsed.identity_key:
        return _poisoning(sender_parsed.display, recipient_parsed.display, (), "not_applicable")
    if not _is_dust(raw_amount, dust):
        return _poisoning(sender_parsed.display, recipient_parsed.display, (), "not_applicable")
    known = _matching_counterparty(sender_parsed, known_counterparties)
    if known is None:
        return _poisoning(sender_parsed.display, recipient_parsed.display, (), "not_applicable")
    label = RiskLabel(
        kind="heuristic",
        source="heuristic",
        function="address_poisoning",
        list_version=None,
        list_date=None,
        retrieved_at=retrieved_at,
        evidence_ref=f"poisoning:{sender_parsed.identity_key}:looks_like:{known.identity_key}",
        matched_identifier=sender_parsed.display,
        detail="poisoning_suspect",
    )
    return _poisoning(sender_parsed.display, recipient_parsed.display, (label,), "valuation_unknown")


def _poisoning(
    sender: str,
    recipient: str,
    labels: tuple[RiskLabel, ...],
    decision: str,
) -> PoisoningAssessment:
    return PoisoningAssessment(
        sender=sender,
        recipient=recipient,
        sender_labels=labels,
        recipient_labels=(),
        counterparty_labels=(),
        usd_threshold_decision=decision,
        display_price_triggers_alert=False,
    )


def _matching_counterparty(sender: NormalizedAddress, known_counterparties: Sequence[str]) -> NormalizedAddress | None:
    for candidate in known_counterparties:
        try:
            known = parse_address("evm", candidate)
        except IdentityError:
            continue
        if _lookalike(sender.identity_key, known.identity_key):
            return known
    return None


def _lookalike(left: str, right: str) -> bool:
    if not left.startswith("0x") or not right.startswith("0x"):
        return False
    a = left[2:]
    b = right[2:]
    if len(a) != 40 or len(b) != 40 or a == b:
        return False
    return a[:4] == b[:4] and a[-4:] == b[-4:]


def _is_dust(raw_amount: int | None, dust: bool) -> bool:
    if dust:
        return True
    if isinstance(raw_amount, bool) or not isinstance(raw_amount, int):
        return False
    return raw_amount == 0


def _unique(values: Sequence[str]) -> tuple[str, ...]:
    found: list[str] = []
    for value in values:
        if value not in found:
            found.append(value)
    return tuple(found)
