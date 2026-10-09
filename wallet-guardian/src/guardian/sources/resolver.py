"""Hostname resolution for the read-only monitor.

Two modes exist. ``system`` is the default and keeps today's lookup path, then
fails closed when the answer is in the benchmarking range 198.18.0.0/15.
``doh`` queries only configured HTTPS resolvers, pinned to their bootstrap
addresses, and never sends a wallet address or an RPC URL.

``trust_env=False`` on the HTTP client is not evidence that the network honored
the destination IP. ``destination_policy_enforced`` stays false until a probe
shows that connecting to host A's address with host B's name does not return
B's certificate.
"""

from __future__ import annotations

import ipaddress
import json
import os
import re
import socket
import ssl
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlsplit, urlunsplit

from guardian.errors import ConfigError, SecurityError

_BENCHMARK_MESSAGE = "benchmark address in 198.18.0.0/15; switch resolver mode to doh"
_MODE_ENV = "GUARDIAN_RESOLVER_MODE"
_ENDPOINTS_ENV = "GUARDIAN_DOH_ENDPOINTS"
_BOOTSTRAP_ENV = "GUARDIAN_DOH_BOOTSTRAP"
_PROXY_NAMES = ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy")
_PROVEN_LOCATIONS = frozenset({"destination_ip", "last_hop", "platform"})
_LOCATIONS = _PROVEN_LOCATIONS | {"unproven", "sni_proxy"}
_REDIRECTS = frozenset({301, 302, 303, 307, 308})
_FALLBACK_REASONS = frozenset({"doh_timeout", "doh_connection", "doh_status"})
_QUERY_TYPES = frozenset({"A", "AAAA"})
_A = 1
_AAAA = 28
_CNAME = 5
_MAX_TTL = 7 * 24 * 60 * 60
_MAX_CNAME = 8
_MAX_RECORDS = 32
_BENCHMARK_V4 = ipaddress.ip_network("198.18.0.0/15")
_BENCHMARK_V6 = ipaddress.ip_network("2001:2::/48")
_CGNAT = ipaddress.ip_network("100.64.0.0/10")
_METADATA_V4 = ipaddress.ip_address("169.254.169.254")
_METADATA_V6 = ipaddress.ip_address("fd00:ec2::254")
_ENV_NAME = re.compile(r"^[A-Z][A-Z0-9_]{1,95}$")
_LABEL = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
_WALLET = re.compile(r"(?i)(?<![0-9a-f])0x[0-9a-f]{40}(?![0-9a-f])")
_FILE_KEYS = frozenset({"mode", "probe_name", "endpoints", "probe_hosts"})
_ENDPOINT_KEYS = frozenset({"url", "bootstrap_ips"})
_HOST_KEYS = frozenset({"hostname", "ip"})
_FORBIDDEN = frozenset({
    "address",
    "addresses",
    "apikey",
    "mnemonic",
    "privatekey",
    "secret",
    "seed",
    "token",
})


def destination_block_reason(value: str) -> str | None:
    """Why ``value`` must not be a connect target. None means global unicast."""
    ip = ipaddress.ip_address(str(value).strip("[]"))
    mapped = getattr(ip, "ipv4_mapped", None)
    if mapped is not None:
        return destination_block_reason(str(mapped))
    if ip.version == 4 and ip in _BENCHMARK_V4:
        return "benchmark"
    if ip.version == 6 and ip in _BENCHMARK_V6:
        return "benchmark"
    if ip.version == 4 and ip in _CGNAT:
        return "cgnat"
    if ip == _METADATA_V4 or ip == _METADATA_V6:
        return "metadata"
    if ip.is_loopback:
        return "loopback"
    if ip.is_link_local:
        return "link_local"
    if ip.is_multicast or ip.is_reserved or ip.is_unspecified:
        return "reserved"
    if ip.is_private or not ip.is_global:
        return "private"
    return None


def egress_evidence(
    *,
    resolver_verified: bool,
    tls_hostname_verified: bool,
    destination_policy_enforced: bool,
    enforcement_location: str,
    trust_env: bool | None = None,
) -> dict[str, Any]:
    """Separate the four facts. A direct client is not last-hop enforcement.

    ``trust_env=False`` never promotes ``destination_policy_enforced``. That
    flag is true only for a location the probe (or an operator last hop) proved.
    """
    location = enforcement_location if enforcement_location in _LOCATIONS else "unproven"
    enforced = bool(destination_policy_enforced) and location in _PROVEN_LOCATIONS
    if trust_env is False and location not in _PROVEN_LOCATIONS:
        enforced = False
    return {
        "destination_policy_enforced": enforced,
        "enforcement_location": location,
        "resolver_verified": bool(resolver_verified),
        "tls_hostname_verified": bool(tls_hostname_verified),
    }


