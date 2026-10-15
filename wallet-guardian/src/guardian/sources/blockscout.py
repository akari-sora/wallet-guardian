"""Blockscout v2 address reads. Paths stay under /api/v2/addresses/.

Pagination uses next_page_params. A missing items field is an error, not an
empty wallet. A failed page keeps the previous cursor.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from guardian.errors import SecurityError
from guardian.security.transport_policy import Operation
from guardian.sources.budget import BootstrapStop, BudgetExhausted
from guardian.sources.etherscan import HistoryRead, plan_not_supported
from guardian.sources.pagination import walk_page_key
from guardian.sources.retry import read_with_retry
from guardian.sources.transport import TransportError

_PREFIX = "/api/v2/addresses/"


class _Stop(Exception):
    pass


_V2_TAILS = {
    "internal-transactions": "history.native_internal",
    "token-transfers": "history.fungible",
    "transactions": "history.native_external",
}


@dataclass
class _State:
    failed: bool = False
    reason: str = ""
    refused: bool = False
    explode: Exception | None = None


class BlockscoutClient:
    """Second-tier REST reader. backend id is blockscout; this client does not set it."""

    def __init__(
        self,
        transport: Any,
        source_id: str,
        *,
        clock: Callable[[], float] | None = None,
        sleep: Callable[[float], None] | None = None,
        random: Callable[[], float] | None = None,
        credential: str | None = None,
    ) -> None:
        self.transport = transport
        self.source_id = source_id
        self.clock = clock or (lambda: 0.0)
        self.sleep = sleep or (lambda _seconds: None)
        self.random = random or (lambda: 0.0)
        self._credential = credential if isinstance(credential, str) else ""

    def operation(
        self,
        path: str,
        query: Mapping[str, str] | None = None,
        headers: tuple[str, ...] | None = None,
    ) -> Operation:
        return Operation(
            protocol="rest",
            adapter="blockscout",
            http_method="GET",
            path=path,
            query=dict(query) if query else None,
            headers=headers,
        )

    def page_query(self, params: Mapping[str, Any]) -> dict[str, str]:
        query: dict[str, str] = {}
        for key, value in params.items():
            if not isinstance(key, str) or not key or isinstance(value, bool) or value is None:
                raise ValueError("bad_page")
            if isinstance(value, int):
                query[key] = str(value)
                continue
            if isinstance(value, str):
                query[key] = value
                continue
            raise ValueError("bad_page")
        # Blockscout v12 removed limit. A page token must not put it back.
        query.pop("limit", None)
        if not query:
            raise ValueError("bad_page")
        return query

    def account_history(
        self,
        *,
        action: str,
        address: str,
        start_block: int,
        end_block: int,
        page_size: int = 1000,
        page_cap: int = 20,
        previous_cursor: str | None = None,
        chain_id: int | None = None,
        pro: bool = False,
    ) -> HistoryRead:
        """Etherscan-compatible account pages.

        PRO uses ``/v2/api`` and ``chain_id``. A key is ``Authorization: Bearer``,
        never a query value. A keyless instance uses ``/api`` and no key.
        """
        capability = {
            "txlist": "history.native_external",
            "txlistinternal": "history.native_internal",
            "tokentx": "history.fungible",
            "getLogs": "history.logs",
        }.get(action, "unspecified")
        if action not in {"txlist", "txlistinternal", "tokentx", "getLogs"}:
            return _kept(capability, previous_cursor, "unknown_action")
        if not isinstance(page_size, int) or isinstance(page_size, bool) or page_size < 1:
            return _kept(capability, previous_cursor, "bad_page")
        if pro and (isinstance(chain_id, bool) or not isinstance(chain_id, int)):
            return _kept(capability, previous_cursor, "chain_id_missing")
        items: list[Any] = []
        page = 1
        while page <= page_cap:
            query = {
                "action": action,
                "address": address,
                "endblock": str(end_block),
                "module": "account" if action != "getLogs" else "logs",
                "offset": str(page_size),
                "page": str(page),
                "sort": "asc",
                "startblock": str(start_block),
            }
            path = "/api"
            headers = None
            if pro:
                query["chain_id"] = str(chain_id)
                path = "/v2/api"
            if self._credential:
                headers = ("authorization",)
            operation = self.operation(path, query, headers)
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
                return HistoryRead(
                    capability, tuple(items), False, False, (exc.reason,), previous_cursor,
                    "partial", False, exc.reason, (exc.reason,), "",
                )
            except TransportError as exc:
                return _kept(capability, previous_cursor, exc.reason)
            except Exception:
                return _kept(capability, previous_cursor, "error")
            refusal = _http_refusal(result)
            if refusal:
                return _refused(capability, previous_cursor, refusal)
            body = result.body
            if not result.ok or not isinstance(body, dict):
                return _kept(capability, previous_cursor, str(getattr(result, "reason", "error") or "error"))
            if action == "txlistinternal" and _indexer_lag(body):
                return HistoryRead(
                    capability, (), False, False, ("indexer_lag",), previous_cursor, "partial",
                    False, "indexer_lag", ("indexer_lag",), "",
                )
            if plan_not_supported(body):
                return HistoryRead(
                    capability, (), False, False, (), previous_cursor, "error", True,
                    "plan_not_supported", ("plan_not_supported",), "",
                )
            rows = body.get("result")
            message = str(body.get("message") or "")
            if isinstance(rows, str):
                lowered = f"{message} {rows}".lower()
                if "no transactions found" in lowered or "no records found" in lowered:
                    break
                return _kept(capability, previous_cursor, "malformed")
            if not isinstance(rows, list):
                return _kept(capability, previous_cursor, "missing_items")
            items.extend(rows)
            if len(rows) < page_size:
                break
            page += 1
        else:
            return _kept(capability, previous_cursor, "page_cap")
        category = "empty" if not items else "ok"
        return HistoryRead(
            capability, tuple(items), True, True, (), str(end_block), category, False, "", (), "",
        )

    def walk(
        self,
        path: str,
        *,
        capability: str,
        page_cap: int = 10,
        item_cap: int = 5_000,
        previous_cursor: str | None = None,
    ) -> HistoryRead:
        if not _address_path(path):
            return _kept(capability, previous_cursor, "bad_path")
        state = _State()

        def fetch(token: str | None) -> dict[str, Any]:
            query = None
            if token:
                try:
                    query = self.page_query(json.loads(token))
                except (ValueError, json.JSONDecodeError):
                    state.failed = True
                    state.reason = "bad_page"
                    raise _Stop() from None
            operation = self.operation(path, query)
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
                raise _Stop() from None
            except Exception:
                state.failed = True
                state.reason = "error"
                raise _Stop() from None
            refusal = _http_refusal(result)
            if refusal:
                state.failed = True
                state.refused = True
                state.reason = refusal
                raise _Stop()
            body = result.body
            if not result.ok or not isinstance(body, dict) or "items" not in body or not isinstance(body.get("items"), list):
                state.failed = True
                state.reason = "missing_items" if result.ok else str(result.reason or "error")
                raise _Stop()
            nxt = body.get("next_page_params")
            if nxt is None:
                return {"items": body["items"], "pageKey": None}
            if not isinstance(nxt, dict) or not nxt:
                state.failed = True
                state.reason = "bad_page"
                raise _Stop()
            encoded = json.dumps(nxt, sort_keys=True, separators=(",", ":"))
            return {"items": body["items"], "pageKey": encoded}

        walk = walk_page_key(fetch, page_cap=page_cap, item_cap=item_cap, items_key="items", token_key="pageKey")
        if state.explode is not None:
            raise state.explode
        if state.refused:
            return _refused(capability, previous_cursor, state.reason or "error")
        if state.failed or walk.stop_reason != "exhausted":
            return _kept(capability, previous_cursor, state.reason or walk.stop_reason or "error")
        category = "empty" if not walk.items else "ok"
        return HistoryRead(
            capability,
            walk.items,
            True,
            True,
            (),
            "exhausted",
            category,
            False,
            "",
            (),
            "",
        )

    def read_v2(
        self,
        address: str,
        tail: str,
        *,
        capability: str | None = None,
        stop_block: int | None = None,
        previous_cursor: str | None = None,
        page_cap: int = 10,
        item_cap: int = 5_000,
        query: Mapping[str, str] | None = None,
        keep_partial: bool = False,
        stop_timestamp: int | None = None,
    ) -> HistoryRead:
        """Newest-first ``/api/v2/addresses/{address}/{tail}``.

        ``tail`` is ``internal-transactions``, ``transactions``, or
        ``token-transfers``. Pages follow ``next_page_params`` and stop once a
        block is at or below ``stop_block``. The request never sends ``limit``.
        A configured credential is ``Authorization: Bearer`` on this same path.
        The base URL stays on the source. This method does not embed a key.

        ``query`` is optional and applies only to the first page. The only
        allowed key is ``filter``, and the only values are ``from`` and ``to``.
        ``keep_partial`` defaults to false. When it is true, a page or item cap
        returns the rows already read, with ``complete`` false and category
        ``partial``. ``stop_timestamp`` stops on an earlier row and treats the
        time window as read. Callers that omit these arguments keep the
        previous result.
        """
        name = _V2_TAILS.get(tail)
        if capability is None:
            capability = name or "unspecified"
        if name is None or not isinstance(address, str) or not address:
            return _kept(capability, previous_cursor, "bad_path")
        path = f"{_PREFIX}{address}/{tail}"
        if not _address_path(path):
            return _kept(capability, previous_cursor, "bad_path")
        if stop_block is not None and (isinstance(stop_block, bool) or not isinstance(stop_block, int)):
            return _kept(capability, previous_cursor, "bad_page")
        if page_cap < 1 or item_cap < 1:
            return _kept(capability, previous_cursor, "bad_page")
        if stop_timestamp is not None and (
            isinstance(stop_timestamp, bool) or not isinstance(stop_timestamp, int) or stop_timestamp < 0
        ):
            return _kept(capability, previous_cursor, "bad_page")
        try:
            first_query = _first_filter(query)
        except ValueError:
            return _kept(capability, previous_cursor, "bad_page")
        kept: list[Any] = []
        token: dict[str, Any] | None = None
        pages = 0
        while pages < page_cap:
            headers = ("authorization",) if self._credential else None
            if token is not None:
                try:
                    request_query = self.page_query(token)
                except ValueError:
                    return _kept(capability, previous_cursor, "bad_page")
                if "limit" in request_query:
                    return _kept(capability, previous_cursor, "bad_page")
            else:
                request_query = first_query
            operation = self.operation(path, request_query, headers)
            try:
                result = read_with_retry(
                    lambda operation=operation: self.transport.request(self.source_id, operation),
                    retry_attempts=self.transport.retry_attempts(self.source_id),
                    sleep=self.sleep,
                    random=self.random,
                    clock=self.clock,
                )
            except (SecurityError, BudgetExhausted):
                raise
            except BootstrapStop as exc:
                return HistoryRead(
                    capability, (), False, False, (exc.reason,), previous_cursor,
                    "partial", False, exc.reason, (exc.reason,), "",
                )
            except TransportError as exc:
                if exc.reason == "fixture_miss":
                    raise
                return _kept(capability, previous_cursor, exc.reason)
            except Exception:
                return _kept(capability, previous_cursor, "error")
            refusal = _http_refusal(result)
            if refusal:
                return _refused(capability, previous_cursor, refusal)
            body = result.body
            if not result.ok or not isinstance(body, dict) or "items" not in body or not isinstance(body.get("items"), list):
                reason = "missing_items" if getattr(result, "ok", False) else str(getattr(result, "reason", "error") or "error")
                return _kept(capability, previous_cursor, reason)
            nxt = body.get("next_page_params")
            if nxt is not None and (not isinstance(nxt, dict) or not nxt):
                return _kept(capability, previous_cursor, "bad_page")
            stop = False
            for item in body["items"]:
                block = _item_block(item)
                if block is None:
                    return _kept(capability, previous_cursor, "missing_block")
                if stop_block is not None and block <= stop_block:
                    stop = True
                    break
                if stop_timestamp is not None:
                    stamp = _item_timestamp(item)
                    if stamp is None:
                        return _kept(capability, previous_cursor, "missing_timestamp")
                    if stamp < stop_timestamp:
                        stop = True
                        break
                kept.append(item)
                if len(kept) >= item_cap:
                    if keep_partial:
                        return _partial(capability, kept, previous_cursor, "item_cap")
                    return _kept(capability, previous_cursor, "item_cap")
            pages += 1
            if stop or nxt is None:
                category = "empty" if not kept else "ok"
                return HistoryRead(
                    capability, tuple(kept), True, True, (), previous_cursor, category, False, "", (), "",
                )
            token = nxt
        if keep_partial:
            return _partial(capability, kept, previous_cursor, "page_cap")
        return _kept(capability, previous_cursor, "page_cap")


def _address_path(path: str) -> bool:
    if not isinstance(path, str) or not path.startswith(_PREFIX):
        return False
    if any(token in path for token in ("\\", "..", "%", "//", "?", "#", "\x00")):
        return False
    rest = path[len(_PREFIX):]
    address, _sep, tail = rest.partition("/")
    return bool(address) and bool(tail)


def _first_filter(query: Mapping[str, str] | None) -> dict[str, str] | None:
    """First-page filter only. Later pages use ``next_page_params`` as sent."""
    if query is None:
        return None
    if not isinstance(query, Mapping) or set(query) != {"filter"}:
        raise ValueError("bad_page")
    value = query.get("filter")
    if value not in {"from", "to"}:
        raise ValueError("bad_page")
    return {"filter": value}


def _partial(capability: str, items: list[Any], previous: str | None, reason: str) -> HistoryRead:
    return HistoryRead(
        capability,
        tuple(items),
        False,
        False,
        (),
        previous,
        "partial",
        False,
        reason,
        (reason,) if reason else (),
        "",
    )


def _kept(capability: str, previous: str | None, reason: str) -> HistoryRead:
    return HistoryRead(capability, (), False, False, (), previous, "error", False, reason, (reason,) if reason else (), "")


def _refused(capability: str, previous: str | None, reason: str) -> HistoryRead:
    return HistoryRead(
        capability, (), False, False, (), previous, "error", True, reason, (reason,), "",
    )


def _http_refusal(result: Any) -> str:
    """Map Blockscout PRO and Cloudflare failures. They are not an empty wallet."""
    status = getattr(result, "status", 0)
    if isinstance(status, bool) or not isinstance(status, int):
        status = 0
    text = _error_text(result)
    if status == 402 or _featured(text) or _needs_key(text):
        if _featured(text):
            return "plan_not_supported"
        return "credential_missing"
    if status == 403 and _bot_challenge(text):
        return "bot_challenge"
    return ""


def _error_text(result: Any) -> str:
    parts = [str(getattr(result, "detail", "") or "")]
    body = getattr(result, "body", None)
    if isinstance(body, dict):
        for key in ("error", "message", "result"):
            value = body.get(key)
            if isinstance(value, str):
                parts.append(value)
    elif isinstance(body, str):
        parts.append(body)
    return " ".join(parts).lower()


def _featured(text: str) -> bool:
    return "featured chain" in text and "plan" in text


def _needs_key(text: str) -> bool:
    return "proceed with api key" in text or "x402" in text


def _bot_challenge(text: str) -> bool:
    return "just a moment" in text


def _indexer_lag(body: Mapping[str, Any]) -> bool:
    """Blockscout ``txlistinternal`` status 2 means the indexer has not caught up."""
    status = body.get("status")
    if status == 2 or status == "2":
        return True
    parts: list[str] = []
    for key in ("message", "result"):
        value = body.get(key)
        if isinstance(value, str):
            parts.append(value.lower())
    return "not yet been processed" in " ".join(parts)


def _item_timestamp(item: Any) -> int | None:
    """Unix seconds for one row. Milliseconds and ISO-8601 are accepted."""
    if not isinstance(item, Mapping):
        return None
    for key in ("timestamp", "timeStamp", "block_timestamp"):
        stamp = _timestamp_seconds(item.get(key))
        if stamp is not None:
            return stamp
    return None


def _timestamp_seconds(value: Any) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return _epoch_seconds(value)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        if text.isdigit():
            return _epoch_seconds(int(text))
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return int(parsed.timestamp())
    return None


def _epoch_seconds(value: int) -> int | None:
    if value < 0:
        return None
    if value >= 1_000_000_000_000:
        return value // 1000
    return value


def _item_block(item: Any) -> int | None:
    if not isinstance(item, Mapping):
        return None
    for key in ("block_number", "blockNumber", "block"):
        value = item.get(key)
        if isinstance(value, bool) or value is None:
            continue
        if isinstance(value, int):
            return value if value >= 0 else None
        if isinstance(value, str):
            text = value.strip()
            if text.isdigit():
                return int(text)
    return None
