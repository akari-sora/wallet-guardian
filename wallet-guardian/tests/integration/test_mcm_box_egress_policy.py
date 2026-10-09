"""MCM-SHARE-01 S0. Offline egress policy through the doctor tool and the CLI.

DNS, DoH, sockets, and TLS are fakes. A real network call fails the test.
"""

from __future__ import annotations

import importlib.util
import json
import socket
import sys
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit

import httpx
import pytest

from guardian.cli import main
from guardian.sources.resolver import (
    DohResponse,
    EgressHooks,
    TlsHandshake,
    TlsOutcome,
    destination_block_reason,
    egress_evidence,
)

ROOT = Path(__file__).resolve().parents[2]
EXAMPLE = ROOT / "examples" / "monitor.json"
WALLET = "0x" + ("ab" * 20)
SECRET = "synthetic-key-s0-share"
AAAA = "2001:4860:4860::8888"
PUBLIC_V4 = "93.184.216.34"
FIELDS = (
    "destination_policy_enforced",
    "enforcement_location",
    "resolver_verified",
    "tls_hostname_verified",
)
_AMBIENT = (
    "ALL_PROXY",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "all_proxy",
    "http_proxy",
    "https_proxy",
    "GUARDIAN_EGRESS_AMBIENT_PROXY",
    "GUARDIAN_RESOLVER_MODE",
    "GUARDIAN_DOH_ENDPOINTS",
    "GUARDIAN_DOH_BOOTSTRAP",
    "MONITOR_ACCOUNTS_FILE",
    "MONITOR_SECRET_PROFILE",
)
_DOH = {
    "mode": "doh",
    "probe_name": "example.com",
    "endpoints": [
        {
            "url": "https://cloudflare-dns.com/dns-query",
            "bootstrap_ips": ["1.1.1.1", "1.0.0.1"],
        },
        {
            "url": "https://dns.google/resolve",
            "bootstrap_ips": ["8.8.8.8", "8.8.4.4"],
        },
    ],
}
_TOOL = None