def ambient_policy(environ: Mapping[str, str]) -> str:
    """``direct`` ignores ambient proxy variables. It does not use them."""
    value = environ.get("GUARDIAN_EGRESS_AMBIENT_PROXY", "")
    if isinstance(value, str) and value.strip() == "direct":
        return "direct"
    return "refuse"


def assert_direct_transport(environ: Mapping[str, str], *, policy: str | None = None) -> None:
    """Refuse unapproved HTTP(S)_PROXY / ALL_PROXY values. Do not echo them."""
    chosen = ambient_policy(environ) if policy is None else policy
    present = [name for name in _PROXY_NAMES if isinstance(environ.get(name), str) and environ[name].strip()]
    if not present or chosen == "direct":
        return
    raise SecurityError(
        "ambient_proxy: direct transport refuses ambient proxy env (" + ",".join(present) + ")",
        reason="ambient_proxy",
    )


def direct_client_kwargs(*, timeout: float) -> dict[str, Any]:
    """httpx settings for a direct connect. Verification stays on."""
    return {
        "follow_redirects": False,
        "timeout": timeout,
        "trust_env": False,
        "verify": True,
    }


def assert_query_hostname(host: str) -> str:
    """A DoH query name. Not a URL, not a wallet, not a single label."""
    name = str(host).strip().lower().rstrip(".")
    if (
        "://" in name
        or any(mark in name for mark in "/?@\\ \t\r\n")
        or _WALLET.search(name)
        or "apikey" in name
        or "api_key" in name
    ):
        raise SecurityError("doh query must be a hostname", reason="doh_query")
    labels = name.split(".")
    if len(labels) < 2 or len(name) > 253 or any(_LABEL.fullmatch(label) is None for label in labels):
        raise SecurityError("doh query must be a hostname", reason="doh_query")
    return name


@dataclass(frozen=True)
class DohEndpoint:
    url: str
    host: str
    bootstrap_ips: tuple[str, ...]


@dataclass(frozen=True)
class ProbeHost:
    hostname: str
    ip: str


@dataclass(frozen=True)
class ResolverSettings:
    mode: str
    endpoints: tuple[DohEndpoint, ...]
    probe_name: str = "example.com"
    probe_hosts: tuple[ProbeHost, ...] = ()
    timeout_seconds: float = 5.0
    max_response_bytes: int = 8192


@dataclass(frozen=True)
class DohRequest:
    method: str
    url: str
    connect_ip: str
    sni_hostname: str
    verify_hostname: str
    tls_verify: bool
    timeout: float
    max_response_bytes: int
    headers: tuple[tuple[str, str], ...]
    query_name: str
    query_type: str


@dataclass(frozen=True)
class DohResponse:
    status: int
    headers: tuple[tuple[str, str], ...]
    body: bytes

    def __post_init__(self) -> None:
        raw = self.headers
        if isinstance(raw, Mapping):
            pairs = tuple((str(key), str(value)) for key, value in raw.items())
        else:
            pairs = tuple((str(key), str(value)) for key, value in raw)
        object.__setattr__(self, "headers", pairs)
        object.__setattr__(self, "body", bytes(self.body))


@dataclass(frozen=True)
class TlsHandshake:
    connect_ip: str
    port: int
    sni_hostname: str
    verify_hostname: str
    tls_verify: bool


@dataclass(frozen=True)
class TlsOutcome:
    verified_hostname: str | None = None
    presented_name: str | None = None
    error: str | None = None


@dataclass
class EgressHooks:
    """Fake DNS, DoH, and TLS layers for offline diagnosis. Production leaves these empty."""

    system_resolve: Callable[[str], list[str]] | None = None
    exchange: Callable[[DohRequest], DohResponse] | None = None
    tls: Callable[[TlsHandshake], TlsOutcome] | None = None
    exercise_pin: bool = False
    redirect_location: str = "https://evil.example/api/v3/ping"
    observed_urls: list[str] = field(default_factory=list)


