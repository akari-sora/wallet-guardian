"""Blockscout v2 address reads. Paths stay under /api/v2/addresses/.

Pagination uses next_page_params. A missing items field is an error, not an
empty wallet. A failed page keeps the previous cursor.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from guardian.errors import SecurityError
from guardian.security.transport_policy import Operation
from guardian.sources.budget import BudgetExhausted
from guardian.sources.etherscan import HistoryRead, plan_not_supported
from guardian.sources.pagination import walk_page_key
from guardian.sources.retry import read_with_retry
from guardian.sources.transport import TransportError

_PREFIX = "/api/v2/addresses/"


class _Stop(Exception):
    pass


@dataclass
class _State:
    failed: bool = False
    reason: str = ""
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

    def operation(self, path: str, query: Mapping[str, str] | None = None) -> Operation:
        return Operation(
            protocol="rest",
            adapter="blockscout",
            http_method="GET",
            path=path,
            query=dict(query) if query else None,
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
    ) -> HistoryRead:
        """Etherscan-compatible /api account pages. The key is a query value, not a log line."""
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
            if self._credential:
                query["apikey"] = self._credential
            operation = self.operation("/api", query)
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
                return _kept(capability, previous_cursor, exc.reason)
            except Exception:
                return _kept(capability, previous_cursor, "error")
            body = result.body
            if plan_not_supported(body):
                return HistoryRead(
                    capability, (), False, False, (), previous_cursor, "error", True,
                    "plan_not_supported", ("plan_not_supported",), "",
                )
            if not result.ok or not isinstance(body, dict):
                return _kept(capability, previous_cursor, str(getattr(result, "reason", "error") or "error"))
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


def _address_path(path: str) -> bool:
    if not isinstance(path, str) or not path.startswith(_PREFIX):
        return False
    if any(token in path for token in ("\\", "..", "%", "//", "?", "#", "\x00")):
        return False
    rest = path[len(_PREFIX):]
    address, _sep, tail = rest.partition("/")
    return bool(address) and bool(tail)


def _kept(capability: str, previous: str | None, reason: str) -> HistoryRead:
    return HistoryRead(capability, (), False, False, (), previous, "error", False, reason, (reason,) if reason else (), "")
