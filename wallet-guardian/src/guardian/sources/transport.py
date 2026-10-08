"""Read-only transport. Policy, endpoint, SSRF, and budget all run before the backend.

Redirects are not followed by the HTTP client. A hop is issued only after the
next URL is checked again and still names the registered host. Task data,
response metadata, and a URL that was not in the private profile cannot change
the endpoint. Secrets are not copied into exception text.
"""

from __future__ import annotations

import ipaddress
import socket
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit

from guardian.errors import ConfigError, GuardianError, SecurityError
from guardian.security.ssrf import validate_url
from guardian.security.transport_policy import Operation, assert_operation_allowed
from guardian.sources.budget import BudgetBook, BudgetGroup
from guardian.sources.fixtures import FixtureMiss, PreparedCall, RawResponse, canonical_body
from guardian.sources.retry import Classification, classify_response

_REDIRECTS = frozenset({301, 302, 303, 307, 308})
_MAX_HOPS = 3
_URL_KEYS = frozenset({"endpoint", "endpoint_url", "rpc", "rpc_url", "url"})
_METADATA_HOSTS = frozenset({"metadata", "metadata.google.internal"})
_METADATA_IP = ipaddress.ip_address("169.254.169.254")
_SSRF_REASON = {
    "dns answer points at a blocked address": "private_endpoint",
    "dns resolution failed": "dns_failed",
    "dns returned no addresses": "dns_failed",
    "local host is not allowed": "private_endpoint",
    "private or non-global ip is not allowed": "private_endpoint",
    "url is missing a host": "url_host",
    "url scheme is not allowed": "url_scheme",
}


class TransportError(GuardianError):
    def __init__(self, reason: str, *, retryable: bool = False) -> None:
        self.reason = reason
        self.retryable = retryable
        super().__init__(reason)


@dataclass(frozen=True)
class RegisteredSource:
    source_id: str
    adapter: str
    endpoint: str
    budget_group: str
    allow_private_network: bool


@dataclass
class EndpointRegistry:
    sources: dict[str, RegisteredSource]

    def get(self, source_id: str) -> RegisteredSource:
        try:
            return self.sources[source_id]
        except KeyError:
            raise SecurityError("unregistered_source", reason="unregistered_source") from None


@dataclass
class TransportResult:
    source_id: str
    status: int
    body: Any
    category: str
    quality: str
    retryable: bool
    blocked: bool
    reason: str
    headers: dict[str, str]

    @property
    def ok(self) -> bool:
        return self.category in {"empty", "ok", "partial"}

    @property
    def classification(self) -> Classification:
        return Classification(self.category, self.quality, self.retryable, self.blocked, self.reason)


class HttpxBackend:
    """Production backend. Redirects stay off. gRPC is not spoken here."""

    def __init__(self, *, timeout: float = 20, max_response_bytes: int = 1_048_576, client: Any = None) -> None:
        self.timeout = timeout
        self.max_response_bytes = max_response_bytes
        self._client = client

    def send(self, call: PreparedCall) -> RawResponse:
        if call.protocol == "grpc":
            raise TransportError("grpc_client_not_enabled")
        import httpx

        owns = self._client is None
        client = self._client or httpx.Client(timeout=self.timeout, follow_redirects=False)
        try:
            with client.stream(
                call.method,
                call.url,
                content=call.body or None,
                headers={"accept": "application/json"},
            ) as response:
                buf = bytearray()
                for chunk in response.iter_bytes():
                    buf.extend(chunk)
                    if len(buf) > self.max_response_bytes:
                        raise TransportError("response_too_large")
                return RawResponse(response.status_code, {key: value for key, value in response.headers.items()}, bytes(buf))
        except TransportError:
            raise
        except httpx.TimeoutException:
            raise TransportError("timeout", retryable=True) from None
        except httpx.HTTPError:
            raise TransportError("connection_error", retryable=True) from None
        finally:
            if owns:
                client.close()


