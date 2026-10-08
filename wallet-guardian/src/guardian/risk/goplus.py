"""GoPlus reads. Support is per function and chain. A miss is unknown, never safe.

The API key would have to travel in an Authorization header. This transport
cannot carry headers, so a configured key is reported and not sent.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from guardian.errors import SecurityError
from guardian.identities import IdentityError, parse_address
from guardian.pricing.catalog import GoPlusFunction, RiskSources
from guardian.security.redact import redact
from guardian.security.transport_policy import Operation
from guardian.sources.budget import BudgetExhausted
from guardian.sources.etherscan import resolve_credential
from guardian.sources.retry import read_with_retry
from guardian.sources.transport import TransportError

_STATUSES = frozenset({"flagged", "not_flagged", "unknown", "unsupported"})


@dataclass(frozen=True)
class FunctionRead:
    """One function on one chain. ``safe`` cannot be true."""

    function: str
    network_id: str
    target: str
    status: str
    reason: str
    flags: tuple[tuple[str, str], ...]
    credential_status: str
    requested: bool
    safe: bool = False

    def __post_init__(self) -> None:
        if self.safe or self.status in {"safe", "clean"}:
            raise ValueError("goplus_cannot_be_safe")
        if self.status not in _STATUSES:
            raise ValueError("status")
        if self.status == "unsupported" and self.requested:
            raise ValueError("unsupported_requested")
        object.__setattr__(self, "flags", tuple(self.flags))


class GoPlusClient:
    """Keyless GET for the functions listed in the catalog."""

    def __init__(
        self,
        transport: Any,
        source_id: str,
        catalog: RiskSources,
        environ: Mapping[str, str],
        *,
        clock: Callable[[], float] | None = None,
        sleep: Callable[[float], None] | None = None,
        random: Callable[[], float] | None = None,
    ) -> None:
        self.transport = transport
        self.source_id = source_id
        self.catalog = catalog
        self.credential_env = catalog.goplus_credential_env
        try:
            self._credential: str | None = resolve_credential(environ, self.credential_env)
        except ValueError as exc:
            if str(exc) != "credential_missing":
                raise
            self._credential = None
        self.credential_status = "credential_transport_unsupported" if self._credential else "keyless"
        self.clock = clock or (lambda: 0.0)
        self.sleep = sleep or (lambda _seconds: None)
        self.random = random or (lambda: 0.0)

    def __repr__(self) -> str:
        return (
            f"GoPlusClient(source_id={self.source_id!r}, credential_env={self.credential_env!r}, "
            f"credential={self.credential_status})"
        )

    def redact_error(self, text: str) -> str:
        secrets = [self._credential] if self._credential else None
        return redact(text, secrets)

    def operation(self, *, function: str, network_id: str, target: str) -> Operation | None:
        """None when this function/chain pair is not in the catalog, or the target is invalid."""
        spec = self.catalog.goplus_function(function)
        if spec is None:
            return None
        chain_id = spec.chain_id(network_id)
        if chain_id is None:
            return None
        normalized = _normalize(self.catalog, network_id, chain_id, target)
        if normalized is None:
            return None
        values = {"address": normalized, "chain_id": chain_id}
        try:
            path = spec.path.format(**values)
            query = {key: template.format(**values) for key, template in spec.query}
        except (IndexError, KeyError, ValueError):
            return None
        if "{" in path or any(token in path for token in ("..", "?", "#", "%")):
            return None
        if any("{" in value or not isinstance(value, str) for value in query.values()):
            return None
        return Operation(
            protocol="rest",
            adapter="goplus",
            http_method="GET",
            path=path,
            query=query,
        )

    def read(self, *, function: str, network_id: str, target: str) -> FunctionRead:
        spec = self.catalog.goplus_function(function)
        chain_id = None if spec is None else spec.chain_id(network_id)
        if spec is None or chain_id is None:
            return _read(
                function, network_id, target if isinstance(target, str) else "",
                "unsupported", "unsupported", (), self.credential_status, False,
            )
        normalized = _normalize(self.catalog, network_id, chain_id, target)
        if normalized is None:
            return _read(function, network_id, target, "unknown", "bad_identity", (), self.credential_status, False)
        operation = self.operation(function=function, network_id=network_id, target=normalized)
        if operation is None:
            return _read(function, network_id, normalized, "unknown", "bad_identity", (), self.credential_status, False)
        try:
            result = read_with_retry(
                lambda: self.transport.request(self.source_id, operation),
                retry_attempts=self.transport.retry_attempts(self.source_id),
                sleep=self.sleep,
                random=self.random,
                clock=self.clock,
            )
        except BudgetExhausted as exc:
            return _read(function, network_id, normalized, "unknown", exc.reason or "budget", (), self.credential_status, True)
        except SecurityError as exc:
            return _read(function, network_id, normalized, "unknown", exc.reason or "blocked", (), self.credential_status, True)
        except TransportError as exc:
            if exc.reason == "fixture_miss":
                raise
            return _read(function, network_id, normalized, "unknown", exc.reason or "read_error", (), self.credential_status, True)
        if getattr(result, "blocked", False) or getattr(result, "status", 0) in {401, 403}:
            return _read(function, network_id, normalized, "unknown", str(result.reason or "blocked"), (), self.credential_status, True)
        if result.category == "rate_limited" or getattr(result, "status", 0) == 429:
            return _read(function, network_id, normalized, "unknown", "rate_limited", (), self.credential_status, True)
        if result.category != "ok" or not isinstance(result.body, dict):
            return _read(function, network_id, normalized, "unknown", "malformed", (), self.credential_status, True)
        status, reason, flags = _interpret(result.body, spec, normalized, _family(self.catalog, network_id, chain_id))
        return _read(function, network_id, normalized, status, reason, flags, self.credential_status, True)


def _interpret(body: Mapping[str, Any], spec: GoPlusFunction, target: str, family: str) -> tuple[str, str, tuple[tuple[str, str], ...]]:
    code = body.get("code")
    if isinstance(code, bool) or not isinstance(code, int) or code != 1:
        return "unknown", "code", ()
    result = body.get("result")
    if not isinstance(result, dict) or not result:
        return "unknown", "missing_result", ()
    entry = _select(result, spec.result, target, family)
    if not isinstance(entry, dict):
        return "unknown", "missing_result", ()
    flags, non_string = _flags(entry)
    if any(state == "flagged" for state in flags.values()):
        return "flagged", "flagged", tuple(flags.items())
    if flags and not non_string:
        return "not_flagged", "not_flagged_by_this_function", tuple(flags.items())
    if non_string:
        return "unknown", "flag_not_string", ()
    return "unknown", "no_string_flags", ()


def _select(result: Mapping[str, Any], shape: str, target: str, family: str) -> dict[str, Any] | None:
    mapped = _all_objects(result)
    if shape == "keyed" or (shape == "keyed_or_flat" and mapped) or (shape == "flat" and mapped):
        return _keyed(result, target, family)
    if shape == "flat" or shape == "keyed_or_flat":
        return dict(result)
    return None


def _all_objects(result: Mapping[str, Any]) -> bool:
    return bool(result) and all(isinstance(value, dict) for value in result.values())


def _keyed(result: Mapping[str, Any], target: str, family: str) -> dict[str, Any] | None:
    for key, value in result.items():
        if not isinstance(key, str) or not isinstance(value, dict):
            continue
        if family == "evm" and key.lower() == target.lower():
            return value
        if family != "evm" and key == target:
            return value
    return None


def _flags(entry: Mapping[str, Any]) -> tuple[dict[str, str], bool]:
    flags: dict[str, str] = {}
    non_string = False
    for key, value in entry.items():
        if not isinstance(key, str):
            continue
        if value == "1":
            flags[key] = "flagged"
        elif value == "0":
            flags[key] = "not_flagged"
        elif isinstance(value, int) and not isinstance(value, bool) and value in {0, 1}:
            non_string = True
    return flags, non_string


def _normalize(catalog: RiskSources, network_id: str, chain_id: str, target: str) -> str | None:
    if not isinstance(target, str) or not target:
        return None
    family = _family(catalog, network_id, chain_id)
    try:
        parsed = parse_address(family, target)
    except IdentityError:
        return None
    if family == "evm":
        return parsed.identity_key
    return parsed.canonical


def _family(catalog: RiskSources, network_id: str, chain_id: str) -> str:
    found = catalog.family_for_network(network_id)
    if found:
        return found
    if chain_id == "solana":
        return "solana"
    if chain_id == "tron":
        return "tron"
    return "evm"


def _read(
    function: str,
    network_id: str,
    target: str,
    status: str,
    reason: str,
    flags: tuple[tuple[str, str], ...],
    credential_status: str,
    requested: bool,
) -> FunctionRead:
    return FunctionRead(
        function=function,
        network_id=network_id,
        target=target,
        status=status,
        reason=reason,
        flags=flags,
        credential_status=credential_status,
        requested=requested,
        safe=False,
    )