@pytest.fixture(autouse=True)
def _block_network(monkeypatch: pytest.MonkeyPatch) -> None:
    def blocked(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("network is disabled")

    monkeypatch.setattr(socket, "socket", blocked)
    monkeypatch.setattr(socket, "create_connection", blocked)
    monkeypatch.setattr(socket, "getaddrinfo", blocked)
    monkeypatch.setattr(httpx, "Client", blocked)
    for name in _AMBIENT:
        monkeypatch.delenv(name, raising=False)


def _tool():
    global _TOOL
    if _TOOL is None:
        path = ROOT / "tools" / "egress_doctor.py"
        spec = importlib.util.spec_from_file_location("wg_egress_doctor_tool", path)
        module = importlib.util.module_from_spec(spec)
        assert spec is not None and spec.loader is not None
        spec.loader.exec_module(module)
        _TOOL = module
    return _TOOL


def _write(tmp_path: Path, document: dict, name: str = "egress.json") -> Path:
    path = tmp_path / name
    path.write_text(json.dumps(document), encoding="utf-8")
    return path


def _monitor(tmp_path: Path) -> Path:
    document = json.loads(EXAMPLE.read_text(encoding="utf-8"))
    document["resolver"] = {
        "mode_env": "GUARDIAN_RESOLVER_MODE",
        "endpoints_env": "GUARDIAN_DOH_ENDPOINTS",
        "bootstrap_env": "GUARDIAN_DOH_BOOTSTRAP",
    }
    path = tmp_path / "monitor.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    return path


def _invoke(capsys: pytest.CaptureFixture[str], argv: list[str], hooks: EgressHooks) -> tuple[int, dict, str]:
    code = _tool().main(argv, hooks=hooks)
    text = capsys.readouterr().out
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise AssertionError(text) from exc
    return code, payload, text


def _record(name: str, kind: int, ttl: int, data: str) -> dict:
    return {"TTL": ttl, "data": data, "name": name, "type": kind}


def _packet(records: list[dict]) -> DohResponse:
    return DohResponse(200, (), json.dumps({"Answer": records, "Status": 0}).encode("utf-8"))


def _plant(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    missing = tmp_path / "missing-accounts.json"
    monkeypatch.setenv("MONITOR_ACCOUNTS_FILE", str(missing))
    monkeypatch.setenv("ETHERSCAN_API_KEY", SECRET)
    monkeypatch.setenv("WATCHED_ADDRESS", WALLET)


def _assert_clean(text: str) -> None:
    folded = text.lower()
    assert WALLET.lower() not in folded
    assert SECRET.lower() not in folded
    assert "apikey=" not in folded


def _assert_query(request, host: str, pins: set[str]) -> None:
    assert request.method == "GET"
    assert request.headers == (("accept", "application/dns-json"),)
    assert request.tls_verify is True
    assert request.connect_ip in pins
    assert request.sni_hostname == host
    assert request.verify_hostname == host
    assert request.query_name == "example.com"
    parts = urlsplit(request.url)
    assert parts.hostname == host
    assert parts.scheme == "https"
    assert parse_qsl(parts.query) == [("name", "example.com"), ("type", request.query_type)]
    assert WALLET not in request.url
    assert SECRET not in request.url


def _fields(report: dict) -> None:
    for key in FIELDS:
        assert key in report


def test_mcm_share_s0_01_doh_validated_resolution(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    """MCM-SHARE-S0-01. DoH validates A/AAAA, CNAME, TTL, and size, and sends only the hostname."""
    assert destination_block_reason(PUBLIC_V4) is None
    assert destination_block_reason(AAAA) is None
    assert destination_block_reason("198.18.0.1") == "benchmark"
    _plant(monkeypatch, tmp_path)
    path = _write(tmp_path, _DOH)
    monitor = _monitor(tmp_path)
    pins = {"1.1.1.1", "1.0.0.1"}

    def good(request):
        _assert_query(request, "cloudflare-dns.com", pins)
        if request.query_type == "A":
            records = [
                _record("example.com.", 5, 120, "origin.example."),
                _record("origin.example.", 1, 60, PUBLIC_V4),
            ]
        else:
            records = [
                _record("example.com.", 5, 120, "origin.example."),
                _record("origin.example.", 28, 60, AAAA),
            ]
        body = {"Answer": records, "Status": 0, "note": "https://evil.example/dns-query"}
        return DohResponse(200, (), json.dumps(body).encode("utf-8"))

    code, report, text = _invoke(
        capsys,
        ["--qualification", "preview", "--mode", "doh", "--egress-config", str(path), "--mcm-config", str(monitor)],
        EgressHooks(exchange=good),
    )
    assert code == 0, text
    assert report["status"] == "ok"
    assert report["resolver_mode"] == "doh"
    assert report["resolver_verified"] is True
    assert report["resolution"]["ips"] == [PUBLIC_V4, AAAA]
    assert report["allow_private_network"] is False
    assert report["destination_policy_enforced"] is False
    assert [item["query_type"] for item in report["queries"]] == ["A", "AAAA"]
    assert {item["query_name"] for item in report["queries"]} == {"example.com"}
    assert all(item["connect_ip"] in pins and item["tls_verify"] is True for item in report["queries"])
    assert "evil.example" not in text
    _assert_clean(text)
    _fields(report)

    cases = [
        ("198.18.0.1", 1, "benchmark_address", "benchmark"),
        ("10.1.2.3", 1, "private_endpoint", "private"),
        ("127.0.0.1", 1, "private_endpoint", "loopback"),
        ("169.254.169.254", 1, "private_endpoint", "metadata"),
        ("169.254.1.1", 1, "private_endpoint", "link_local"),
        ("100.64.0.1", 1, "private_endpoint", "cgnat"),
        ("192.0.2.1", 1, "private_endpoint", "private"),
        ("240.0.0.1", 1, "private_endpoint", "reserved"),
        ("::1", 28, "private_endpoint", "loopback"),
        ("2001:2::1", 28, "benchmark_address", "benchmark"),
        ("fd00:ec2::254", 28, "private_endpoint", "metadata"),
    ]
    for ip, kind, reason, word in cases:
        assert destination_block_reason(ip) == word

        def answer(request, ip=ip, kind=kind):
            return _packet([_record("example.com.", kind, 30, ip)])

        code, report, text = _invoke(
            capsys,
            ["--qualification", "preview", "--mode", "doh", "--egress-config", str(path)],
            EgressHooks(exchange=answer),
        )
        assert code == 4, text
        assert report["status"] == "refused"
        assert report["reason"] == reason
        assert report["allow_private_network"] is False
        assert report["destination_policy_enforced"] is False
        assert ip in report["error"]
        assert word in report["error"]
        assert '"allow_private_network": true' not in text
        _assert_clean(text)

    def missing_ttl(_request):
        body = {"Answer": [{"data": PUBLIC_V4, "name": "example.com.", "type": 1}], "Status": 0}
        return DohResponse(200, (), json.dumps(body).encode("utf-8"))

    code, report, text = _invoke(
        capsys,
        ["--qualification", "preview", "--mode", "doh", "--egress-config", str(path)],
        EgressHooks(exchange=missing_ttl),
    )
    assert code == 4, text
    assert report["reason"] == "doh_ttl"

    def huge_ttl(_request):
        return _packet([_record("example.com.", 1, 604801, PUBLIC_V4)])

    code, report, text = _invoke(
        capsys,
        ["--qualification", "preview", "--mode", "doh", "--egress-config", str(path)],
        EgressHooks(exchange=huge_ttl),
    )
    assert code == 4, text
    assert report["reason"] == "doh_ttl"

    def oversized(_request):
        return DohResponse(200, (), b"x" * 8193)

    code, report, text = _invoke(
        capsys,
        ["--qualification", "preview", "--mode", "doh", "--egress-config", str(path)],
        EgressHooks(exchange=oversized),
    )
    assert code == 4, text
    assert report["reason"] == "doh_size"

    def looped(_request):
        return _packet([
            _record("example.com.", 5, 30, "loop.example."),
            _record("loop.example.", 5, 30, "example.com."),
        ])

    code, report, text = _invoke(
        capsys,
        ["--qualification", "preview", "--mode", "doh", "--egress-config", str(path)],
        EgressHooks(exchange=looped),
    )
    assert code == 4, text
    assert report["reason"] == "doh_cname"

    def mixed(request):
        if request.query_type == "A":
            return _packet([_record("example.com.", 1, 30, PUBLIC_V4)])
        return _packet([_record("example.com.", 28, 30, "::1")])

    code, report, text = _invoke(
        capsys,
        ["--qualification", "preview", "--mode", "doh", "--egress-config", str(path)],
        EgressHooks(exchange=mixed),
    )
    assert code == 4, text
    assert report["reason"] == "private_endpoint"
    assert "loopback" in report["error"]
    assert report["resolution"]["ips"] == []

    calls: list[str] = []

    def redirected(request):
        calls.append(request.url)
        location = f"https://evil.example/dns-query?name={WALLET}&type=A"
        return DohResponse(302, {"location": location}, b"")

    code, report, text = _invoke(
        capsys,
        ["--qualification", "preview", "--mode", "doh", "--egress-config", str(path)],
        EgressHooks(exchange=redirected),
    )
    assert code == 4, text
    assert report["reason"] == "doh_redirect_unapproved"
    assert len(calls) == 1
    assert urlsplit(calls[0]).hostname == "cloudflare-dns.com"
    assert "evil.example" not in text
    _assert_clean(text)

    fallen: list[str] = []

    def http_404(request):
        fallen.append(urlsplit(request.url).hostname or "")
        return DohResponse(404, (), b"")

    code, report, text = _invoke(
        capsys,
        ["--qualification", "preview", "--mode", "doh", "--egress-config", str(path)],
        EgressHooks(exchange=http_404),
    )
    assert code == 4, text
    assert report["reason"] == "doh_http"
    assert fallen == ["cloudflare-dns.com"]


def test_mcm_share_s0_02_fake_ip_system_resolver_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    """MCM-SHARE-S0-02. A public answer stays public. 198.18.0.1 for api.etherscan.io is refused."""
    _plant(monkeypatch, tmp_path)

    def public(host: str) -> list[str]:
        assert host == "example.com"
        return [PUBLIC_V4]

    code, report, text = _invoke(
        capsys,
        ["--qualification", "preview", "--mode", "system"],
        EgressHooks(system_resolve=public),
    )
    assert code == 0, text
    assert report["status"] == "ok"
    assert report["resolver_mode"] == "system"
    assert report["resolution"]["ips"] == [PUBLIC_V4]
    assert report["resolver_verified"] is False
    assert report["destination_policy_enforced"] is False
    assert report["enforcement_location"] == "unproven"
    assert report["allow_private_network"] is False
    assert report["verdict"] == "OK"
    assert "198.18.0.1" not in text
    _fields(report)

    path = _write(tmp_path, {"mode": "system", "probe_name": "api.etherscan.io"})
    seen: list[str] = []

    def fake(host: str) -> list[str]:
        seen.append(host)
        return ["198.18.0.1"]

    code, report, text = _invoke(
        capsys,
        ["--qualification", "formal", "--mode", "system", "--egress-config", str(path)],
        EgressHooks(system_resolve=fake),
    )
    assert seen == ["api.etherscan.io"]
    assert code == 4, text
    assert report["status"] == "refused"
    assert report["reason"] == "benchmark_address"
    assert report["verdict"] == "BLOCKED_EGRESS"
    assert report["resolution"]["ips"] == []
    assert report["allow_private_network"] is False
    assert report["destination_policy_enforced"] is False
    assert "198.18.0.1" in report["error"]
    assert "doh" in report["error"]
    assert '"allow_private_network": true' not in text
    _assert_clean(text)

    code, report, text = _invoke(
        capsys,
        ["--qualification", "formal", "--mode", "system"],
        EgressHooks(system_resolve=public),
    )
    assert code == 4, text
    assert report["status"] == "ok"
    assert report["resolution"]["ips"] == [PUBLIC_V4]
    assert report["destination_policy_enforced"] is False
    assert report["verdict"] == "BLOCKED_EGRESS"


class _Body:
    status_code = 200
    headers: dict[str, str] = {}

    def iter_bytes(self):
        yield b"{}"

    def __enter__(self):
        return self

    def __exit__(self, *_args: object) -> bool:
        return False


def _patch_client(monkeypatch: pytest.MonkeyPatch) -> tuple[list[dict], list[dict]]:
    created: list[dict] = []
    streams: list[dict] = []

    class _Client:
        def __init__(self, **kwargs: object) -> None:
            created.append(kwargs)

        def stream(self, method: str, url: str, **kwargs: object) -> _Body:
            streams.append({"method": method, "url": url, **kwargs})
            return _Body()

        def close(self) -> None:
            return None

    monkeypatch.setattr(httpx, "Client", _Client)
    return created, streams


def test_mcm_share_s0_03_tls_and_pinned_connect(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    """MCM-SHARE-S0-03. Pin the validated IP, keep SNI, and refuse an unapproved hop or proxy."""
    path = _write(tmp_path, _DOH)
    created, streams = _patch_client(monkeypatch)
    calls = []

    def exchange(request):
        calls.append(request)
        host = urlsplit(request.url).hostname
        assert host in {"cloudflare-dns.com", "dns.google"}
        if host == "cloudflare-dns.com":
            raise TimeoutError("timed out")
        if request.query_type == "A":
            _assert_query(request, "dns.google", {"8.8.8.8", "8.8.4.4"})
            return _packet([_record("example.com.", 1, 60, PUBLIC_V4)])
        return _packet([])

    hooks = EgressHooks(exchange=exchange, exercise_pin=True)
    code, report, text = _invoke(
        capsys,
        ["--qualification", "preview", "--mode", "doh", "--egress-config", str(path)],
        hooks,
    )
    assert code == 0, text
    assert [urlsplit(item.url).hostname for item in calls] == [
        "cloudflare-dns.com",
        "cloudflare-dns.com",
        "dns.google",
        "dns.google",
    ]
    assert report["redirect_guard"]["rejected"] is True
    assert report["destination_policy_enforced"] is False
    assert all("evil.example" not in url for url in hooks.observed_urls)
    assert len(created) == 1
    assert created[0]["trust_env"] is False
    assert created[0]["verify"] is True
    assert created[0]["follow_redirects"] is False
    assert len(streams) == 1
    pinned = streams[0]
    assert urlsplit(str(pinned["url"])).hostname == PUBLIC_V4
    assert pinned["headers"]["host"] == "example.com"
    assert pinned["extensions"] == {"sni_hostname": "example.com"}
    _assert_clean(text)

    redirected: list[str] = []

    def redirect(request):
        redirected.append(request.url)
        return DohResponse(302, {"location": "https://evil.example/dns-query?name=example.com&type=A"}, b"")

    code, report, text = _invoke(
        capsys,
        ["--qualification", "preview", "--mode", "doh", "--egress-config", str(path)],
        EgressHooks(exchange=redirect),
    )
    assert code == 4, text
    assert report["reason"] == "doh_redirect_unapproved"
    assert len(redirected) == 1
    assert urlsplit(redirected[0]).hostname == "cloudflare-dns.com"
    assert "evil.example" not in redirected[0]

    secret = "synthetic-secret-s0-03"
    monkeypatch.setenv("HTTPS_PROXY", f"http://user:{secret}@127.0.0.1:9")
    before = len(created)
    code, report, text = _invoke(
        capsys,
        ["--qualification", "formal", "--mode", "doh", "--egress-config", str(path)],
        EgressHooks(exchange=exchange, exercise_pin=True),
    )
    assert code == 4, text
    assert report["status"] == "refused"
    assert report["reason"] == "ambient_proxy"
    assert secret not in text
    assert len(created) == before

    monkeypatch.setenv("GUARDIAN_EGRESS_AMBIENT_PROXY", "direct")
    direct_calls = []

    def direct(request):
        direct_calls.append(request)
        if request.query_type == "A":
            return _packet([_record("example.com.", 1, 60, PUBLIC_V4)])
        return _packet([])

    code, report, text = _invoke(
        capsys,
        ["--qualification", "preview", "--mode", "doh", "--egress-config", str(path)],
        EgressHooks(exchange=direct, exercise_pin=True),
    )
    assert code == 0, text
    assert report["destination_policy_enforced"] is False
    assert secret not in text
    assert created[-1]["trust_env"] is False
    assert "127.0.0.1" not in json.dumps(created[-1])
    assert urlsplit(direct_calls[0].url).hostname == "cloudflare-dns.com"


def test_mcm_share_s0_04_evidence_fields_separated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    """MCM-SHARE-S0-04. The four fields travel on the run report. trust_env=False is not enforcement."""
    forced = egress_evidence(
        resolver_verified=True,
        tls_hostname_verified=True,
        destination_policy_enforced=True,
        enforcement_location="unproven",
        trust_env=False,
    )
    assert forced["destination_policy_enforced"] is False
    assert forced["enforcement_location"] == "unproven"

    live = _live_helpers()
    desk = live._desk(tmp_path, monkeypatch)
    monkeypatch.delenv("GUARDIAN_RESOLVER_MODE", raising=False)
    code = main(["run-once", "--mcm-config", str(desk.config), "--no-send"], backend=desk.backend)
    text = capsys.readouterr().out
    report = json.loads(text)
    assert code == 0, text
    assert report.get("status") != "error"
    _fields(report["egress"])
    _fields(report["budget"]["egress"])
    assert report["egress"]["destination_policy_enforced"] is False
    assert report["egress"]["enforcement_location"] == "unproven"
    assert report["egress"]["tls_hostname_verified"] is False
    assert report["egress"]["resolver_verified"] is False
    assert report["budget"]["egress"] == report["egress"]
    assert report["coverage"]
    for row in report["coverage"]:
        assert "destination_policy_enforced" not in row
        assert "resolver_verified" not in row
        assert "tls_hostname_verified" not in row
        assert "enforcement_location" not in row
    blob = json.dumps(report)
    assert live.ADDRESS_A.lower() not in blob.lower()
    assert live.KEY not in blob

    created, streams = _patch_client(monkeypatch)
    path = _write(tmp_path, {"mode": "system", "probe_name": "example.com"})

    def public(_host: str) -> list[str]:
        return [PUBLIC_V4]

    code, doctor, text = _invoke(
        capsys,
        ["--qualification", "preview", "--mode", "system", "--egress-config", str(path)],
        EgressHooks(system_resolve=public, exercise_pin=True),
    )
    assert code == 0, text
    assert created[0]["trust_env"] is False
    assert streams
    assert doctor["destination_policy_enforced"] is False
    assert doctor["enforcement_location"] == "unproven"
    assert doctor["tls_hostname_verified"] is True
    _fields(doctor)


def test_mcm_share_s0_05_egress_doctor_verdict(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    """MCM-SHARE-S0-05. SNI routing blocks formal qualification. An enforcing network does not."""
    _plant(monkeypatch, tmp_path)
    document = dict(_DOH)
    document["probe_hosts"] = [
        {"hostname": "cloudflare-dns.com", "ip": "1.1.1.1"},
        {"hostname": "dns.google", "ip": "8.8.8.8"},
    ]
    path = _write(tmp_path, document)
    owners = {"1.1.1.1": "cloudflare-dns.com", "8.8.8.8": "dns.google"}

    def exchange(request):
        _assert_query(request, "cloudflare-dns.com", {"1.1.1.1", "1.0.0.1"})
        if request.query_type == "A":
            return _packet([_record("example.com.", 1, 30, PUBLIC_V4)])
        return _packet([])

    def sni_proxy(handshake: TlsHandshake) -> TlsOutcome:
        assert handshake.tls_verify is True
        assert handshake.verify_hostname == handshake.sni_hostname
        seen.append(handshake)
        return TlsOutcome(verified_hostname=handshake.sni_hostname, presented_name=handshake.sni_hostname)

    def enforcing(handshake: TlsHandshake) -> TlsOutcome:
        seen.append(handshake)
        owner = owners[handshake.connect_ip]
        if handshake.sni_hostname == owner:
            return TlsOutcome(verified_hostname=owner, presented_name=owner)
        return TlsOutcome(presented_name=owner, error="hostname_mismatch")

    seen: list[TlsHandshake] = []
    code, report, text = _invoke(
        capsys,
        ["--qualification", "formal", "--mode", "doh", "--egress-config", str(path)],
        EgressHooks(exchange=exchange, tls=sni_proxy),
    )
    assert code == 4, text
    assert report["status"] == "ok"
    assert report["verdict"] == "BLOCKED_EGRESS"
    assert report["destination_policy_enforced"] is False
    assert report["enforcement_location"] == "sni_proxy"
    assert report["tls_hostname_verified"] is True
    assert report["resolver_verified"] is True
    assert report["allow_private_network"] is False
    assert any(item.connect_ip == "1.1.1.1" and item.sni_hostname == "dns.google" for item in seen)
    _assert_clean(text)

    seen.clear()
    code, report, text = _invoke(
        capsys,
        ["--qualification", "preview", "--mode", "doh", "--egress-config", str(path)],
        EgressHooks(exchange=exchange, tls=sni_proxy),
    )
    assert code == 0, text
    assert report["verdict"] == "OK"
    assert report["destination_policy_enforced"] is False
    assert report["enforcement_location"] == "sni_proxy"
    _assert_clean(text)

    seen.clear()
    code, report, text = _invoke(
        capsys,
        ["--qualification", "formal", "--mode", "doh", "--egress-config", str(path)],
        EgressHooks(exchange=exchange, tls=enforcing),
    )
    assert code == 0, text
    assert report["verdict"] == "OK"
    assert report["destination_policy_enforced"] is True
    assert report["enforcement_location"] == "destination_ip"
    assert report["tls_hostname_verified"] is True
    assert report["resolver_verified"] is True
    assert any(item.connect_ip == "1.1.1.1" and item.sni_hostname == "dns.google" for item in seen)
    _assert_clean(text)
    _fields(report)


def _live_helpers():
    name = "mcm_live_entrypoint_helpers"
    if name in sys.modules:
        return sys.modules[name]
    path = Path(__file__).resolve().parent / "test_mcm_live_entrypoint.py"
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec is not None and spec.loader is not None
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module
