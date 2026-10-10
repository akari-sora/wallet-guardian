"""Bounded historical reads. ``latest`` is never sent."""

from __future__ import annotations

import json
from typing import Any

from guardian.chains.evm.adapter import evm_rpc
from guardian.explanations.models import CALL_LIMIT, TIME_LIMIT_SECONDS

_PRUNED = ("pruned", "history has been pruned", "log response size exceeded")
_HISTORICAL = ("historical state", "missing trie node", "archive")


class ExplanationRpc:
    """One event's provider budget. The round budget is shared and is not extended."""

    def __init__(self, transport: Any, source_id: str, clock: Any, round_budget: Any | None) -> None:
        self.transport = transport
        self.source_id = source_id
        self.clock = clock
        self.round_budget = round_budget
        self.started = float(clock())
        self.calls = 0
        self.stopped: str | None = None
        self.seen: list[dict[str, Any]] = []

    def historical(self, block_hash: str) -> dict[str, Any]:
        return {"blockHash": block_hash, "requireCanonical": True}

    def call(self, method: str, params: list[Any]) -> tuple[Any, str | None]:
        if self._blocked(method, params):
            return None, self.stopped or "deadline"
        self.calls += 1
        self.seen.append({"method": method, "params": params})
        try:
            result = self.transport.request(self.source_id, evm_rpc(method, params), priority="low")
        except Exception as exc:
            reason = getattr(exc, "reason", None)
            if reason == "rate_limited":
                self.stopped = "rate_limited"
                return None, "rate_limited"
            if reason == "timeout":
                self.stopped = "deadline"
                return None, "deadline"
            return None, "read_error"
        status = getattr(result, "status", 0)
        if status == 429 or getattr(result, "category", "") == "rate_limited":
            self.stopped = "rate_limited"
            return None, "rate_limited"
        body = getattr(result, "body", None)
        if not isinstance(body, dict):
            return None, "read_error"
        if "error" in body and body.get("error") is not None and "result" in body and body.get("result") is not None:
            return None, "malformed_response"
        if body.get("error") is not None and body.get("result") is None:
            return None, _error_code(body.get("error"))
        if getattr(result, "ok", True) is False:
            return None, _error_code(body.get("error")) if body.get("error") else "read_error"
        return body.get("result"), None

    def _blocked(self, method: str, params: list[Any]) -> bool:
        if _contains_latest(params):
            self.stopped = "latest_forbidden"
            return True
        if self.stopped in {"deadline", "provider_call_budget", "rate_limited", "latest_forbidden"}:
            return True
        if self.calls >= CALL_LIMIT:
            self.stopped = "provider_call_budget"
            return True
        if float(self.clock()) - self.started >= TIME_LIMIT_SECONDS:
            self.stopped = "deadline"
            return True
        budget = self.round_budget
        if budget is not None and getattr(budget, "expired", None) and budget.expired():
            self.stopped = "deadline"
            return True
        del method
        return False


def _contains_latest(value: Any) -> bool:
    if isinstance(value, str):
        return value == "latest"
    if isinstance(value, dict):
        return any(_contains_latest(item) for item in value.values())
    if isinstance(value, (list, tuple)):
        return any(_contains_latest(item) for item in value)
    return False


def _error_code(error: Any) -> str:
    message = ""
    if isinstance(error, dict):
        message = str(error.get("message") or "").lower()
    elif isinstance(error, str):
        message = error.lower()
    if any(token in message for token in _PRUNED):
        return "pruned_logs"
    if any(token in message for token in _HISTORICAL):
        return "historical_state_unavailable"
    if "rate" in message or "429" in message:
        return "rate_limited"
    try:
        json.dumps(error)
    except TypeError:
        return "read_error"
    return "read_error"
