"""Bounded parsers for fixture documents. This module does not fetch or follow links.

DefiLlama numbers stay in the unit the field name states. APR is not turned into
APY. Binance catalog titles are not terms: catalog 93 is undocumented here, and
article detail is not retrieved. A failed parse is a reason, not an empty success
and not a zero yield. A gzip member must be complete, and bytes after that member
are rejected. Depth and decoder failures return a reason. A negated or conflicting
clause is not an affirmative term. Negation before or after a matched expression
blocks that field. A deadline role counts only in the same clause as its
timestamp, a publication time is not a deadline, and a disputed deadline clears
the affirmative date. Free text stays residual unknown unless every clause matches
the supported grammar. A date clause counts only when the timestamp is a real
calendar time.
"""

from __future__ import annotations

import json
import re
import zlib
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from guardian.chains.evm.accounting import normalize_address
from guardian.yields.models import (
    campaign_id_ok,
    canonical,
    record_id_ok,
    synthetic_address,
    yield_decimal,
)

DECOMPRESSED_CAP = 16 * 1024 * 1024
MAX_DEPTH = 32
MAX_RECORDS = 20_000
MAX_KEYS = 128
MAX_STRING = 8_192
_ISO = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})")
_ROLE = re.compile(
    r"\b(until|through|before|deadline|ends|end|from|starts|start|beginning)\b",
    re.IGNORECASE,
)
_END_WORDS = frozenset({"until", "through", "before", "deadline", "ends", "end"})
_START_WORDS = frozenset({"from", "starts", "start", "beginning"})
_AMBIGUOUS = re.compile(
    r"\b(?:next\s+friday|next\s+week|q[1-4]|until\s+further\s+notice|soon|limited\s+time|this\s+month)\b",
    re.IGNORECASE,
)
_MINIMUM = re.compile(r"\bminimum\s+([0-9]+(?:\.[0-9]+)?)\s+([A-Za-z]{2,12})\b", re.IGNORECASE)
_LOCK = re.compile(r"\blocked\s+for\s+([0-9]+)\s+days\b", re.IGNORECASE)
_NEGATION = re.compile(r"\b(?:not|no|never|without|cannot|can\s*not)\b|n['’]t\b", re.IGNORECASE)
_CLAUSE_BOUNDARY = re.compile(r"[!;?\n]|\.(?=\s|$)")
# Residual "none" means every clause matched one of these forms. Other eligibility
# text is unknown; it is not filtered by a keyword list. A deadline form counts
# only after its timestamp parses as a real calendar time.
_MINIMUM_CLAUSE = re.compile(
    r"(?i)^(?:a\s+)?minimum\s+[0-9]+(?:\.[0-9]+)?\s+[A-Za-z]{2,12}(?:\s+locked\s+for\s+[0-9]+\s+days)?$"
)
_LOCK_CLAUSE = re.compile(r"(?i)^(?:funds\s+are\s+)?locked\s+for\s+[0-9]+\s+days$")
_DEADLINE_CLAUSE = re.compile(
    r"(?i)^(?:eligible\s+)?(?:until|through|before|deadline|ends|end|from|starts|start|beginning)"
    r"\s+\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$"
)
_LINK_CLAUSE = re.compile(r"(?i)^(?:see\s+)?https://\S+$")
_LINK = re.compile(r"https://[^\s\"'<>]+")
_DATE_TIME = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$")
_PUBLICATION = re.compile(r"\b(?:published|publication)\b", re.IGNORECASE)


class _NonFinite(Exception):
    def __init__(self, token: str) -> None:
        self.token = token


@dataclass(frozen=True)
class ParsedRecord:
    source: str
    record_id: str
    published_at: str | None
    unit: str
    base: Decimal | None
    reward: Decimal | None
    total: Decimal | None
    reward_tokens: tuple[str, ...]
    reward_token_status: str
    reward_token_count_known: bool
    conditions: dict[str, Any]
    deadlines: tuple[dict[str, str], ...]
    deadline_status: str
    excerpt: str
    extraction_status: str
    provenance: tuple[dict[str, str], ...]
    residual_conditions: str
    article_detail: str
    catalog_status: str
    links_not_followed: int
    described_as_temporary: bool
    duration_stated: bool
    temporary_language: str
    campaign_id: str | None
    claimed_contracts: tuple[str, ...]
    claim_status: str
    read_coverage: str


