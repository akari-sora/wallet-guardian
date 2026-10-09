"""Alchemy Transfers reads for Base. Approval logs are not this path.

The endpoint URL comes from ``ALCHEMY_TRANSFERS_URL``. Alchemy puts the key in
that URL, so this module never appends ``ALCHEMY_API_KEY`` and never logs the
URL. ``alchemy_getAssetTransfers`` is 120 compute units per call in Alchemy's
published table; that cost is an estimate, not a live quota reading.

Free-tier ``eth_getLogs`` is 10 blocks. This adapter does not call it.
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

LOG_BLOCK_CAP = 10
ENDPOINT_ENV = "ALCHEMY_TRANSFERS_URL"
CREDENTIAL_ENV = "ALCHEMY_API_KEY"
ROUTED_BY_DEFAULT = True
CU_ESTIMATE = 120
TRANSFER_CATEGORIES = ("external", "erc20", "erc721", "erc1155")
MAX_COUNT = 0x3E8
_ADDRESS = re.compile(r"^0x[0-9a-fA-F]{40}$")
_NETWORKS = frozenset({"base-mainnet"})


class _Stop(Exception):
    pass


@dataclass
class _State:
    failed: bool = False
    blocked: bool = False
    reason: str = ""
    explode: Exception | None = None


class _Counter:
    def __init__(self, limit: int) -> None:
        self.limit = limit
        self.used = 0

    def allow(self) -> bool:
        if self.used >= self.limit:
            return False
        self.used += 1
        return True


class AlchemyClient:
    """One ``alchemy_getAssetTransfers`` walk. ``from`` and ``to`` are separate calls."""

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
        address: str,
        start: int,
        end: int,
        page_key: str | None,
        side: str,
    ) -> Operation:
        params: dict[str, Any] = {
            "category": list(TRANSFER_CATEGORIES),
            "excludeZeroValue": False,
            "fromBlock": hex(start),
            "maxCount": hex(MAX_COUNT),
            "order": "asc",
            "toBlock": hex(end),
            "withMetadata": False,
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
            adapter="alchemy_transfers",
            method="alchemy_getAssetTransfers",
            params=[params],
        )

    def transfers(
        self,
        *,
        address: str,
        start_block: int,
        end_block: int,
        side: str,
        previous_cursor: str | None = None,
        page_cap: int = 10,
        item_cap: int = 5_000,
        request_cap: int = 32,
    ) -> HistoryRead:
        capability = "history.native_external"
        if self.network not in _NETWORKS:
            return _refused(capability, previous_cursor, "alchemy_chain_not_supported")
        if side not in {"from", "to"}:
            return _kept(capability, previous_cursor, "bad_side")
        if not isinstance(address, str) or _ADDRESS.fullmatch(address) is None:
            return _kept(capability, previous_cursor, "bad_address")
        if isinstance(start_block, bool) or isinstance(end_block, bool):
            return _kept(capability, previous_cursor, "bad_range")
        if not isinstance(start_block, int) or not isinstance(end_block, int) or end_block < start_block or start_block < 0:
            return _kept(capability, previous_cursor, "bad_range")
        state = _State()
        counter = _Counter(request_cap)

        def fetch(token: str | None) -> dict[str, Any]:
            if not counter.allow():
                state.failed = True
                state.reason = "request_cap"
                raise _Stop()
            operation = self.operation(
                address=address,
                start=start_block,
                end=end_block,
                page_key=token,
                side=side,
            )
            try:
                result = read_with_retry(
                    lambda: self.transport.request(self.source_id, operation, credits=CU_ESTIMATE),
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
            refusal = _refusal(result)
            if refusal:
                state.failed = True
                state.blocked = True
                state.reason = refusal
                raise _Stop()
            body = result.body
            if not result.ok or not isinstance(body, dict):
                state.failed = True
                state.reason = str(getattr(result, "reason", "error") or "error")
                raise _Stop()
            inner = body.get("result")
            if not isinstance(inner, dict) or not isinstance(inner.get("transfers"), list):
                state.failed = True
                state.reason = "missing_transfers"
                raise _Stop()
            transfers = inner["transfers"]
            if any(not isinstance(item, dict) or item.get("category") not in TRANSFER_CATEGORIES for item in transfers):
                state.failed = True
                state.reason = "bad_category"
                raise _Stop()
            page_key = inner.get("pageKey")
            if page_key in (None, ""):
                encoded = None
            elif isinstance(page_key, str):
                encoded = page_key
            else:
                state.failed = True
                state.reason = "bad_page_key"
                raise _Stop()
            return {"pageKey": encoded, "transfers": transfers}

        walk = walk_page_key(fetch, page_cap=page_cap, item_cap=item_cap)
        if state.explode is not None:
            raise state.explode
        if state.reason in {"bootstrap_request_cap", "deadline"}:
            return HistoryRead(
                capability,
                tuple(walk.items),
                False,
                False,
                (),
                previous_cursor,
                "partial",
                False,
                state.reason,
                (state.reason,),
                "",
            )
        if state.failed or walk.stop_reason != "exhausted":
            reason = state.reason or walk.stop_reason or "error"
            return HistoryRead(
                capability,
                (),
                False,
                False,
                (),
                previous_cursor,
                "error",
                state.blocked,
                reason,
                (reason,) if reason else (),
                "",
            )
        name = "empty" if not walk.items else "ok"
        return HistoryRead(
            capability,
            tuple(walk.items),
            True,
            True,
            (),
            str(end_block),
            name,
            False,
            "",
            (),
            "",
        )


def describe() -> dict[str, object]:
    return {
        "credential_env": CREDENTIAL_ENV,
        "cu_per_call_estimate": CU_ESTIMATE,
        "does_not_replace": ["approval_logs", "operator_logs"],
        "endpoint_env": ENDPOINT_ENV,
        "free_eth_getLogs_block_cap": LOG_BLOCK_CAP,
        "free_eth_getLogs_is_approval_path": False,
        "routed_by_default": ROUTED_BY_DEFAULT,
        "supports": ["asset_transfers"],
    }


def _kept(capability: str, previous: str | None, reason: str) -> HistoryRead:
    return HistoryRead(capability, (), False, False, (), previous, "error", False, reason, (reason,) if reason else (), "")


def _refused(capability: str, previous: str | None, reason: str) -> HistoryRead:
    return HistoryRead(capability, (), False, False, (), previous, "error", True, reason, (reason,), "")


def _refusal(result: Any) -> str | None:
    """A refused Alchemy call is blocked. It is not an empty transfer list."""
    status = getattr(result, "status", 0)
    if isinstance(status, bool) or not isinstance(status, int):
        status = 0
    text = _error_text(result)
    code = _error_code(getattr(result, "body", None))
    if "just a moment" in text:
        return "bot_challenge"
    if code == -32601 or "method not found" in text or "method-not-found" in text:
        return "method_not_found"
    if status == 402 or ("plan" in text and ("not support" in text or "upgrade" in text)):
        return "plan_not_supported"
    if (
        status in {401, 403}
        or code in {-32001, 401}
        or "unauthorized" in text
        or "invalid api key" in text
        or "not allowed" in text
    ):
        return "unauthorized"
    return None


def _error_code(body: Any) -> Any:
    if not isinstance(body, dict):
        return None
    err = body.get("error")
    if isinstance(err, dict):
        return err.get("code")
    return None


def _error_text(result: Any) -> str:
    parts = [str(getattr(result, "detail", "") or "")]
    body = getattr(result, "body", None)
    if isinstance(body, dict):
        err = body.get("error")
        if isinstance(err, dict):
            parts.append(str(err.get("message") or ""))
        elif isinstance(err, str):
            parts.append(err)
        message = body.get("message")
        if isinstance(message, str):
            parts.append(message)
    elif isinstance(body, str):
        parts.append(body)
    return " ".join(parts).lower()