def load_resolver_settings(
    document: Mapping[str, Any] | None,
    environ: Mapping[str, str],
    *,
    egress_config: str | None = None,
    mode: str | None = None,
) -> ResolverSettings:
    """Public monitor data may name env vars. URLs and bootstrap IPs come from the operator."""
    mode_name = _MODE_ENV
    endpoints_name = _ENDPOINTS_ENV
    bootstrap_name = _BOOTSTRAP_ENV
    if isinstance(document, Mapping):
        block = document.get("resolver")
        if isinstance(block, Mapping):
            mode_name = _env_name(block.get("mode_env"), mode_name)
            endpoints_name = _env_name(block.get("endpoints_env"), endpoints_name)
            bootstrap_name = _env_name(block.get("bootstrap_env"), bootstrap_name)
    selected = "system"
    file_mode = None
    endpoints: tuple[DohEndpoint, ...] = ()
    probe_name = "example.com"
    probe_hosts: tuple[ProbeHost, ...] = ()
    if egress_config:
        loaded = _read_egress_file(egress_config)
        file_mode = loaded["mode"]
        endpoints = loaded["endpoints"]
        probe_name = loaded["probe_name"]
        probe_hosts = loaded["probe_hosts"]
    env_mode = environ.get(mode_name)
    if file_mode:
        selected = file_mode
    elif isinstance(env_mode, str) and env_mode.strip():
        selected = env_mode.strip().lower()
    if not endpoints:
        endpoints = _endpoints_from_env(environ.get(endpoints_name), environ.get(bootstrap_name))
    if not probe_hosts:
        probe_hosts = _hosts_from_endpoints(endpoints)
    if mode:
        selected = mode
    if selected not in {"system", "doh"}:
        raise ConfigError("resolver mode must be system or doh")
    if selected == "doh" and not endpoints:
        raise ConfigError("doh mode requires endpoints")
    try:
        probe_name = assert_query_hostname(probe_name)
    except SecurityError as exc:
        raise ConfigError("probe name must be a hostname") from exc
    return ResolverSettings(
        mode=selected,
        endpoints=endpoints,
        probe_name=probe_name,
        probe_hosts=probe_hosts,
    )


class HostnameResolver:
    """Resolve one hostname. The default mode does not change a normal public answer."""

    def __init__(self, settings: ResolverSettings, hooks: EgressHooks | None = None) -> None:
        self.settings = settings
        self.hooks = hooks
        self.last_verified = False
        self.sent: list[DohRequest] = []

    def resolve(self, host: str) -> list[str]:
        name = assert_query_hostname(host)
        self.last_verified = False
        if self.settings.mode == "system":
            answers = self._system(name)
            _reject_destination_answers(answers, mode="system")
            return _dedupe(answers)
        if self.settings.mode != "doh":
            raise ConfigError("resolver mode must be system or doh")
        return self._doh(name)

    def _system(self, host: str) -> list[str]:
        if self.hooks is not None and self.hooks.system_resolve is not None:
            return [str(item) for item in self.hooks.system_resolve(host)]
        infos = socket.getaddrinfo(host, None)
        return [item[4][0] for item in infos]

    def _doh(self, host: str) -> list[str]:
        errors: list[SecurityError] = []
        for endpoint in self.settings.endpoints:
            start = len(self.sent)
            try:
                ips = self._query_endpoint(endpoint, host)
            except SecurityError as exc:
                if exc.reason in _FALLBACK_REASONS:
                    errors.append(exc)
                    continue
                raise
            self.last_verified = _pins_ok(self.sent[start:], self.settings.endpoints)
            return ips
        if errors:
            raise errors[-1]
        raise SecurityError("doh failed", reason="doh_failed")

    def _query_endpoint(self, endpoint: DohEndpoint, host: str) -> list[str]:
        addresses = _parse_answers(host, self._fetch(endpoint, host, "A"), self.settings.max_response_bytes)
        addresses.extend(_parse_answers(host, self._fetch(endpoint, host, "AAAA"), self.settings.max_response_bytes))
        found = _dedupe(addresses)
        if not found:
            raise SecurityError("doh returned no addresses", reason="dns_failed")
        _reject_destination_answers(found, mode="doh")
        return found

    def _fetch(self, endpoint: DohEndpoint, host: str, qtype: str) -> bytes:
        last = "doh_timeout"
        for _attempt in range(2):
            request = _build_request(endpoint, host, qtype, self.settings)
            self.sent.append(request)
            try:
                response = self._call(request)
            except TimeoutError:
                last = "doh_timeout"
                continue
            except ConnectionError:
                last = "doh_connection"
                continue
            if response.status in _REDIRECTS:
                _reject_redirect()
            if response.status >= 500:
                last = "doh_status"
                continue
            if response.status != 200:
                raise SecurityError("doh http status is not usable", reason="doh_http")
            _check_size(response, self.settings.max_response_bytes)
            return bytes(response.body)
        raise SecurityError(last, reason=last)

    def _call(self, request: DohRequest) -> DohResponse:
        if request.tls_verify is not True:
            raise SecurityError("tls verification must stay enabled", reason="tls_verify_disabled")
        exchange = self.hooks.exchange if self.hooks is not None and self.hooks.exchange is not None else production_exchange
        return exchange(request)