@dataclass(frozen=True)
class ParseResult:
    ok: bool
    reason: str
    records: tuple[ParsedRecord, ...]


def parse_source(source: str, data: bytes) -> ParseResult:
    if source == "defillama_pools":
        return parse_defillama(data)
    if source == "binance_catalog_93":
        return parse_binance_catalog(data)
    if source == "explicit_body":
        return parse_explicit(data)
    return ParseResult(False, "unsupported_source", ())


def parse_defillama(data: bytes) -> ParseResult:
    document, reason = bounded_json(data)
    if document is None:
        return ParseResult(False, reason, ())
    if not isinstance(document, dict) or not isinstance(document.get("data"), list):
        return ParseResult(False, "unparseable", ())
    records: list[ParsedRecord] = []
    for row in document["data"]:
        if not isinstance(row, dict) or not record_id_ok(row.get("pool")):
            return ParseResult(False, "malformed_record", ())
        unit = _unit(row, "APY")
        tokens = _tokens(row.get("rewardTokens"))
        records.append(_numbers(
            source="defillama_pools",
            record_id=row["pool"],
            published_at=None,
            unit=unit,
            base=yield_decimal(row.get("apyBase")),
            reward=yield_decimal(row.get("apyReward")),
            total=yield_decimal(row.get("apy")),
            tokens=tokens,
            terms=_empty_terms(extraction="not_applicable", article="not_applicable", catalog="not_applicable"),
            campaign_id=None,
            claimed=(),
            claim_status="ok",
            read_coverage="partial",
        ))
    return ParseResult(True, "", tuple(records))


def parse_binance_catalog(data: bytes) -> ParseResult:
    """Titles and publication dates only. Deadlines are not inferred from titles."""
    document, reason = bounded_json(data)
    if document is None:
        return ParseResult(False, reason, ())
    articles = _articles(document)
    if articles is None:
        return ParseResult(False, "unparseable", ())
    records: list[ParsedRecord] = []
    for row in articles:
        if not isinstance(row, dict):
            return ParseResult(False, "malformed_record", ())
        record_id = _article_id(row.get("id"))
        title = row.get("title")
        if record_id is None or not isinstance(title, str) or not title.strip():
            return ParseResult(False, "malformed_record", ())
        excerpt = _quote(_clip(title.strip()))
        links = _link_count(title) + (1 if isinstance(row.get("url"), str) and row["url"].startswith("https://") else 0)
        terms = _empty_terms(
            extraction="title_only",
            article="not_observable",
            catalog="undocumented",
            excerpt=excerpt,
            residual="unknown",
            links=links,
            provenance=({"field": "title", "quote": excerpt, "source": "binance_catalog_93"},),
        )
        records.append(_numbers(
            source="binance_catalog_93",
            record_id=record_id,
            published_at=_publication(row.get("releaseDate")),
            unit="unknown",
            base=None,
            reward=None,
            total=None,
            tokens=("unknown", False, ()),
            terms=terms,
            campaign_id=None,
            claimed=(),
            claim_status="ok",
            read_coverage="not_observable",
        ))
    return ParseResult(True, "", tuple(records))


def parse_explicit(data: bytes) -> ParseResult:
    """Caller-supplied bodies. Titles are ignored. Links in the body are not followed."""
    document, reason = bounded_json(data)
    if document is None:
        return ParseResult(False, reason, ())
    rows = document.get("records") if isinstance(document, dict) else None
    if not isinstance(rows, list):
        return ParseResult(False, "unparseable", ())
    records: list[ParsedRecord] = []
    for row in rows:
        if not isinstance(row, dict) or not record_id_ok(row.get("record_id")):
            return ParseResult(False, "malformed_record", ())
        body = row.get("body")
        if not isinstance(body, str):
            return ParseResult(False, "malformed_record", ())
        campaign = row.get("campaign_id")
        campaign_id = campaign if campaign_id_ok(campaign) else None
        claim_status, claimed = _claimed(row.get("contracts"))
        if campaign is not None and campaign_id is None:
            claim_status = "rejected"
        terms = _terms_from_body(body)
        unit = _unit(row, "unknown")
        records.append(_numbers(
            source="explicit_body",
            record_id=row["record_id"],
            published_at=_publication(row.get("published_at")),
            unit=unit,
            base=yield_decimal(row.get("apyBase")),
            reward=yield_decimal(row.get("apyReward")),
            total=yield_decimal(row.get("apy")),
            tokens=_tokens(row.get("rewardTokens")),
            terms=terms,
            campaign_id=campaign_id,
            claimed=claimed,
            claim_status=claim_status,
            read_coverage="partial" if terms["extraction_status"] == "extracted" else "unknown",
        ))
    return ParseResult(True, "", tuple(records))


