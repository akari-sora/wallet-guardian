"""NodeReal nr_getAssetTransfers for Ethereum and BNB Chain only.

A range wider than 100000 blocks is split. maxCount stays at 0x3E8. pageKey
walks one chunk at a time. Categories are not interchangeable: external,
internal, 20, 721, and 1155 each keep their own capability.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from guardian.errors import SecurityError
from guardian.security.transport_policy import Operation
from guardian.sources.budget import BootstrapStop, BudgetExhausted
from guardian.sources.etherscan import HistoryRead
from guardian.sources.pagination import walk_page_key
from guardian.sources.retry import read_with_retry
from guardian.sources.transport import TransportError

NODEREAL_NETWORKS = frozenset({"ethereum-mainnet", "bsc-mainnet"})
MAX_BLOCK_SPAN = 100_000
MAX_COUNT = 0x3E8
CATEGORY_CAPABILITY = {
    "external": "history.native_external",
    "internal": "history.native_internal",
    "20": "history.fungible",
    "721": "history.nft",
    "1155": "history.nft",
}
_ADDRESS = re.compile(r"^0x[0-9a-fA-F]{40}$")


class _Stop(Exception):
    pass


@dataclass
class _State:
    failed: bool = False
    blocked: bool = False
    reason: str = ""
    explode: Exception | None = None


class NodeRealClient:
    def __init__(
        self,
        transport: Any,
        source_id: str,
        *,
        network: str,
        clock: Callable[[], float] | None = None,
        sleep: Callable[[float], None] | None = None,
        random: Callable[[], float] | None = None,
    ) -> None:
        self.transport = transport
        self.source_id = source_id
        self.network = network
        self.clock = clock or (lambda: 0.0)
        self.sleep = sleep or (lambda _seconds: None)
        self.random = random or (lambda: 0.0)

    def operation(
        self,
        *,
        category: str,
        address: str,
        start: int,
        end: int,
        page_key: str | None,
        side: str,
    ) -> Operation:
        params: dict[str, Any] = {
            "category": [category],
            "fromBlock": hex(start),
            "maxCount": hex(MAX_COUNT),
            "order": "asc",
            "toBlock": hex(end),
        }
        if side == "from":
            params["fromAddress"] = address
        elif side == "to":
            params["toAddress"] = address
        else:
            raise ValueError("side")
        if page_key:
            params["pageKey"] = page_key
        return Operation(
            protocol="jsonrpc",
            adapter="nodereal_transfers",
            method="nr_getAssetTransfers",
            params=[params],
        )

    def transfers(
        self,
        *,
        category: str,
        address: str,
        start_block: int,
        end_block: int,
        side: str = "to",
        page_cap: int = 10,
        item_cap: int = 5_000,
        previous_cursor: str | None = None,
        request_cap: int = 32,
    ) -> HistoryRead:
        capability = CATEGORY_CAPABILITY.get(category, "")
        if self.network not in NODEREAL_NETWORKS:
            return HistoryRead(
                capability or "unspecified",
                (),
                False,
                False,
                (),
                previous_cursor,
                "error",
                True,
                "nodereal_chain_not_supported",
                ("nodereal_chain_not_supported",),
                "",
            )
        if category not in CATEGORY_CAPABILITY:
            return _kept("unspecified", previous_cursor, "unknown_category")
        if side not in {"from", "to"}:
            return _kept(capability, previous_cursor, "bad_side")
        if not isinstance(address, str) or _ADDRESS.fullmatch(address) is None:
            return _kept(capability, previous_cursor, "bad_address")
        if isinstance(start_block, bool) or isinstance(end_block, bool):
            return _kept(capability, previous_cursor, "bad_range")
        if not isinstance(start_block, int) or not isinstance(end_block, int) or end_block < start_block:
            return _kept(capability, previous_cursor, "bad_range")
        counter = _Counter(request_cap)
        items: list[Any] = []
        for start, end in _chunks(start_block, end_block):
            chunk = self._walk_chunk(
                category=category,
                address=address,
                start=start,
                end=end,
                side=side,
                page_cap=page_cap,
                item_cap=item_cap,
                counter=counter,
                capability=capability,
            )
            if chunk is None:
                return _kept(capability, previous_cursor, "error")
            if not chunk.pagination_complete or chunk.category not in {"ok", "empty"}:
                stopped = chunk.reason in {"bootstrap_request_cap", "deadline"}
                return HistoryRead(
                    capability,
                    tuple(items) + tuple(chunk.items) if stopped else (),
                    False,
                    False,
                    chunk.gaps,
                    previous_cursor,
                    "partial" if stopped else (chunk.category if chunk.category in {"partial", "error"} else "error"),
                    chunk.blocked,
                    chunk.reason or "error",
                    chunk.reason_codes or ((chunk.reason,) if chunk.reason else ()),
                    chunk.detail,
                )
            items.extend(chunk.items)
        category_name = "empty" if not items else "ok"
        return HistoryRead(capability, tuple(items), True, True, (), str(end_block), category_name, False, "", (), "")

    def _walk_chunk(
        self,
        *,
        category: str,
        address: str,
        start: int,
        end: int,
        side: str,
        page_cap: int,
        item_cap: int,
        counter: _Counter,
        capability: str,
    ) -> HistoryRead | None:
        state = _State()

        def fetch(token: str | None) -> dict[str, Any]:
            if not counter.allow():
                state.failed = True
                state.reason = "request_cap"
                raise _Stop()
            operation = self.operation(
                category=category,
                address=address,
                start=start,
                end=end,
                page_key=token,
                side=side,
            )
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
            body = result.body
            refusal = _method_refusal(body)
            if refusal:
                state.failed = True
                state.blocked = True
                state.reason = refusal
                raise _Stop()
            if not result.ok or not isinstance(body, dict):
                state.failed = True
                state.reason = str(getattr(result, "reason", "error"))
                raise _Stop()
            inner = body.get("result")
            if not isinstance(inner, dict) or not isinstance(inner.get("transfers"), list):
                state.failed = True
                state.reason = "missing_transfers"
                raise _Stop()
            page_key = inner.get("pageKey")
            if page_key is None:
                encoded = None
            elif isinstance(page_key, str) and page_key:
                encoded = page_key
            else:
                state.failed = True
                state.reason = "bad_page_key"
                raise _Stop()
            return {"pageKey": encoded, "transfers": inner["transfers"]}

        walk = walk_page_key(fetch, page_cap=page_cap, item_cap=item_cap)
        if state.explode is not None:
            raise state.explode
        if state.reason in {"bootstrap_request_cap", "deadline"}:
            tagged = tuple(_tag(item, category) for item in walk.items)
            return HistoryRead(
                capability, tagged, False, False, (), None, "partial", False, state.reason, (state.reason,), "",
            )
        if state.failed or walk.stop_reason != "exhausted":
            reason = state.reason or walk.stop_reason or "error"
            return HistoryRead(
                capability, (), False, False, (), None, "error", state.blocked, reason, (reason,), "",
            )
        tagged = tuple(_tag(item, category) for item in walk.items)
        name = "empty" if not tagged else "ok"
        return HistoryRead(capability, tagged, True, True, (), None, name, False, "", (), "")


class _Counter:
    def __init__(self, limit: int) -> None:
        self.limit = limit
        self.used = 0

    def allow(self) -> bool:
        if self.used >= self.limit:
            return False
        self.used += 1
        return True


def _chunks(start: int, end: int) -> list[tuple[int, int]]:
    chunks: list[tuple[int, int]] = []
    cursor = start
    while cursor <= end:
        chunk_end = min(end, cursor + MAX_BLOCK_SPAN)
        chunks.append((cursor, chunk_end))
        if chunk_end == end:
            break
        cursor = chunk_end + 1
    return chunks


def _tag(item: Any, category: str) -> Any:
    if not isinstance(item, dict):
        return item
    copied = dict(item)
    copied.setdefault("category", category)
    return copied


def _method_refusal(body: Any) -> str | None:
    """A refused method is blocked. It is not an empty transfer list and not an eth_call."""
    if not isinstance(body, dict):
        return None
    err = body.get("error")
    code = None
    message = ""
    if isinstance(err, dict):
        code = err.get("code")
        message = str(err.get("message") or "")
    elif isinstance(err, str):
        message = err
    text = message.lower()
    if code == -32601 or "method not found" in text or "method-not-found" in text:
        return "method_not_found"
    if code in {-32001, 401} or "unauthorized" in text or "not allowed" in text:
        return "unauthorized"
    return None


def _kept(capability: str, previous: str | None, reason: str) -> HistoryRead:
    return HistoryRead(capability, (), False, False, (), previous, "error", False, reason, (reason,) if reason else (), "")