def production_exchange(request: DohRequest) -> DohResponse:
    """Pinned DoH GET. Redirects stay off here; the resolver rejects them."""
    if request.tls_verify is not True:
        raise SecurityError("tls verification must stay enabled", reason="tls_verify_disabled")
    from guardian.sources.fixtures import PreparedCall
    from guardian.sources.transport import HttpxBackend, TransportError

    call = PreparedCall(
        "doh",
        "GET",
        request.url,
        b"",
        "rest",
        connect_ip=request.connect_ip,
        public_headers=(("accept", "application/dns-json"),),
    )
    backend = HttpxBackend(timeout=request.timeout, max_response_bytes=request.max_response_bytes)
    try:
        raw = backend.send(call)
    except TransportError as exc:
        if exc.reason == "timeout":
            raise TimeoutError("doh_timeout") from None
        if exc.reason == "response_too_large":
            raise SecurityError("doh response exceeds the size limit", reason="doh_size") from None
        raise ConnectionError("doh_connection") from None
    headers = tuple((str(key), str(value)) for key, value in raw.headers.items())
    return DohResponse(raw.status, headers, raw.body)


def production_tls(handshake: TlsHandshake) -> TlsOutcome:
    """Verify the certificate for the SNI name. Never disable hostname checks."""
    if handshake.tls_verify is not True or handshake.verify_hostname != handshake.sni_hostname:
        raise SecurityError("tls verification must stay enabled", reason="tls_verify_disabled")
    context = ssl.create_default_context()
    if context.check_hostname is not True or context.verify_mode != ssl.CERT_REQUIRED:
        raise SecurityError("tls verification must stay enabled", reason="tls_verify_disabled")
    try:
        raw = socket.create_connection((handshake.connect_ip, handshake.port), timeout=5)
    except OSError:
        return TlsOutcome(error="tls_error")
    tls = None
    try:
        try:
            tls = context.wrap_socket(raw, server_hostname=handshake.sni_hostname)
        except ssl.SSLCertVerificationError:
            return TlsOutcome(error="hostname_mismatch")
        except ssl.SSLError:
            return TlsOutcome(error="tls_error")
        return TlsOutcome(verified_hostname=handshake.sni_hostname, presented_name=handshake.sni_hostname)
    finally:
        closer = tls if tls is not None else raw
        try:
            closer.close()
        except OSError:
            pass


def diagnose_egress(
    *,
    document: Mapping[str, Any] | None = None,
    environ: Mapping[str, str] | None = None,
    egress_config: str | None = None,
    qualification: str = "formal",
    mode: str | None = None,
    hooks: EgressHooks | None = None,
) -> tuple[dict[str, Any], int]:
    """Read-only host diagnosis. The JSON report has no wallet identifier and no key."""
    if qualification not in {"formal", "preview"}:
        raise ConfigError("qualification must be formal or preview")
    pool = os.environ if environ is None else environ
    settings = load_resolver_settings(document, pool, egress_config=egress_config, mode=mode)
    report = _base_report(settings, qualification)
    try:
        assert_direct_transport(pool)
    except SecurityError as exc:
        return _refuse(report, exc, qualification), 4
    report["redirect_guard"] = _prove_redirect(settings, hooks)
    resolver = HostnameResolver(settings, hooks)
    try:
        ips = resolver.resolve(settings.probe_name)
    except SecurityError as exc:
        report["queries"] = _query_view(resolver.sent)
        return _refuse(report, exc, qualification), 4
    report["queries"] = _query_view(resolver.sent)
    report["resolution"] = {"ips": list(ips), "name": settings.probe_name}
    report["resolver_verified"] = bool(resolver.last_verified)
    if hooks is not None and hooks.exercise_pin and ips:
        _exercise_pin(settings.probe_name, ips[0])
        report["tls_hostname_verified"] = True
    report.update(_probe_destination(settings, hooks))
    _finish_verdict(report, qualification)
    _assert_public_report(report)
    return report, 0 if report["verdict"] == "OK" else 4


def _base_report(settings: ResolverSettings, qualification: str) -> dict[str, Any]:
    report = {
        "allow_private_network": False,
        "qualification": qualification,
        "queries": [],
        "reason": "",
        "redirect_guard": {},
        "resolution": {"ips": [], "name": settings.probe_name},
        "resolver_mode": settings.mode,
        "status": "ok",
    }
    report.update(egress_evidence(
        resolver_verified=False,
        tls_hostname_verified=False,
        destination_policy_enforced=False,
        enforcement_location="unproven",
    ))
    report["verdict"] = "BLOCKED_EGRESS"
    return report


