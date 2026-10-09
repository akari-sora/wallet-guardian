"""Per-provider logical method counts for one run.

An HTTP batch counts every method inside it. Credits follow that count.
Blockscout basic calls are about 20 credits each, not one request per credit
grant. Remaining quota is recorded only when the provider sends it.
"""

from __future__ import annotations

from collections import Counter
from typing import Any, Mapping

_REMAINING = ("x-credits-remaining", "x-ratelimit-remaining", "x-ratelimit-remaining-quota")
_BLOCKSCOUT_CREDITS = 20


class CallLedger:
    def __init__(self) -> None:
        self.providers: dict[str, dict[str, Any]] = {}
        self.backoff: list[dict[str, Any]] = []

    def note_backoff(self, source_id: str, seconds: float) -> None:
        self.backoff.append({"seconds": seconds, "source_id": source_id})

    def observe(self, source: Any, operation: Any, result: Any) -> None:
        source_id = str(getattr(source, "source_id", "") or "")
        adapter = str(getattr(source, "adapter", "") or getattr(operation, "adapter", "") or "")
        entry = self.providers.setdefault(source_id, {
            "adapter": adapter,
            "credits": 0,
            "http_calls": 0,
            "logical_methods": 0,
            "methods": Counter(),
            "pages": 0,
            "remaining_quota": None,
        })
        methods = _methods(operation)
        each = _BLOCKSCOUT_CREDITS if adapter == "blockscout" else 1
        entry["http_calls"] += 1
        entry["logical_methods"] += len(methods)
        entry["credits"] += each * len(methods)
        for name in methods:
            entry["methods"][name] += 1
        if _is_page(operation):
            entry["pages"] += 1
        headers = getattr(result, "headers", None) or {}
        if isinstance(headers, Mapping):
            for key in _REMAINING:
                if key in headers and headers[key] not in (None, ""):
                    entry["remaining_quota"] = str(headers[key])

    def as_dict(self) -> dict[str, Any]:
        providers = {}
        for source_id in sorted(self.providers):
            entry = self.providers[source_id]
            providers[source_id] = {
                "adapter": entry["adapter"],
                "credits": entry["credits"],
                "http_calls": entry["http_calls"],
                "logical_methods": entry["logical_methods"],
                "methods": dict(sorted(entry["methods"].items())),
                "pages": entry["pages"],
                "remaining_quota": entry["remaining_quota"],
            }
        return {
            "backoff": list(self.backoff),
            "providers": providers,
        }


def _methods(operation: Any) -> list[str]:
    batch = getattr(operation, "batch", None)
    if batch:
        found = []
        for item in batch:
            if isinstance(item, Mapping) and isinstance(item.get("method"), str):
                found.append(item["method"])
        return found or ["batch"]
    method = getattr(operation, "method", None)
    if isinstance(method, str) and method:
        return [method]
    path = getattr(operation, "path", None)
    if isinstance(path, str) and path:
        action = ""
        query = getattr(operation, "query", None)
        if isinstance(query, Mapping) and isinstance(query.get("action"), str):
            action = query["action"]
        return [f"{path}:{action}" if action else path]
    return ["request"]


def _is_page(operation: Any) -> bool:
    method = getattr(operation, "method", None)
    if method == "nr_getAssetTransfers":
        return True
    query = getattr(operation, "query", None)
    if not isinstance(query, Mapping):
        return False
    return "page" in query or "pageKey" in query