def bounded_json(data: bytes) -> tuple[Any | None, str]:
    """Decode JSON with a 16 MiB decompressed cap. NaN and truncated documents fail closed."""
    if not isinstance(data, (bytes, bytearray)):
        return None, "unparseable"
    raw = bytes(data)
    if raw.startswith(b"\xff\xfe") or raw.startswith(b"\xfe\xff"):
        return None, "unparseable"
    if raw.startswith(b"\x1f\x8b"):
        decoded, reason = _gunzip(raw, DECOMPRESSED_CAP)
        if decoded is None:
            return None, "decompressed_cap" if reason == "too_large" else reason
        raw = decoded
    elif len(raw) > DECOMPRESSED_CAP:
        return None, "decompressed_cap"
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeError:
        return None, "unparseable"
    depth_limit = _raw_nesting(text)
    if depth_limit:
        return None, depth_limit
    try:
        value = json.loads(text, parse_float=Decimal, parse_int=Decimal, parse_constant=_reject_constant)
    except _NonFinite as exc:
        return None, "nan" if exc.token == "NaN" else "non_finite"
    except RecursionError:
        return None, "nesting_limit"
    except json.JSONDecodeError:
        return None, "truncated" if text.lstrip()[:1] in "{[" else "unparseable"
    limit = _limits(value)
    if limit:
        return None, limit
    return value, ""


def _reject_constant(token: str) -> None:
    raise _NonFinite(token)


def _gunzip(raw: bytes, cap: int) -> tuple[bytes | None, str]:
    """Inflate one complete gzip member. A missing trailer or a second member fails."""
    if len(raw) > cap:
        return None, "too_large"
    decoder = zlib.decompressobj(16 + zlib.MAX_WBITS)
    chunks: list[bytes] = []
    total = 0
    try:
        pending = raw
        while pending:
            piece = decoder.decompress(pending, cap + 1 - total)
            pending = decoder.unconsumed_tail
            if piece:
                total += len(piece)
                if total > cap:
                    return None, "too_large"
                chunks.append(piece)
                continue
            if pending:
                return None, "unparseable"
            break
        tail = decoder.flush()
    except zlib.error:
        if chunks and not decoder.eof:
            return None, "truncated"
        return None, "unparseable"
    if tail:
        total += len(tail)
        if total > cap:
            return None, "too_large"
        chunks.append(tail)
    if total > cap:
        return None, "too_large"
    if not decoder.eof:
        return None, "truncated"
    if decoder.unused_data:
        return None, "trailing_data"
    return b"".join(chunks), ""


def _raw_nesting(text: str) -> str:
    """Reject deep documents before the recursive JSON decoder sees them."""
    depth = 0
    in_string = False
    escape = False
    for char in text:
        if in_string:
            if escape:
                escape = False
            elif char == "\\":
                escape = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char in "{[":
            depth += 1
            if depth > MAX_DEPTH:
                return "nesting_limit"
        elif char in "}]":
            if depth:
                depth -= 1
    return ""


def _limits(value: Any) -> str:
    stack: list[tuple[Any, int]] = [(value, 1)]
    while stack:
        current, depth = stack.pop()
        if depth > MAX_DEPTH:
            return "nesting_limit"
        if isinstance(current, dict):
            if len(current) > MAX_KEYS:
                return "record_limit"
            for item in current.values():
                stack.append((item, depth + 1))
        elif isinstance(current, list):
            if len(current) > MAX_RECORDS:
                return "record_limit"
            for item in current:
                stack.append((item, depth + 1))
        elif isinstance(current, str) and len(current) > MAX_STRING:
            return "string_limit"
        elif isinstance(current, Decimal) and not current.is_finite():
            return "nan"
    return ""