def _refuse(report: dict[str, Any], exc: SecurityError, qualification: str) -> dict[str, Any]:
    report["status"] = "refused"
    report["reason"] = exc.reason or "refused"
    report["error"] = str(exc)
    report["destination_policy_enforced"] = False
    if report.get("enforcement_location") not in _LOCATIONS:
        report["enforcement_location"] = "unproven"
    _finish_verdict(report, qualification)
    if report["status"] == "refused":
        report["verdict"] = "BLOCKED_EGRESS"
    _assert_public_report(report)
    return report


def _finish_verdict(report: dict[str, Any], qualification: str) -> None:
    if report.get("status") == "refused":
        report["verdict"] = "BLOCKED_EGRESS"
        return
    enforced = bool(report.get("destination_policy_enforced"))
    if qualification == "preview" or enforced:
        report["verdict"] = "OK"
        return
    report["verdict"] = "BLOCKED_EGRESS"


def _probe_destination(settings: ResolverSettings, hooks: EgressHooks | None) -> dict[str, Any]:
    hosts = settings.probe_hosts
    if len(hosts) < 2:
        return {}
    tls = None
    if hooks is not None and hooks.tls is not None:
        tls = hooks.tls
    elif hooks is None:
        tls = production_tls
    if tls is None:
        return {}
    host_a, host_b = hosts[0], hosts[1]
    same = tls(TlsHandshake(host_a.ip, 443, host_a.hostname, host_a.hostname, True))
    cross = tls(TlsHandshake(host_a.ip, 443, host_b.hostname, host_b.hostname, True))
    tls_ok = same.error in (None, "") and same.verified_hostname == host_a.hostname
    matched_other = cross.error in (None, "") and cross.verified_hostname == host_b.hostname
    if matched_other:
        evidence = egress_evidence(
            resolver_verified=False,
            tls_hostname_verified=tls_ok,
            destination_policy_enforced=False,
            enforcement_location="sni_proxy",
        )
    elif cross.error == "hostname_mismatch":
        evidence = egress_evidence(
            resolver_verified=False,
            tls_hostname_verified=tls_ok,
            destination_policy_enforced=True,
            enforcement_location="destination_ip",
        )
    else:
        evidence = egress_evidence(
            resolver_verified=False,
            tls_hostname_verified=tls_ok,
            destination_policy_enforced=False,
            enforcement_location="unproven",
        )
    return {
        "destination_policy_enforced": evidence["destination_policy_enforced"],
        "enforcement_location": evidence["enforcement_location"],
        "tls_hostname_verified": evidence["tls_hostname_verified"],
    }


def _exercise_pin(hostname: str, connect_ip: str) -> None:
    """One direct GET so a fake HTTP client can observe IP, SNI, and verify."""
    from guardian.sources.fixtures import PreparedCall
    from guardian.sources.transport import HttpxBackend

    call = PreparedCall("egress", "GET", f"https://{hostname}/", b"", "rest", connect_ip=connect_ip)
    HttpxBackend(timeout=5).send(call)


def _prove_redirect(settings: ResolverSettings, hooks: EgressHooks | None) -> dict[str, Any]:
    """A 3xx to another host is not fetched. The registered host stays the connect target."""
    from guardian.security.transport_policy import Operation
    from guardian.sources.budget import BudgetBook
    from guardian.sources.fixtures import RawResponse
    from guardian.sources.transport import EndpointRegistry, ReadOnlyTransport, RegisteredSource

    host = settings.probe_hosts[0].hostname if settings.probe_hosts else settings.probe_name
    location = hooks.redirect_location if hooks is not None else "https://evil.example/api/v3/ping"
    observed = hooks.observed_urls if hooks is not None else []
    source = RegisteredSource(
        source_id="egress-probe",
        adapter="coingecko",
        endpoint=f"https://{host}/",
        budget_group="egress-probe",
        allow_private_network=False,
    )
    book = BudgetBook.from_document(
        {
            "egress-probe": {
                "max_concurrency": 1,
                "max_requests_per_day": 10,
                "max_rps": 10,
                "retry_attempts": 0,
            }
        },
        clock=lambda: 0.0,
        sleep=lambda _seconds: None,
    )

    class _Backend:
        def send(self, call: Any) -> RawResponse:
            observed.append(str(call.url))
            return RawResponse(302, {"location": location}, b"")

    transport = ReadOnlyTransport(
        EndpointRegistry({"egress-probe": source}),
        book,
        _Backend(),
        resolve=lambda _host: ["93.184.216.34"],
    )
    operation = Operation(protocol="rest", adapter="coingecko", http_method="GET", path="/api/v3/ping")
    reason = "not_rejected"
    try:
        transport.request("egress-probe", operation)
    except SecurityError as exc:
        reason = exc.reason or "rejected"
    hosts = sorted({(urlsplit(url).hostname or "") for url in observed})
    return {"hosts": hosts, "reason": reason, "rejected": reason == "redirect_host"}