class ReadOnlyTransport:
    def __init__(
        self,
        endpoints: EndpointRegistry,
        budgets: BudgetBook,
        backend: Any,
        *,
        resolve=None,
        max_response_bytes: int = 1_048_576,
        max_hops: int = _MAX_HOPS,
    ) -> None:
        self.endpoints = endpoints
        self.budgets = budgets
        self.backend = backend
        self.resolve = resolve
        self.max_response_bytes = max_response_bytes
        self.max_hops = max_hops
        if hasattr(backend, "max_response_bytes"):
            backend.max_response_bytes = max_response_bytes

    def retry_attempts(self, source_id: str) -> int:
        return self._group(source_id).spec.retry_attempts

    def encode(self, source_id: str, operation: Operation) -> PreparedCall:
        source = self._source(source_id)
        url = _request_target(source.endpoint, operation)
        return _encode(source_id, operation, url)

    def request(
        self,
        source_id: str,
        operation: Operation,
        *,
        priority: str = "normal",
        credits: int = 1,
        task: Any = None,
        metadata: Any = None,
        response_metadata: Any = None,
    ) -> TransportResult:
        assert_operation_allowed(operation)
        _reject_untrusted(task, metadata, response_metadata)
        source = self._source(source_id)
        if operation.adapter != source.adapter:
            raise SecurityError("adapter_mismatch", reason="adapter_mismatch")
        if not source.endpoint:
            raise SecurityError("endpoint_missing", reason="endpoint_missing")
        current = _request_target(source.endpoint, operation)
        registered = _host_key(source.endpoint)
        if _host_key(current) != registered:
            raise SecurityError("endpoint_mismatch", reason="endpoint_mismatch")
        hops = 0
        while True:
            _guard_url(current, allow_private=source.allow_private_network, resolve=self.resolve)
            if _host_key(current) != registered:
                raise SecurityError("redirect_host", reason="redirect_host")
            call = _encode(source_id, operation, current)
            raw = self._send(source, call, priority=priority, credits=credits)
            if len(raw.body) > self.max_response_bytes:
                raise TransportError("response_too_large")
            if raw.status in _REDIRECTS:
                hops += 1
                if hops > self.max_hops:
                    raise SecurityError("too_many_redirects", reason="too_many_redirects")
                location = _header(raw.headers, "location")
                if not location:
                    raise SecurityError("redirect_missing", reason="redirect_missing")
                target = urljoin(current, location)
                if urlsplit(target).username or urlsplit(target).password:
                    raise SecurityError("url_userinfo", reason="url_userinfo")
                if _host_key(target) != registered:
                    raise SecurityError("redirect_host", reason="redirect_host")
                current = target
                continue
            return _finish(source_id, operation.protocol, raw, self.max_response_bytes)

    def _source(self, source_id: str) -> RegisteredSource:
        return self.endpoints.get(source_id)

    def _group(self, source_id: str) -> BudgetGroup:
        source = self._source(source_id)
        try:
            return self.budgets.group(source.budget_group)
        except ConfigError:
            raise SecurityError("unknown_budget", reason="unknown_budget") from None

    def _send(self, source: RegisteredSource, call: PreparedCall, *, priority: str, credits: int) -> RawResponse:
        lease = self._group(source.source_id).acquire(priority=priority, credits=credits)
        try:
            try:
                return self.backend.send(call)
            except TimeoutError:
                raise TransportError("timeout", retryable=True) from None
            except ConnectionError:
                raise TransportError("connection_error", retryable=True) from None
            except FixtureMiss:
                raise TransportError("fixture_miss") from None
        finally:
            lease.release()


def build_endpoint_registry(
    public_document: Mapping[str, Any],
    *,
    secrets: Mapping[str, str] | None = None,
    environ: Mapping[str, str] | None = None,
    local_profiles: Mapping[str, Mapping[str, Any]] | None = None,
) -> EndpointRegistry:
    """Resolve endpoints from private env values. The local-network flag is not read from public config."""
    assert_no_public_local_flag(public_document)
    secrets = secrets or {}
    environ = environ or {}
    local_profiles = local_profiles or {}
    sources = public_document.get("sources")
    if not isinstance(sources, Mapping):
        raise ConfigError("public config is missing")
    for source_id in local_profiles:
        if source_id not in sources:
            raise SecurityError("unregistered_source", reason="unregistered_source")
    registered: dict[str, RegisteredSource] = {}
    for source_id, source in sources.items():
        if not isinstance(source, Mapping):
            raise ConfigError("public config is missing")
        env_name = source.get("endpoint_env")
        if not isinstance(env_name, str):
            raise ConfigError("endpoint_env must be an env var name")
        registered[source_id] = RegisteredSource(
            source_id=source_id,
            adapter=str(source.get("adapter", "")),
            endpoint=_lookup_endpoint(env_name, secrets, environ),
            budget_group=str(source.get("budget_group", "")),
            allow_private_network=_private_flag(local_profiles.get(source_id)),
        )
    return EndpointRegistry(registered)