def _articles(document: Any) -> list[Any] | None:
    if not isinstance(document, dict):
        return None
    if isinstance(document.get("articles"), list):
        return list(document["articles"])
    data = document.get("data")
    if not isinstance(data, dict) or not isinstance(data.get("catalogs"), list):
        return None
    found: list[Any] = []
    for catalog in data["catalogs"]:
        if not isinstance(catalog, dict) or not isinstance(catalog.get("articles"), list):
            return None
        found.extend(catalog["articles"])
    return found


def _article_id(value: Any) -> str | None:
    if isinstance(value, str) and record_id_ok(value):
        return value
    if isinstance(value, Decimal) and value == value.to_integral_value():
        text = str(int(value))
        if record_id_ok(text):
            return text
    return None


def _unit(row: dict[str, Any], default: str) -> str:
    raw = row.get("unit", row.get("apyUnit"))
    if raw is None:
        return default
    if isinstance(raw, str) and raw.strip().upper() in {"APY", "APR"}:
        return raw.strip().upper()
    return "unknown"


def _tokens(value: Any) -> tuple[str, bool, tuple[str, ...]]:
    if value is None:
        return "unknown", False, ()
    if value == []:
        return "none_listed", True, ()
    if not isinstance(value, list):
        return "unknown", False, ()
    found: list[str] = []
    for item in value:
        if not isinstance(item, str):
            return "unknown", False, ()
        address = normalize_address(item)
        if address is None or not synthetic_address(address):
            return "unknown", False, ()
        found.append(address)
    return "identified", True, tuple(found)


def _claimed(value: Any) -> tuple[str, tuple[str, ...]]:
    if value is None:
        return "ok", ()
    if not isinstance(value, list):
        return "rejected", ()
    found: list[str] = []
    for item in value:
        if not isinstance(item, str):
            return "rejected", ()
        address = normalize_address(item)
        if address is None or not synthetic_address(address):
            return "rejected", ()
        found.append(address)
    return "ok", tuple(found)


def _publication(value: Any) -> str | None:
    if isinstance(value, str) and _DATE_TIME.fullmatch(value.strip()) is not None:
        text = value.strip()
        try:
            _as_utc(text)
        except ValueError:
            return None
        return text
    if isinstance(value, Decimal) and value == value.to_integral_value():
        return _epoch(int(value))
    return None