def _reject_destination_answers(answers: list[str], *, mode: str) -> None:
    if not answers:
        raise SecurityError("dns returned no addresses", reason="dns_failed")
    for raw in answers:
        try:
            reason = destination_block_reason(raw)
        except ValueError:
            raise SecurityError("resolver answer is not an ip", reason="dns_failed") from None
        if reason is None:
            continue
        if reason == "benchmark" and mode == "system":
            raise SecurityError(
                f"benchmark_address: system resolver returned {raw} in 198.18.0.0/15; switch resolver mode to doh",
                reason="benchmark_address",
            )
        if reason == "benchmark":
            raise SecurityError(
                f"benchmark_address: resolver answer {raw} is in 198.18.0.0/15 and was rejected",
                reason="benchmark_address",
            )
        raise SecurityError(
            f"private_endpoint: resolver answer {raw} is {reason}",
            reason="private_endpoint",
        )


def _build_request(endpoint: DohEndpoint, host: str, qtype: str, settings: ResolverSettings) -> DohRequest:
    if qtype not in _QUERY_TYPES:
        raise SecurityError("doh query must be a hostname", reason="doh_query")
    parts = urlsplit(endpoint.url)
    query = urlunsplit((
        "https",
        parts.netloc,
        parts.path or "/",
        "name=" + host + "&type=" + qtype,
        "",
    ))
    parsed = urlsplit(query)
    pairs = parse_qsl(parsed.query, keep_blank_values=True)
    if pairs != [("name", host), ("type", qtype)]:
        raise SecurityError("doh query must be a hostname", reason="doh_query")
    if parsed.hostname != endpoint.host:
        raise SecurityError("doh query must be a hostname", reason="doh_query")
    return DohRequest(
        method="GET",
        url=query,
        connect_ip=endpoint.bootstrap_ips[0],
        sni_hostname=endpoint.host,
        verify_hostname=endpoint.host,
        tls_verify=True,
        timeout=settings.timeout_seconds,
        max_response_bytes=settings.max_response_bytes,
        headers=(("accept", "application/dns-json"),),
        query_name=host,
        query_type=qtype,
    )


def _reject_redirect() -> None:
    raise SecurityError("doh redirect is not followed", reason="doh_redirect_unapproved")


def _check_size(response: DohResponse, limit: int) -> None:
    for key, value in response.headers:
        if key.lower() != "content-length":
            continue
        try:
            declared = int(value)
        except ValueError:
            raise SecurityError("doh response exceeds the size limit", reason="doh_size") from None
        if declared > limit:
            raise SecurityError("doh response exceeds the size limit", reason="doh_size")
    if len(response.body) > limit:
        raise SecurityError("doh response exceeds the size limit", reason="doh_size")


def _parse_answers(host: str, body: bytes, limit: int) -> list[str]:
    if len(body) > limit:
        raise SecurityError("doh response exceeds the size limit", reason="doh_size")
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError):
        raise SecurityError("doh answer is malformed", reason="doh_malformed") from None
    if not isinstance(payload, dict):
        raise SecurityError("doh answer is malformed", reason="doh_malformed")
    status = payload.get("Status")
    if isinstance(status, bool) or status != 0:
        raise SecurityError("doh status is not NOERROR", reason="doh_status")
    if payload.get("TC") is True:
        raise SecurityError("doh answer was truncated", reason="doh_truncated")
    answers = payload.get("Answer", [])
    if answers is None:
        answers = []
    if not isinstance(answers, list) or len(answers) > _MAX_RECORDS:
        raise SecurityError("doh answer is malformed", reason="doh_malformed")
    return _follow_cname(host, answers)


