"""Per-capability source order. A fallback that lacks the capability is not a success.

Two endpoints with the same backend operator count as one independent source.
Failover may still use the next endpoint. It does not create a second vote.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from guardian.errors import SecurityError
from guardian.sources.budget import BudgetExhausted


@dataclass
class CapabilityResult:
    capability: str
    source_id: str | None
    quality: str
    degraded: bool
    reason_codes: tuple[str, ...]
    source_ids_tried: tuple[str, ...]
    body: Any = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "capability": self.capability,
            "degraded": self.degraded,
            "quality": self.quality,
            "reason_codes": list(self.reason_codes),
            "source_id": self.source_id,
            "source_ids_tried": list(self.source_ids_tried),
        }


class SourceRouter:
    def __init__(
        self,
        routing: Sequence[Mapping[str, Any]],
        sources: Mapping[str, str],
        catalog: Any,
        send: Callable[[str, Any], Any],
    ) -> None:
        self.routing = list(routing)
        self.sources = dict(sources)
        self.catalog = catalog
        self.send = send

    def fetch(self, network: str, capability: str, operation_for: Callable[[str], Any]) -> CapabilityResult:
        route = _route_for(self.routing, network, capability)
        tried: list[str] = []
        reasons: list[str] = []
        saw_budget = False
        saw_failure = False
        for index, source_id in enumerate(route):
            tried.append(source_id)
            adapter = self.sources.get(source_id)
            if adapter is None or not self.catalog.declares(adapter, network, capability):
                reasons.append("fallback_lacks_capability" if index else "source_lacks_capability")
                continue
            try:
                result = self.send(source_id, operation_for(source_id))
            except BudgetExhausted:
                saw_budget = True
                reasons.append("budget")
                continue
            except SecurityError:
                raise
            except Exception as exc:
                saw_failure = True
                reasons.append(str(getattr(exc, "reason", "error")))
                continue
            if not getattr(result, "ok", False):
                saw_failure = True
                reasons.append(str(getattr(result, "reason", "error")))
                continue
            return CapabilityResult(
                capability=capability,
                source_id=source_id,
                quality=result.quality,
                degraded=index > 0,
                reason_codes=tuple(reasons) if index else (),
                source_ids_tried=tuple(tried),
                body=result.body,
            )
        if saw_budget and not saw_failure and not any(code.endswith("lacks_capability") for code in reasons):
            raise BudgetExhausted("budget")
        quality = "unknown" if any(code.endswith("lacks_capability") for code in reasons) else "error"
        if saw_failure and not any(code.endswith("lacks_capability") for code in reasons):
            quality = "error"
        return CapabilityResult(capability, None, quality, True, tuple(reasons), tuple(tried), None)


def independent_source_count(source_ids: Sequence[str], backends: Mapping[str, str]) -> int:
    """Count distinct backend operators. Domain names do not add a vote."""
    seen: list[str] = []
    for source_id in source_ids:
        backend = backends[source_id]
        if backend not in seen:
            seen.append(backend)
    return len(seen)


def require_independent_quorum(source_ids: Sequence[str], backends: Mapping[str, str], *, minimum: int) -> int:
    count = independent_source_count(source_ids, backends)
    if minimum < 1 or count < minimum:
        raise SecurityError("quorum_not_independent", reason="quorum_not_independent")
    return count


def _route_for(routing: Sequence[Mapping[str, Any]], network: str, capability: str) -> list[str]:
    for route in routing:
        if route.get("network") == network and route.get("capability") == capability:
            return list(route.get("source_ids") or [])
    return []
