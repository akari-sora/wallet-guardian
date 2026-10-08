"""Optional Helius parsed-events enrichment. The key is a query parameter.

A missing key does not construct a client and is BLOCKED, not an empty wallet.
Parsed events never mark RPC coverage complete and never replace on-chain amounts.
processed commitment is rejected. 401 and 403 stay blocked. 429 is retried, then partial.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from guardian.coverage import CoverageObservation
from guardian.errors import SecurityError
from guardian.security.redact import redact
from guardian.security.transport_policy import Operation
from guardian.sources.budget import BudgetExhausted
from guardian.sources.etherscan import HistoryRead, resolve_credential
from guardian.sources.pagination import walk_page_key
from guardian.sources.retry import read_with_retry
from guardian.sources.transport import TransportError

HELIUS_HISTORY_PATH = "/v1/parsed-events/transaction-history"
HELIUS_TRANSACTIONS_PATH = "/v1/parsed-events/transactions"
HELIUS_LIMIT_MAX = 100
_COMMITMENTS = frozenset({"confirmed", "finalized"})
_PROBE_BLOCKED = "blocked"
_PROBE_UNTESTED = "untested"
ENRICHMENT_REASON = "helius_enrichment_not_coverage"


class _Stop(Exception):
    """Stop a page walk without pretending the transport failed open."""


@dataclass
class _State:
    blocked: bool = False
    blocked_reason: str = ""
    partial: bool = False
    failed: bool = False
    reason: str = ""
    detail: str = ""
    probe: str = _PROBE_UNTESTED


@dataclass(frozen=True)
class EnrichmentRead:
    """Helius output. ``completes_rpc_coverage`` is always false."""

    history: HistoryRead
    probe: str
    completes_rpc_coverage: bool = False

    def __post_init__(self) -> None:
        if self.completes_rpc_coverage:
            raise ValueError("helius_cannot_complete_coverage")
        if self.probe not in {"blocked", "documented", "untested"}:
            raise ValueError("probe")


class HeliusClient:
    """POST parsed-events. The API key is not part of repr."""

    def __init__(
        self,
        transport: Any,
        source_id: str,
        *,
        credential: str,
        credential_env: str = "HELIUS_API_KEY",
        clock: Callable[[], float] | None = None,
        sleep: Callable[[float], None] | None = None,
        random: Callable[[], float] | None = None,
    ) -> None:
        if not isinstance(credential, str) or not credential:
            raise ValueError("credential_missing")
        self.transport = transport
        self.source_id = source_id
        self.credential_env = credential_env
        self._credential = credential
        self.clock = clock or (lambda: 0.0)
        self.sleep = sleep or (lambda _seconds: None)
        self.random = random or (lambda: 0.0)

    def __repr__(self) -> str:
        return f"HeliusClient(source_id={self.source_id!r}, credential_env={self.credential_env!r})"

    def redact_error(self, text: str) -> str:
        return redact(text, [self._credential])

    def history_operation(
        self,
        *,
        address: str,
        limit: int,
        sort_order: str,
        commitment: str,
        pagination_token: str | None,
    ) -> Operation:
        body: dict[str, Any] = {
            "address": address,
            "commitment": commitment,
            "limit": limit,
            "sortOrder": sort_order,
        }
        if pagination_token:
            body["paginationToken"] = pagination_token
        return Operation(
            protocol="rest",
            adapter="helius_parsed_events",
            http_method="POST",
            path=HELIUS_HISTORY_PATH,
            query={"api-key": self._credential},
            body=body,
        )

    def transactions_operation(self, signatures: list[str]) -> Operation:
        return Operation(
            protocol="rest",
            adapter="helius_parsed_events",
            http_method="POST",
            path=HELIUS_TRANSACTIONS_PATH,
            query={"api-key": self._credential},
            body={"transactions": list(signatures)},
        )

    def transaction_history(
        self,
        *,
        address: str,
        limit: int = 100,
        sort_order: str = "desc",
        commitment: str = "confirmed",
        pagination_token: str | None = None,
        page_cap: int = 10,
        item_cap: int = 1_000,
        previous_cursor: str | None = None,
        capability: str = "history.native_external",
    ) -> EnrichmentRead:
        if commitment not in _COMMITMENTS:
            return _enrichment(_error(capability, "commitment_rejected", previous_cursor), _PROBE_UNTESTED)
        if sort_order not in {"asc", "desc"}:
            return _enrichment(_error(capability, "bad_sort", previous_cursor), _PROBE_UNTESTED)
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1 or limit > HELIUS_LIMIT_MAX:
            return _enrichment(_error(capability, "bad_limit", previous_cursor), _PROBE_UNTESTED)
        if not isinstance(address, str) or not address:
            return _enrichment(_error(capability, "bad_address", previous_cursor), _PROBE_UNTESTED)
        state = _State()

        def fetch(token: str | None) -> dict[str, Any]:
            operation = self.history_operation(
                address=address,
                limit=limit,
                sort_order=sort_order,
                commitment=commitment,
                pagination_token=token or pagination_token,
            )
            detail = self._detail(operation)
            try:
                result = read_with_retry(
                    lambda: self.transport.request(self.source_id, operation),
                    retry_attempts=self.transport.retry_attempts(self.source_id),
                    sleep=self.sleep,
                    random=self.random,
                    clock=self.clock,
                )
            except (SecurityError, BudgetExhausted):
                raise
            except TransportError as exc:
                state.failed = True
                state.reason = exc.reason or "read_error"
                state.detail = self.redact_error(f"{state.reason} {detail}")
                raise _Stop() from None
            except Exception as exc:
                state.failed = True
                state.reason = "error"
                state.detail = self.redact_error(f"{exc} {detail}")
                raise _Stop() from None
            if getattr(result, "blocked", False):
                state.blocked = True
                state.blocked_reason = str(result.reason or "blocked")
                state.probe = _PROBE_BLOCKED
                state.detail = self.redact_error(detail)
                raise _Stop()
            if result.category == "rate_limited" or getattr(result, "status", 0) == 429:
                state.partial = True
                state.reason = "rate_limited"
                state.detail = self.redact_error(detail)
                raise _Stop()
            if not getattr(result, "ok", False):
                state.failed = True
                state.reason = str(result.reason or "read_error")
                state.detail = self.redact_error(detail)
                raise _Stop()
            body = result.body
            if not isinstance(body, dict) or not isinstance(body.get("transactions"), list):
                state.failed = True
                state.reason = "malformed"
                state.detail = self.redact_error(detail)
                raise _Stop()
            return body

        try:
            walk = walk_page_key(
                fetch,
                page_cap=page_cap,
                item_cap=item_cap,
                items_key="transactions",
                token_key="paginationToken",
            )
        except (SecurityError, BudgetExhausted) as exc:
            reason = str(getattr(exc, "reason", "error") or "error")
            blocked = isinstance(exc, SecurityError) and reason == "endpoint_missing"
            probe = _PROBE_BLOCKED if blocked else _PROBE_UNTESTED
            return _enrichment(_held(capability, reason, previous_cursor, blocked=blocked), probe)
        if state.blocked:
            return _enrichment(
                _held(capability, state.blocked_reason or "blocked", previous_cursor, blocked=True, detail=state.detail),
                _PROBE_BLOCKED,
            )
        if state.partial:
            return _enrichment(
                HistoryRead(
                    capability, (), False, False, ("rate_limited",), previous_cursor, "partial",
                    False, "rate_limited", ("rate_limited",), state.detail,
                ),
                _PROBE_UNTESTED,
            )
        if state.failed or walk.stop_reason != "exhausted":
            reason = state.reason or walk.stop_reason or "error"
            return _enrichment(_held(capability, reason, previous_cursor, detail=state.detail), _PROBE_UNTESTED)
        category = "empty" if not walk.items else "ok"
        cursor = _history_cursor(walk.items, previous_cursor)
        return _enrichment(
            HistoryRead(capability, walk.items, True, True, (), cursor, category, False, "", (), ""),
            _PROBE_UNTESTED,
        )

    def parsed_transactions(
        self,
        signatures: list[str],
        *,
        previous_cursor: str | None = None,
        capability: str = "history.fungible",
    ) -> EnrichmentRead:
        if not isinstance(signatures, list) or any(not isinstance(item, str) or not item for item in signatures):
            return _enrichment(_error(capability, "bad_signature", previous_cursor), _PROBE_UNTESTED)
        if len(signatures) > HELIUS_LIMIT_MAX:
            return _enrichment(_error(capability, "bad_limit", previous_cursor), _PROBE_UNTESTED)
        operation = self.transactions_operation(signatures)
        detail = self._detail(operation)
        try:
            result = read_with_retry(
                lambda: self.transport.request(self.source_id, operation),
                retry_attempts=self.transport.retry_attempts(self.source_id),
                sleep=self.sleep,
                random=self.random,
                clock=self.clock,
            )
        except (SecurityError, BudgetExhausted) as exc:
            reason = str(getattr(exc, "reason", "error") or "error")
            blocked = isinstance(exc, SecurityError) and reason == "endpoint_missing"
            return _enrichment(_held(capability, reason, previous_cursor, blocked=blocked), _PROBE_BLOCKED if blocked else _PROBE_UNTESTED)
        except TransportError as exc:
            return _enrichment(
                _held(capability, exc.reason or "read_error", previous_cursor, detail=self.redact_error(f"{exc.reason} {detail}")),
                _PROBE_UNTESTED,
            )
        if getattr(result, "blocked", False):
            return _enrichment(
                _held(capability, str(result.reason or "blocked"), previous_cursor, blocked=True, detail=self.redact_error(detail)),
                _PROBE_BLOCKED,
            )
        if result.category == "rate_limited" or getattr(result, "status", 0) == 429:
            return _enrichment(
                HistoryRead(capability, (), False, False, ("rate_limited",), previous_cursor, "partial", False, "rate_limited", ("rate_limited",), self.redact_error(detail)),
                _PROBE_UNTESTED,
            )
        if not getattr(result, "ok", False) or not isinstance(result.body, dict) or not isinstance(result.body.get("transactions"), list):
            return _enrichment(_held(capability, "malformed", previous_cursor, detail=self.redact_error(detail)), _PROBE_UNTESTED)
        items = tuple(result.body["transactions"])
        category = "empty" if not items else "ok"
        return _enrichment(HistoryRead(capability, items, True, True, (), previous_cursor, category, False, "", (), ""), _PROBE_UNTESTED)

    def _detail(self, operation: Operation) -> str:
        try:
            url = self.transport.encode(self.source_id, operation).url
        except Exception:
            url = operation.path or ""
        return self.redact_error(url)


def open_helius(
    transport: Any,
    source_id: str,
    environ: Mapping[str, str],
    *,
    credential_env: str = "HELIUS_API_KEY",
    capability: str = "history.native_external",
    previous_cursor: str | None = None,
    clock: Callable[[], float] | None = None,
    sleep: Callable[[float], None] | None = None,
    random: Callable[[], float] | None = None,
) -> tuple[HeliusClient | None, EnrichmentRead | None]:
    """Missing or blank keys are BLOCKED. The client is not constructed."""
    try:
        credential = resolve_credential(environ, credential_env)
    except ValueError:
        read = _held(capability, "credential_missing", previous_cursor, blocked=True)
        return None, _enrichment(read, _PROBE_BLOCKED)
    client = HeliusClient(
        transport,
        source_id,
        credential=credential,
        credential_env=credential_env,
        clock=clock,
        sleep=sleep,
        random=random,
    )
    return client, None


def authoritative_amount(rpc_amount: int | None, helius_amount: int | None) -> tuple[int | None, tuple[str, ...]]:
    """On-chain integers win. A missing RPC amount stays missing."""
    if isinstance(rpc_amount, bool) or isinstance(helius_amount, bool):
        return None, ("malformed",)
    if isinstance(rpc_amount, float) or isinstance(helius_amount, float):
        return None, ("float_amount_rejected",)
    if rpc_amount is None:
        return None, ("rpc_amount_missing",)
    if rpc_amount < 0:
        return None, ("malformed",)
    if helius_amount is not None and helius_amount != rpc_amount:
        return rpc_amount, ("helius_amount_not_authoritative",)
    return rpc_amount, ()


def coverage_from_enrichment(read: HistoryRead, *, source: str = "helius") -> CoverageObservation:
    """Enrichment is not a complete RPC observation, even when the HTTP walk finished."""
    reasons = tuple(dict.fromkeys([*read.reason_codes, ENRICHMENT_REASON]))
    gaps = tuple(dict.fromkeys([*read.gaps, ENRICHMENT_REASON]))
    category = "error" if read.category == "error" or read.blocked else "partial"
    return CoverageObservation(
        category=category,
        pagination_complete=False,
        gap_detection_complete=False,
        gaps=gaps,
        cursor=read.cursor,
        declared="helius_enrichment",
        sources=(source,),
        trust="unverified",
        reason_codes=reasons,
    )


def _enrichment(read: HistoryRead, probe: str) -> EnrichmentRead:
    return EnrichmentRead(read, probe, False)


def _error(capability: str, reason: str, previous: str | None) -> HistoryRead:
    return _held(capability, reason, previous, blocked=False)


def _held(
    capability: str,
    reason: str,
    previous: str | None,
    *,
    blocked: bool = False,
    detail: str = "",
) -> HistoryRead:
    safe_detail = detail
    return HistoryRead(
        capability, (), False, False, (), previous, "error", blocked, reason, (reason,) if reason else (), safe_detail,
    )


def _history_cursor(items: tuple[Any, ...], previous: str | None) -> str | None:
    if not items:
        return previous if previous is not None else "empty"
    first = items[0]
    if isinstance(first, Mapping):
        signature = first.get("signature")
        if isinstance(signature, str) and signature:
            return signature
    return previous if previous is not None else "helius"