def _follow_cname(host: str, answers: list[Any]) -> list[str]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for record in answers:
        if not isinstance(record, dict):
            raise SecurityError("doh answer is malformed", reason="doh_malformed")
        _require_ttl(record)
        _record_type(record)
        owner = _record_name(record.get("name"))
        grouped.setdefault(owner, []).append(record)
    name = assert_query_hostname(host)
    seen: set[str] = set()
    for _step in range(_MAX_CNAME):
        if name in seen:
            raise SecurityError("doh cname chain looped", reason="doh_cname")
        seen.add(name)
        records = grouped.get(name, [])
        addresses = [item for item in records if _record_type(item) in {_A, _AAAA}]
        cnames = [item for item in records if _record_type(item) == _CNAME]
        if addresses:
            found = []
            for item in addresses:
                data = item.get("data")
                if not isinstance(data, str) or not data.strip():
                    raise SecurityError("doh answer is malformed", reason="doh_malformed")
                found.append(data.strip())
            _reject_destination_answers(found, mode="doh")
            return found
        if not cnames:
            return []
        if len(cnames) != 1:
            raise SecurityError("doh cname chain is ambiguous", reason="doh_cname")
        target = cnames[0].get("data")
        if not isinstance(target, str):
            raise SecurityError("doh cname chain is unusable", reason="doh_cname")
        try:
            name = assert_query_hostname(target)
        except SecurityError as exc:
            raise SecurityError("doh cname chain is unusable", reason="doh_cname") from exc
    raise SecurityError("doh cname chain is too long", reason="doh_cname")


def _require_ttl(record: Mapping[str, Any]) -> None:
    ttl = record.get("TTL")
    if isinstance(ttl, bool) or not isinstance(ttl, int) or ttl < 0 or ttl > _MAX_TTL:
        raise SecurityError("doh ttl is missing or out of range", reason="doh_ttl")


def _record_type(record: Mapping[str, Any]) -> int:
    value = record.get("type")
    if isinstance(value, bool) or not isinstance(value, int):
        raise SecurityError("doh answer is malformed", reason="doh_malformed")
    return value