def _epoch(number: int) -> str | None:
    seconds = number / 1000 if abs(number) > 10**12 else number
    if seconds < 0 or seconds > 10**11:
        return None
    try:
        moment = datetime.fromtimestamp(seconds, timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def _terms_from_body(body: str) -> dict[str, Any]:
    """Extract affirmative terms only. Negation, conflict, and leftover text stay unknown."""
    deadlines, deadline_dispute = _deadlines(body)
    minimum, minimum_dispute = _accepted(_MINIMUM.finditer(body), body, _minimum_key)
    lock, lock_dispute = _accepted(_LOCK.finditer(body), body, lambda match: match.group(1))
    disputed = deadline_dispute or minimum_dispute or lock_dispute
    ambiguous = _AMBIGUOUS.search(body) is not None
    end = next((item for item in deadlines if item["role"] == "end"), None)
    extracted = end is not None or minimum is not None or lock is not None
    covered = _complete_coverage(body)
    if extracted:
        extraction = "extracted"
        condition_status = "extracted"
        residual = "none" if covered and not ambiguous and not disputed else "unknown"
    elif ambiguous:
        extraction = "ambiguous"
        condition_status = "ambiguous"
        residual = "unknown"
    else:
        extraction = "unknown"
        condition_status = "unknown"
        residual = "unknown"
    duration = end is not None or lock is not None
    excerpt_source = end["quote"] if end is not None else _clip(body.strip())
    excerpt = _quote(excerpt_source)
    provenance: list[dict[str, str]] = []
    if minimum is not None:
        provenance.append(_field_quote("minimum_amount", minimum.group(0)))
    if lock is not None:
        provenance.append(_field_quote("lock_days", lock.group(0)))
    for item in deadlines:
        field = "deadline" if item["role"] == "end" else "start"
        provenance.append(_field_quote(field, item["quote"]))
    return {
        "conditions": {
            "status": condition_status,
            "minimum_amount": canonical(Decimal(minimum.group(1))) if minimum else None,
            "minimum_unit": minimum.group(2).upper() if minimum else None,
            "lock_days": int(lock.group(1)) if lock else None,
        },
        "deadlines": tuple(deadlines),
        "deadline_status": "extracted" if end is not None else "unknown",
        "excerpt": excerpt,
        "extraction_status": extraction,
        "provenance": tuple(item for item in provenance if item["quote"]),
        "residual_conditions": residual,
        "article_detail": "not_applicable",
        "catalog_status": "not_applicable",
        "links_not_followed": _link_count(body),
        "duration_stated": duration,
        "described_as_temporary": duration and extracted,
        "temporary_language": "publisher_stated_duration" if duration and extracted else "not_described_as_temporary",
    }


def _deadlines(body: str) -> tuple[list[dict[str, str]], bool]:
    """Accept one supported deadline expression per role, inside its own clause.

    The role word has to sit in the same clause as the timestamp. Text from an
    earlier sentence, including the word "deadline" before a publication time,
    does not label that timestamp. Negation before or after the timestamp disputes
    that role, and any affirmative date for a disputed role is dropped.
    """
    found: list[dict[str, str]] = []
    disputed_roles: set[str] = set()
    for match in _ISO.finditer(body):
        token = match.group(0)
        try:
            utc = _as_utc(token)
        except ValueError:
            continue
        left, right = _clause_bounds(body, match.start(), match.end())
        before = body[left:match.start()]
        after = body[match.end():right]
        preceding = _last_role(before)
        following = _roles_named(after)
        if _negated(body, match.start(), match.end()) and (preceding is not None or following):
            if preceding is not None:
                named = _deadline_role(preceding.group(1))
                if named is not None:
                    disputed_roles.add(named)
            disputed_roles.update(following)
            continue
        if _PUBLICATION.search(body[left:right]) is not None:
            continue
        if preceding is None:
            continue
        role = _deadline_role(preceding.group(1))
        if role is None:
            continue
        expression = body[left + preceding.start():match.end()].strip()
        if not _supported_deadline(expression):
            continue
        found.append({
            "role": role,
            "original": token,
            "timezone": "Z" if token.endswith("Z") else token[-6:],
            "utc": utc,
            "quote": _clip(expression),
        })
    for role in ("end", "start"):
        chosen = [item for item in found if item["role"] == role]
        if len({item["original"] for item in chosen}) > 1 or (role in disputed_roles and chosen):
            found = [item for item in found if item["role"] != role]
            disputed_roles.add(role)
    return found, bool(disputed_roles)


def _accepted(
    matches: Any,
    body: str,
    key,
) -> tuple[re.Match[str] | None, bool]:
    affirmative: list[re.Match[str]] = []
    disputed = False
    for match in matches:
        if _negated(body, match.start(), match.end()):
            disputed = True
            continue
        if not _supported_clause(_clause_text(body, match.start(), match.end())):
            continue
        affirmative.append(match)
    values = {key(match) for match in affirmative}
    if len(values) > 1 or (disputed and affirmative):
        return None, True
    if affirmative:
        return affirmative[0], False
    return None, disputed


def _minimum_key(match: re.Match[str]) -> tuple[str, str]:
    return match.group(1), match.group(2).upper()


def _complete_coverage(body: str) -> bool:
    """True when every clause is a supported term. Leftover free text is not covered."""
    clauses = [part.strip() for part in _CLAUSE_BOUNDARY.split(body) if part.strip()]
    if not clauses:
        return False
    return all(_supported_clause(clause) for clause in clauses)


def _supported_clause(clause: str) -> bool:
    if any(pattern.fullmatch(clause) is not None for pattern in (_MINIMUM_CLAUSE, _LOCK_CLAUSE, _LINK_CLAUSE)):
        return True
    return _supported_deadline(clause)


def _supported_deadline(text: str) -> bool:
    """True when the text is a deadline clause whose timestamp is a real calendar time."""
    if _DEADLINE_CLAUSE.fullmatch(text) is None:
        return False
    found = _ISO.search(text)
    if found is None:
        return False
    try:
        _as_utc(found.group(0))
    except ValueError:
        return False
    return True


def _last_role(text: str) -> re.Match[str] | None:
    found: re.Match[str] | None = None
    for candidate in _ROLE.finditer(text):
        found = candidate
    return found


def _roles_named(text: str) -> list[str]:
    named: list[str] = []
    for candidate in _ROLE.finditer(text):
        role = _deadline_role(candidate.group(1))
        if role is not None and role not in named:
            named.append(role)
    return named


def _deadline_role(word: str) -> str | None:
    lowered = word.lower()
    if lowered in _END_WORDS:
        return "end"
    if lowered in _START_WORDS:
        return "start"
    return None


def _negated(text: str, start: int, end: int) -> bool:
    """True when the same clause negates the match before or after the expression."""
    left, right = _clause_bounds(text, start, end)
    before = text[left:start]
    after = text[end:right]
    return _NEGATION.search(before) is not None or _NEGATION.search(after) is not None


def _clause_bounds(text: str, start: int, end: int) -> tuple[int, int]:
    left = 0
    right = len(text)
    for boundary in _CLAUSE_BOUNDARY.finditer(text):
        if boundary.start() < start:
            left = boundary.end()
        elif boundary.start() >= end:
            right = boundary.start()
            break
    return left, right


def _clause_text(text: str, start: int, end: int) -> str:
    left, right = _clause_bounds(text, start, end)
    return text[left:right].strip()


def _field_quote(field: str, text: str) -> dict[str, str]:
    return {"field": field, "quote": _quote(text), "source": "explicit_body"}


def _empty_terms(
    *,
    extraction: str,
    article: str,
    catalog: str,
    excerpt: str = "",
    residual: str = "none",
    links: int = 0,
    provenance: tuple[dict[str, str], ...] = (),
) -> dict[str, Any]:
    return {
        "conditions": {"status": "unknown", "minimum_amount": None, "minimum_unit": None, "lock_days": None},
        "deadlines": (),
        "deadline_status": "unknown",
        "excerpt": excerpt,
        "extraction_status": extraction,
        "provenance": provenance,
        "residual_conditions": residual,
        "article_detail": article,
        "catalog_status": catalog,
        "links_not_followed": links,
        "duration_stated": False,
        "described_as_temporary": False,
        "temporary_language": "not_described_as_temporary",
    }


def _numbers(
    *,
    source: str,
    record_id: str,
    published_at: str | None,
    unit: str,
    base: Decimal | None,
    reward: Decimal | None,
    total: Decimal | None,
    tokens: tuple[str, bool, tuple[str, ...]],
    terms: dict[str, Any],
    campaign_id: str | None,
    claimed: tuple[str, ...],
    claim_status: str,
    read_coverage: str,
) -> ParsedRecord:
    return ParsedRecord(
        source=source,
        record_id=record_id,
        published_at=published_at,
        unit=unit,
        base=base,
        reward=reward,
        total=total,
        reward_tokens=tokens[2],
        reward_token_status=tokens[0],
        reward_token_count_known=tokens[1],
        conditions=terms["conditions"],
        deadlines=tuple(terms["deadlines"]),
        deadline_status=terms["deadline_status"],
        excerpt=terms["excerpt"],
        extraction_status=terms["extraction_status"],
        provenance=tuple(terms["provenance"]),
        residual_conditions=terms["residual_conditions"],
        article_detail=terms["article_detail"],
        catalog_status=terms["catalog_status"],
        links_not_followed=terms["links_not_followed"],
        described_as_temporary=terms["described_as_temporary"],
        duration_stated=terms["duration_stated"],
        temporary_language=terms["temporary_language"],
        campaign_id=campaign_id,
        claimed_contracts=claimed,
        claim_status=claim_status,
        read_coverage=read_coverage,
    )


def _as_utc(token: str) -> str:
    moment = datetime.fromisoformat(token.replace("Z", "+00:00"))
    if moment.tzinfo is None:
        raise ValueError("timezone")
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _clip(text: str) -> str:
    cleaned = "".join(ch for ch in text if ord(ch) >= 32 and ord(ch) != 127)
    cleaned = " ".join(cleaned.split())
    return cleaned[:240]


def _quote(text: str) -> str:
    body = _clip(text).strip('"')
    if not body:
        return ""
    return f"\"{body}\""


def _link_count(text: str) -> int:
    return len(_LINK.findall(text))
