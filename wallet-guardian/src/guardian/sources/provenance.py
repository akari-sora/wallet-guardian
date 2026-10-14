"""Per-provider logical method counts for one run.

An HTTP batch counts every method inside it. Credits follow that count.
Blockscout basic calls are about 20 credits each, not one request per credit
grant. Remaining quota is recorded only when the provider sends it.
"""

from __future__ import annotations

import re
from collections import Counter
from typing import Any, Mapping

from guardian.sources.alchemy import CU_ESTIMATE
from guardian.sources.resolver import egress_evidence

_REMAINING = ("x-credits-remaining", "x-ratelimit-remaining", "x-ratelimit-remaining-quota")
_RATE_HEADERS = (
    "x-credits-remaining",
    "x-ratelimit-limit",
    "x-ratelimit-remaining",
    "x-ratelimit-remaining-quota",
)
_BLOCKSCOUT_CREDITS = 20
_PATH_ADDRESS = re.compile(r"/(address|addresses|scripthash)/[^/]+")
_TRON_ACCOUNT = re.compile(r"/v1/accounts/[^/]+")


class CallLedger:
    def __init__(self) -> None:
        self.providers: dict[str, dict[str, Any]] = {}
        self.backoff: list[dict[str, Any]] = []
        self.egress = egress_evidence(
            resolver_verified=False,
            tls_hostname_verified=False,
            destination_policy_enforced=False,
            enforcement_location="unproven",
        )

    def note_backoff(self, source_id: str, seconds: float) -> None:
        self.backoff.append({"seconds": seconds, "source_id": source_id})

    def observe(self, source: Any, operation: Any, result: Any) -> None:
        source_id = str(getattr(source, "source_id", "") or "")
        adapter = str(getattr(source, "adapter", "") or getattr(operation, "adapter", "") or "")
        each, basis = _credit_rule(adapter)
        entry = self.providers.setdefault(source_id, {
            "adapter": adapter,
            "credits": 0,
            "credits_basis": basis,
            "http_calls": 0,
            "logical_methods": 0,
            "methods": Counter(),
            "pages": 0,
            "rate_headers": {},
            "remaining_quota": None,
        })
        methods = _methods(operation)
        entry["http_calls"] += 1
        entry["logical_methods"] += len(methods)
        entry["credits"] += each * len(methods)
        for name in methods:
            entry["methods"][name] += 1
        if _is_page(operation):
            entry["pages"] += 1
        observed = getattr(result, "egress", None)
        if isinstance(observed, dict):
            self.egress = egress_evidence(
                resolver_verified=bool(observed.get("resolver_verified")),
                tls_hostname_verified=bool(observed.get("tls_hostname_verified")),
                destination_policy_enforced=bool(observed.get("destination_policy_enforced")),
                enforcement_location=str(observed.get("enforcement_location") or "unproven"),
            )
        headers = getattr(result, "headers", None) or {}
        if isinstance(headers, Mapping):
            for key in _REMAINING:
                if key in headers and headers[key] not in (None, ""):
                    entry["remaining_quota"] = str(headers[key])
            for key in _RATE_HEADERS:
                if key in headers and headers[key] not in (None, ""):
                    entry["rate_headers"][key] = str(headers[key])

    def as_dict(self) -> dict[str, Any]:
        providers = {}
        for source_id in sorted(self.providers):
            entry = self.providers[source_id]
            providers[source_id] = {
                "adapter": entry["adapter"],
                "credits": entry["credits"],
                "credits_basis": entry.get("credits_basis", "count"),
                "http_calls": entry["http_calls"],
                "logical_methods": entry["logical_methods"],
                "methods": dict(sorted(entry["methods"].items())),
                "pages": entry["pages"],
                "rate_headers": dict(sorted(entry["rate_headers"].items())),
                "remaining_quota": entry["remaining_quota"],
            }
        return {
            "backoff": list(self.backoff),
            "egress": dict(self.egress),
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
        public = _TRON_ACCOUNT.sub("/v1/accounts/{address}", _PATH_ADDRESS.sub(r"/\1/{address}", path))
        return [f"{public}:{action}" if action else public]
    return ["request"]


def _credit_rule(adapter: str) -> tuple[int, str]:
    """Blockscout credits and Alchemy compute units are published estimates."""
    if adapter == "alchemy_transfers":
        return CU_ESTIMATE, "estimate"
    if adapter == "blockscout":
        return _BLOCKSCOUT_CREDITS, "estimate"
    return 1, "count"


def _is_page(operation: Any) -> bool:
    method = getattr(operation, "method", None)
    if method in {"alchemy_getAssetTransfers", "nr_getAssetTransfers"}:
        return True
    query = getattr(operation, "query", None)
    if not isinstance(query, Mapping):
        return False
    return "page" in query or "pageKey" in query