def _record_name(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise SecurityError("doh answer is malformed", reason="doh_malformed")
    return value.strip().lower().rstrip(".")


def _pins_ok(requests: list[DohRequest], endpoints: tuple[DohEndpoint, ...]) -> bool:
    if not requests:
        return False
    allowed = {ip for endpoint in endpoints for ip in endpoint.bootstrap_ips}
    hosts = {endpoint.host for endpoint in endpoints}
    for request in requests:
        if request.tls_verify is not True or request.connect_ip not in allowed:
            return False
        if request.sni_hostname not in hosts or request.verify_hostname != request.sni_hostname:
            return False
        if urlsplit(request.url).hostname != request.sni_hostname:
            return False
    return True


def _query_view(requests: list[DohRequest]) -> list[dict[str, Any]]:
    view = []
    for request in requests:
        view.append({
            "connect_ip": request.connect_ip,
            "query_name": request.query_name,
            "query_type": request.query_type,
            "sni_hostname": request.sni_hostname,
            "tls_verify": request.tls_verify,
            "url": request.url,
            "verify_hostname": request.verify_hostname,
        })
    return view


def _dedupe(values: list[str]) -> list[str]:
    found: list[str] = []
    for value in values:
        if value not in found:
            found.append(value)
    return found


def _assert_public_report(report: dict[str, Any]) -> None:
    blob = json.dumps(report, ensure_ascii=True)
    if _WALLET.search(blob) or "apikey=" in blob.lower() or "glpat-" in blob.lower():
        raise SecurityError("egress report contained private material", reason="report_private")


def _env_name(value: Any, default: str) -> str:
    if value is None:
        return default
    if not isinstance(value, str) or _ENV_NAME.fullmatch(value) is None:
        raise ConfigError("resolver env name is invalid")
    return value


def _endpoints_from_env(raw_endpoints: Any, raw_bootstrap: Any) -> tuple[DohEndpoint, ...]:
    if not isinstance(raw_endpoints, str) or not raw_endpoints.strip():
        return ()
    pins = _bootstrap_map(raw_bootstrap)
    endpoints = []
    for item in raw_endpoints.split(","):
        url = item.strip()
        if not url:
            continue
        endpoint = _endpoint(url, pins.get(_host_of(url), ()))
        endpoints.append(endpoint)
    return tuple(endpoints)


def _bootstrap_map(raw: Any) -> dict[str, tuple[str, ...]]:
    if not isinstance(raw, str) or not raw.strip():
        return {}
    pins: dict[str, tuple[str, ...]] = {}
    for item in raw.split(";"):
        piece = item.strip()
        if not piece:
            continue
        if "=" not in piece:
            raise ConfigError("doh bootstrap entry is invalid")
        host, ips = piece.split("=", 1)
        try:
            name = assert_query_hostname(host)
        except SecurityError as exc:
            raise ConfigError("doh bootstrap host is invalid") from exc
        pins[name] = tuple(_public_ip(part) for part in ips.split(",") if part.strip())
        if not pins[name]:
            raise ConfigError("doh bootstrap entry is invalid")
    return pins


def _hosts_from_endpoints(endpoints: tuple[DohEndpoint, ...]) -> tuple[ProbeHost, ...]:
    hosts: list[ProbeHost] = []
    seen: set[str] = set()
    for endpoint in endpoints:
        if endpoint.host in seen:
            continue
        seen.add(endpoint.host)
        hosts.append(ProbeHost(endpoint.host, endpoint.bootstrap_ips[0]))
    return tuple(hosts)


def _read_egress_file(path: str) -> dict[str, Any]:
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError("egress config was not found") from exc
    if _WALLET.search(text) or "apikey=" in text.lower():
        raise ConfigError("egress config contains a forbidden value")
    try:
        document = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ConfigError("egress config is not valid json") from exc
    if not isinstance(document, dict) or not set(document).issubset(_FILE_KEYS):
        raise ConfigError("egress config is not valid")
    _reject_forbidden_keys(document)
    mode = document.get("mode", "system")
    if mode not in {"system", "doh"}:
        raise ConfigError("resolver mode must be system or doh")
    probe_name = document.get("probe_name", "example.com")
    if not isinstance(probe_name, str):
        raise ConfigError("probe name must be a hostname")
    raw_endpoints = document.get("endpoints", [])
    raw_hosts = document.get("probe_hosts", [])
    if not isinstance(raw_endpoints, list) or not isinstance(raw_hosts, list):
        raise ConfigError("egress config is not valid")
    endpoints = tuple(_endpoint_record(item) for item in raw_endpoints)
    hosts = tuple(_probe_host(item) for item in raw_hosts)
    return {"endpoints": endpoints, "mode": mode, "probe_hosts": hosts, "probe_name": probe_name}


def _endpoint_record(item: Any) -> DohEndpoint:
    if not isinstance(item, dict) or not set(item).issubset(_ENDPOINT_KEYS):
        raise ConfigError("doh endpoints are invalid")
    _reject_forbidden_keys(item)
    url = item.get("url")
    ips = item.get("bootstrap_ips")
    if not isinstance(url, str) or not isinstance(ips, list) or not ips:
        raise ConfigError("doh endpoints are invalid")
    return _endpoint(url, tuple(_public_ip(ip) for ip in ips))


def _endpoint(url: str, bootstrap_ips: tuple[str, ...]) -> DohEndpoint:
    parts = urlsplit(url)
    if parts.scheme != "https" or not parts.hostname or parts.username or parts.password or parts.query or parts.fragment:
        raise ConfigError("doh endpoint must be https without userinfo or a query")
    try:
        host = assert_query_hostname(parts.hostname)
    except SecurityError as exc:
        raise ConfigError("doh endpoint host is invalid") from exc
    if not bootstrap_ips:
        raise ConfigError("doh endpoint requires bootstrap ips")
    return DohEndpoint(url=url, host=host, bootstrap_ips=bootstrap_ips)


def _probe_host(item: Any) -> ProbeHost:
    if not isinstance(item, dict) or not set(item).issubset(_HOST_KEYS):
        raise ConfigError("probe hosts are invalid")
    _reject_forbidden_keys(item)
    hostname = item.get("hostname")
    ip = item.get("ip")
    if not isinstance(hostname, str) or not isinstance(ip, str):
        raise ConfigError("probe hosts are invalid")
    try:
        name = assert_query_hostname(hostname)
    except SecurityError as exc:
        raise ConfigError("probe hosts are invalid") from exc
    return ProbeHost(name, _public_ip(ip))


def _public_ip(value: Any) -> str:
    if not isinstance(value, str):
        raise ConfigError("bootstrap ip is invalid")
    try:
        reason = destination_block_reason(value.strip())
    except ValueError as exc:
        raise ConfigError("bootstrap ip is invalid") from exc
    if reason is not None:
        raise ConfigError("bootstrap ip is not a public address")
    return value.strip()


def _host_of(url: str) -> str:
    host = urlsplit(url).hostname or ""
    try:
        return assert_query_hostname(host)
    except SecurityError as exc:
        raise ConfigError("doh endpoint host is invalid") from exc


def _reject_forbidden_keys(value: Any) -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            folded = "".join(ch for ch in str(key).lower() if ch.isalnum())
            if folded in _FORBIDDEN:
                raise ConfigError("egress config contains a forbidden field")
            _reject_forbidden_keys(item)
        return
    if isinstance(value, list):
        for item in value:
            _reject_forbidden_keys(item)


BENCHMARK_MESSAGE = _BENCHMARK_MESSAGE