def assert_no_public_local_flag(document: Mapping[str, Any]) -> None:
    if _contains_key(document, "allow_private_network"):
        raise SecurityError("public_local_profile", reason="public_local_profile")


def _lookup_endpoint(env_name: str, secrets: Mapping[str, str], environ: Mapping[str, str]) -> str:
    for pool in (secrets, environ):
        value = pool.get(env_name)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _private_flag(profile: Any) -> bool:
    return isinstance(profile, Mapping) and profile.get("allow_private_network") is True


def _request_target(endpoint: str, operation: Operation) -> str:
    parts = urlsplit(endpoint)
    if parts.username or parts.password:
        raise SecurityError("url_userinfo", reason="url_userinfo")
    if parts.scheme not in {"http", "https"} or not parts.hostname:
        raise SecurityError("endpoint_missing", reason="endpoint_missing")
    if operation.protocol in {"graphql", "grpc", "jsonrpc"}:
        target = endpoint
    else:
        path = operation.path if isinstance(operation.path, str) else "/"
        if len(path) > 1 and path.endswith("/"):
            path = path[:-1]
        origin = f"{parts.scheme}://{parts.netloc}"
        merged = dict(parse_qsl(parts.query, keep_blank_values=True))
        for key, value in (operation.query or {}).items():
            merged[str(key)] = str(value)
        target = origin + path
        if merged:
            target += "?" + urlencode(sorted(merged.items()))
    if operation.url is not None and operation.url != target:
        raise SecurityError("endpoint_mismatch", reason="endpoint_mismatch")
    return target


def _encode(source_id: str, operation: Operation, url: str) -> PreparedCall:
    if operation.protocol == "jsonrpc":
        return PreparedCall(source_id, "POST", url, _jsonrpc_body(operation), "jsonrpc")
    if operation.protocol == "graphql":
        return PreparedCall(source_id, "POST", url, _graphql_body(operation), "graphql")
    if operation.protocol == "grpc":
        return PreparedCall(source_id, "GRPC", url, canonical_body({"method": operation.method}), "grpc")
    method = operation.http_method if isinstance(operation.http_method, str) else "GET"
    body = b"" if operation.body is None else canonical_body(operation.body)
    return PreparedCall(source_id, method, url, body, "rest")


def _jsonrpc_body(operation: Operation) -> bytes:
    if operation.batch is not None:
        payload = []
        for index, item in enumerate(operation.batch, start=1):
            entry: dict[str, Any] = {"id": index, "jsonrpc": "2.0", "method": item["method"]}
            if "params" in item:
                entry["params"] = item["params"]
            payload.append(entry)
        return canonical_body(payload)
    payload: dict[str, Any] = {"id": 1, "jsonrpc": "2.0", "method": operation.method}
    if operation.params is not None:
        payload["params"] = operation.params
    return canonical_body(payload)


def _graphql_body(operation: Operation) -> bytes:
    if operation.documents is not None:
        return canonical_body(list(operation.documents))
    payload: dict[str, Any] = {"query": operation.document}
    if operation.operation_name:
        payload["operationName"] = operation.operation_name
    if operation.body is not None:
        payload["variables"] = operation.body
    return canonical_body(payload)


def _finish(source_id: str, protocol: str, raw: RawResponse, max_bytes: int) -> TransportResult:
    if len(raw.body) > max_bytes:
        raise TransportError("response_too_large")
    headers = {str(key).lower(): str(value) for key, value in raw.headers.items()}
    if raw.status >= 400 or raw.status < 200:
        classification = classify_response(raw.status, None)
        return _result(source_id, raw.status, None, classification, headers)
    try:
        parsed = _parse_body(protocol, raw.body)
    except TransportError:
        raise
    classification = classify_response(raw.status, parsed)
    body = parsed if classification.category in {"empty", "error", "ok", "partial"} else None
    return _result(source_id, raw.status, body, classification, headers)


