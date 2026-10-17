"""Etherscan V2 reads over the existing read-only transport.

The API key is a query parameter supplied by the caller. This module does not
log it. A free-plan refusal is BLOCKED, not an empty wallet. A failed or
partial page does not advance the cursor.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from typing import Any

from guardian.errors import SecurityError
from guardian.security.redact import redact
from guardian.security.transport_policy import Operation
from guardian.sources.budget import BootstrapStop, BudgetExhausted
from guardian.sources.pagination import walk_page_offset
from guardian.sources.retry import read_with_retry
from guardian.sources.transport import TransportError

ETHERSCAN_RESULT_WINDOW = 10_000
_ENV_NAME = re.compile(r"^[A-Z][A-Z0-9_]{0,95}$")
_ADDRESS = re.compile(r"^0x[0-9a-fA-F]{40}$")
_ACTIONS = {
    "txlist": "history.native_external",
    "txlistinternal": "history.native_internal",
    "tokentx": "history.fungible",
    "tokennfttx": "history.nft",
    "token1155tx": "history.nft",
}


class _Stop(Exception):
    """Stop the page walker without looking like a transport failure."""


@dataclass
class _State:
    window: bool = False
    plan: bool = False
    partial: bool = False
    failed: bool = False
    cap: bool = False
    reason: str = ""
    detail: str = ""
    explode: Exception | None = None


@dataclass(frozen=True)
class HistoryRead:
    """One capability read. ``category`` ``empty`` is a real empty result."""

    capability: str
    items: tuple[Any, ...]
    pagination_complete: bool
    gap_detection_complete: bool
    gaps: tuple[str, ...]
    cursor: str | None
    category: str
    blocked: bool = False
    reason: str = ""
    reason_codes: tuple[str, ...] = ()
    detail: str = ""
    page_lengths: tuple[int, ...] = ()

    def __post_init__(self) -> None:
        if self.category not in {"ok", "empty", "partial", "error"}:
            raise ValueError("category")
        object.__setattr__(self, "items", tuple(self.items))
        object.__setattr__(self, "gaps", tuple(self.gaps))
        object.__setattr__(self, "reason_codes", tuple(self.reason_codes))
        object.__setattr__(self, "page_lengths", tuple(self.page_lengths))


def resolve_credential(
    environ: Mapping[str, str],
    name: str,
    profile: Mapping[str, str] | None = None,
) -> str:
    """Resolve a secret by variable name. Profile wins, then the process environment.

    The name is not a value. A 401 does not switch to the other source; callers
    keep the string this function returned.
    """
    if not isinstance(name, str) or _ENV_NAME.fullmatch(name) is None:
        raise ValueError("credential_env")
    from guardian.credentials import profile_from_environ, resolve_named

    loaded = profile if profile is not None else profile_from_environ(environ)
    found = resolve_named(name, loaded, environ)
    if not found.present:
        raise ValueError("credential_missing")
    return found.reveal()


def plan_not_supported(body: Any) -> bool:
    """Free-plan and similar NOTOK refusals. Empty history is not one of these."""
    if not isinstance(body, dict) or str(body.get("status")) != "0":
        return False
    parts: list[str] = []
    for key in ("message", "result"):
        value = body.get(key)
        if isinstance(value, str):
            parts.append(value.lower())
    text = " ".join(parts)
    if "no transactions found" in text or "no records found" in text:
        return False
    if "max rate limit reached" in text:
        return False
    if "not support" in text and ("free" in text or "plan" in text):
        return True
    return False


class _Counter:
    def __init__(self, limit: int) -> None:
        self.limit = limit
        self.used = 0

    def allow(self) -> bool:
        if self.used >= self.limit:
            return False
        self.used += 1
        return True


class EtherscanClient:
    """GET /v2/api. Module and action are enforced by the transport policy."""

    def __init__(
        self,
        transport: Any,
        source_id: str,
        *,
        credential: str,
        credential_env: str = "ETHERSCAN_API_KEY",
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
        return f"EtherscanClient(source_id={self.source_id!r}, credential_env={self.credential_env!r})"

    def redact_error(self, text: str) -> str:
        return redact(text, [self._credential])

    def operation(
        self,
        *,
        chain_id: int,
        module: str,
        action: str,
        address: str,
        start_block: int,
        end_block: int,
        page: int,
        offset: int,
    ) -> Operation:
        query = {
            "action": action,
            "address": address,
            "apikey": self._credential,
            "chainid": str(chain_id),
            "endblock": str(end_block),
            "module": module,
            "offset": str(offset),
            "page": str(page),
            "sort": "asc",
            "startblock": str(start_block),
        }
        return Operation(
            protocol="rest",
            adapter="etherscan_v2",
            http_method="GET",
            path="/v2/api",
            query=query,
        )

    def account_history(
        self,
        *,
        chain_id: int,
        action: str,
        address: str,
        start_block: int,
        end_block: int,
        offset: int = 1000,
        page_cap: int = 20,
        item_cap: int = 50_000,
        previous_cursor: str | None = None,
        request_cap: int = 32,
        module: str = "account",
    ) -> HistoryRead:
        capability = _ACTIONS.get(action, "unspecified")
        if isinstance(chain_id, bool) or not isinstance(chain_id, int):
            return _failed(capability, "bad_chain", previous_cursor)
        if isinstance(start_block, bool) or isinstance(end_block, bool):
            return _failed(capability, "bad_range", previous_cursor)
        if not isinstance(start_block, int) or not isinstance(end_block, int) or end_block < start_block:
            return _failed(capability, "bad_range", previous_cursor)
        if isinstance(offset, bool) or not isinstance(offset, int) or offset < 1 or offset > ETHERSCAN_RESULT_WINDOW:
            return _failed(capability, "offset_out_of_range", previous_cursor)
        if not isinstance(address, str) or _ADDRESS.fullmatch(address) is None:
            return _failed(capability, "bad_address", previous_cursor)
        counter = _Counter(request_cap)
        read = self._walk_range(
            chain_id=chain_id,
            module=module,
            action=action,
            address=address,
            start=start_block,
            end=end_block,
            offset=offset,
            page_cap=page_cap,
            item_cap=item_cap,
            counter=counter,
            capability=capability,
        )
        return _with_cursor(read, previous_cursor, end_block)

    def _walk_range(
        self,
        *,
        chain_id: int,
        module: str,
        action: str,
        address: str,
        start: int,
        end: int,
        offset: int,
        page_cap: int,
        item_cap: int,
        counter: _Counter,
        capability: str,
    ) -> HistoryRead:
        state = _State()

        def fetch(page: int) -> list[Any]:
            if page * offset > ETHERSCAN_RESULT_WINDOW:
                state.window = True
                raise _Stop()
            if not counter.allow():
                state.cap = True
                state.reason = "request_cap"
                raise _Stop()
            operation = self.operation(
                chain_id=chain_id,
                module=module,
                action=action,
                address=address,
                start_block=start,
                end_block=end,
                page=page,
                offset=offset,
            )
            detail = self.redact_error(self.transport.encode(self.source_id, operation).url)
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
            except BootstrapStop as exc:
                state.reason = exc.reason
                raise _Stop() from None
            except TransportError as exc:
                if exc.reason == "fixture_miss":
                    state.explode = exc
                state.failed = True
                state.reason = exc.reason
                state.detail = self.redact_error(f"{exc.reason} {detail}")
                raise _Stop() from None
            except Exception as exc:
                state.failed = True
                state.reason = "error"
                state.detail = self.redact_error(f"{exc} {detail}")
                raise _Stop() from None
            if plan_not_supported(result.body):
                state.plan = True
                state.detail = detail
                raise _Stop()
            if result.category == "empty":
                return []
            if result.category == "partial":
                state.partial = True
                state.detail = detail
                raise _Stop()
            if result.category != "ok":
                state.failed = True
                state.reason = str(result.reason or "error")
                state.detail = detail
                raise _Stop()
            body = result.body
            if not isinstance(body, dict) or not isinstance(body.get("result"), list):
                state.failed = True
                state.reason = "malformed"
                state.detail = detail
                raise _Stop()
            return list(body["result"])

        walk = walk_page_offset(fetch, page_cap=page_cap, item_cap=item_cap, offset=offset)
        if state.explode is not None:
            raise state.explode
        if state.reason in {"bootstrap_request_cap", "deadline"}:
            return HistoryRead(
                capability,
                tuple(walk.items),
                False,
                False,
                (state.reason,),
                None,
                "partial",
                False,
                state.reason,
                (state.reason,),
                "",
            )
        if state.plan:
            return HistoryRead(
                capability,
                (),
                False,
                False,
                (),
                None,
                "error",
                True,
                "plan_not_supported",
                ("plan_not_supported",),
                state.detail,
            )
        if state.cap:
            return _open(capability, "request_cap", state.detail)
        if state.window:
            if start == end:
                gap = f"single_block_over_limit:{start}"
                return HistoryRead(
                    capability,
                    (),
                    False,
                    True,
                    (gap,),
                    None,
                    "partial",
                    False,
                    gap,
                    (gap,),
                    "",
                )
            mid = start + (end - start) // 2
            left = self._walk_range(
                chain_id=chain_id,
                module=module,
                action=action,
                address=address,
                start=start,
                end=mid,
                offset=offset,
                page_cap=page_cap,
                item_cap=item_cap,
                counter=counter,
                capability=capability,
            )
            if left.blocked or left.category == "error" or left.reason in {"bootstrap_request_cap", "deadline"}:
                return left
            right = self._walk_range(
                chain_id=chain_id,
                module=module,
                action=action,
                address=address,
                start=mid + 1,
                end=end,
                offset=offset,
                page_cap=page_cap,
                item_cap=item_cap,
                counter=counter,
                capability=capability,
            )
            return _merge(left, right)
        if state.partial:
            return _open(capability, "partial", state.detail, category="partial")
        if state.failed or walk.stop_reason != "exhausted":
            return _open(capability, state.reason or walk.stop_reason or "error", state.detail)
        category = "empty" if not walk.items else "ok"
        return HistoryRead(capability, walk.items, True, True, (), None, category, False, "", (), "")


def _with_cursor(read: HistoryRead, previous: str | None, end_block: int) -> HistoryRead:
    success = (
        read.pagination_complete
        and read.category in {"ok", "empty"}
        and not read.gaps
        and not read.blocked
    )
    cursor = str(end_block) if success else previous
    detail = read.detail
    if detail:
        detail = redact(detail)
    return replace(read, cursor=cursor, detail=detail)


def _failed(capability: str, reason: str, previous: str | None) -> HistoryRead:
    return HistoryRead(capability, (), False, False, (), previous, "error", False, reason, (reason,), "")


def _open(capability: str, reason: str, detail: str, *, category: str = "error") -> HistoryRead:
    codes = (reason,) if reason else ()
    return HistoryRead(capability, (), False, False, (), None, category, False, reason, codes, detail)


def _merge(left: HistoryRead, right: HistoryRead) -> HistoryRead:
    if left.blocked or right.blocked:
        chosen = left if left.blocked else right
        return replace(chosen, items=())
    gaps = left.gaps + tuple(gap for gap in right.gaps if gap not in left.gaps)
    complete = (
        left.pagination_complete
        and right.pagination_complete
        and not gaps
        and left.category in {"ok", "empty"}
        and right.category in {"ok", "empty"}
    )
    if not complete:
        category = "partial" if gaps or left.category == "partial" or right.category == "partial" else "error"
        reason = gaps[0] if gaps else (left.reason or right.reason or "partial")
        return HistoryRead(
            left.capability,
            (),
            False,
            left.gap_detection_complete and right.gap_detection_complete,
            gaps,
            None,
            category,
            False,
            reason,
            gaps or ((reason,) if reason else ()),
            left.detail or right.detail,
        )
    items = left.items + right.items
    category = "empty" if not items else "ok"
    return HistoryRead(left.capability, items, True, True, (), None, category, False, "", (), "")