def _result(source_id: str, status: int, body: Any, classification: Classification, headers: dict[str, str]) -> TransportResult:
    return TransportResult(
        source_id=source_id,
        status=status,
        body=body,
        category=classification.category,
        quality=classification.quality,
        retryable=classification.retryable,
        blocked=classification.blocked,
        reason=classification.reason,
        headers=headers,
    )


def _parse_body(protocol: str, body: bytes) -> Any:
    if not body:
        raise TransportError("bad_json")
    try:
        text = body.decode("utf-8")
    except UnicodeError:
        raise TransportError("bad_json") from None
    stripped = text.lstrip()
    if protocol in {"graphql", "grpc", "jsonrpc"} or stripped.startswith("{") or stripped.startswith("["):
        import json

        try:
            return json.loads(text)
        except json.JSONDecodeError:
            raise TransportError("bad_json") from None
    if protocol == "rest":
        return text
    raise TransportError("bad_json")


def _guard_url(url: str, *, allow_private: bool, resolve) -> None:
    if allow_private:
        _validate_relaxed(url, resolve=resolve)
        return
    try:
        validate_url(url, resolve=resolve)
    except SecurityError as exc:
        reason = _SSRF_REASON.get(str(exc), "endpoint_rejected")
        raise SecurityError(reason, reason=reason) from None


def _validate_relaxed(url: str, *, resolve) -> None:
    parts = urlsplit(url)
    if parts.scheme not in {"http", "https"}:
        raise SecurityError("url_scheme", reason="url_scheme")
    if parts.username or parts.password:
        raise SecurityError("url_userinfo", reason="url_userinfo")
    host = parts.hostname
    if not host:
        raise SecurityError("url_host", reason="url_host")
    lowered = host.lower().rstrip(".")
    if lowered in _METADATA_HOSTS:
        raise SecurityError("metadata_endpoint", reason="metadata_endpoint")
    try:
        literal = ipaddress.ip_address(lowered)
    except ValueError:
        literal = None
    if literal is not None:
        if not _acceptable(literal):
            raise SecurityError("private_endpoint", reason="private_endpoint")
        return
    resolver = resolve or _default_resolve
    try:
        addresses = resolver(lowered)
    except OSError:
        raise SecurityError("dns_failed", reason="dns_failed") from None
    if not addresses:
        raise SecurityError("dns_failed", reason="dns_failed")
    for addr in addresses:
        try:
            ip = ipaddress.ip_address(addr)
        except ValueError:
            raise SecurityError("dns_failed", reason="dns_failed") from None
        if not _acceptable(ip):
            raise SecurityError("private_endpoint", reason="private_endpoint")


def _acceptable(ip: ipaddress._BaseAddress) -> bool:
    if ip == _METADATA_IP or ip.is_multicast or ip.is_unspecified:
        return False
    if ip.is_global or ip.is_private or ip.is_loopback or ip.is_link_local:
        return True
    return False


def _default_resolve(host: str) -> list[str]:
    infos = socket.getaddrinfo(host, None)
    return [item[4][0] for item in infos]


def _host_key(url: str) -> tuple[str, str, int | None]:
    parts = urlsplit(url)
    host = (parts.hostname or "").lower().rstrip(".")
    return (parts.scheme.lower(), host, parts.port)


def _header(headers: Mapping[str, str], name: str) -> str:
    for key, value in headers.items():
        if str(key).lower() == name:
            return str(value)
    return ""


def _reject_untrusted(*payloads: Any) -> None:
    for payload in payloads:
        if payload is not None and _has_endpoint(payload):
            raise SecurityError("untrusted_endpoint", reason="untrusted_endpoint")


def _has_endpoint(value: Any) -> bool:
    if isinstance(value, Mapping):
        for key, item in value.items():
            if isinstance(key, str) and key.lower().replace("-", "_") in _URL_KEYS:
                return True
            if _has_endpoint(item):
                return True
        return False
    if isinstance(value, list):
        return any(_has_endpoint(item) for item in value)
    if isinstance(value, str) and (value.startswith("http://") or value.startswith("https://")):
        return True
    return False


def _contains_key(value: Any, key: str) -> bool:
    if isinstance(value, Mapping):
        if key in value:
            return True
        return any(_contains_key(item, key) for item in value.values())
    if isinstance(value, list):
        return any(_contains_key(item, key) for item in value)
    return False

