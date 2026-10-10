"""START-02. Profile-then-env matches doctor output and the request that is sent."""

from __future__ import annotations

import json
import os
import socket
import ssl
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from guardian.credentials import resolve_named
from guardian.scan.drive import _secret
from guardian.service import doctor
from guardian.sources.budget import BudgetBook
from guardian.sources.etherscan import EtherscanClient
from guardian.sources.fixtures import RawResponse
from guardian.sources.retry import classify_response
from guardian.sources.transport import ReadOnlyTransport, build_endpoint_registry
from guardian.security.transport_policy import Operation

ROOT = Path(__file__).resolve().parents[2]
PROFILE_KEY = "synthetic-profile-key-START02-aa11"
ENV_KEY = "synthetic-env-key-START02-bb22"
NOW = 1_700_000_000.0


class Recording:
    def __init__(self) -> None:
        self.calls: list[object] = []

    def send(self, call: object) -> RawResponse:
        self.calls.append(call)
        return RawResponse(401, {}, b"{}")


def _monitor(path: Path) -> Path:
    document = json.loads((ROOT / "examples" / "monitor.nokeys.json").read_text(encoding="utf-8"))
    path.write_text(json.dumps(document), encoding="utf-8")
    return path


def _write_profile(path: Path, value: str) -> None:
    path.write_text(json.dumps({"ETHERSCAN_API_KEY": value, "BLOCKSCOUT_API_KEY": value}), encoding="utf-8")


def _environ(secret: Path, env_value: str) -> dict[str, str]:
    return {
        "MONITOR_SECRET_PROFILE": str(secret),
        "ETHERSCAN_API_KEY": env_value,
        "BLOCKSCOUT_API_KEY": env_value,
        "GUARDIAN_RESOLVER_MODE": "doh",
        "GUARDIAN_DOH_ENDPOINTS": "https://cloudflare-dns.com/dns-query,https://dns.google/resolve",
        "GUARDIAN_DOH_BOOTSTRAP": "cloudflare-dns.com=1.1.1.1,1.0.0.1;dns.google=8.8.8.8,8.8.4.4",
    }


@pytest.fixture(autouse=True)
def _offline(monkeypatch: pytest.MonkeyPatch) -> None:
    def blocked(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("network is disabled")

    monkeypatch.setattr(socket, "socket", blocked)
    monkeypatch.setattr(socket, "create_connection", blocked)
    monkeypatch.setattr(socket, "getaddrinfo", blocked)


def test_start02_profile_wins_on_doctor_url_and_header(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GUARDIAN_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    secret = tmp_path / "secrets.json"
    _write_profile(secret, PROFILE_KEY)
    config = _monitor(tmp_path / "monitor.json")
    pool = _environ(secret, ENV_KEY)
    for name, value in pool.items():
        monkeypatch.setenv(name, value)

    report = doctor(mcm_config=config, environ=pool)
    blob = json.dumps(report)
    assert PROFILE_KEY not in blob
    assert ENV_KEY not in blob
    assert "START02-aa11" not in blob
    assert "START02-bb22" not in blob
    assert "apikey=" not in blob.lower()
    assert report["offline"] is True
    assert report["connectivity"]["probed"] is False
    assert report["egress_qualification"]["qualified"] is False
    assert report["egress_qualification"]["note"] == "DoH enabled is not a qualified path"
    assert report["resolver"]["requested_mode"] == "doh"
    assert report["resolver"]["effective_mode"] == "doh"
    assert report["resolver"]["endpoints"] == ["cloudflare-dns.com", "dns.google"]
    etherscan = next(row for row in report["credentials"] if row["variable"] == "ETHERSCAN_API_KEY")
    assert etherscan == {
        "variable": "ETHERSCAN_API_KEY",
        "selected_from": "profile",
        "present": True,
        "shadowed": True,
    }
    for row in report["credentials"]:
        assert set(row) == {"variable", "selected_from", "present", "shadowed"}
        assert row["selected_from"] in {"profile", "env", "absent"}

    scan = SimpleNamespace(public_config=SimpleNamespace(document={
        "private_inputs": {"secret_profile_file_env": "MONITOR_SECRET_PROFILE"},
    }))
    assert _secret(scan, "ETHERSCAN_API_KEY") == PROFILE_KEY

    secrets = json.loads(secret.read_text(encoding="utf-8"))
    endpoints = build_endpoint_registry(
        {
            "sources": {
                "etherscan": {
                    "adapter": "etherscan_v2",
                    "budget_group": "etherscan",
                    "credential_env": "ETHERSCAN_API_KEY",
                    "endpoint_env": "ETHERSCAN_API_BASE",
                },
                "blockscout": {
                    "adapter": "blockscout",
                    "budget_group": "blockscout",
                    "credential_env": "BLOCKSCOUT_API_KEY",
                    "endpoint_env": "BLOCKSCOUT_API_BASE",
                },
            },
        },
        secrets={**secrets, "ETHERSCAN_API_BASE": "https://etherscan.example.invalid", "BLOCKSCOUT_API_BASE": "https://blockscout.example.invalid"},
        environ=pool,
    )
    book = BudgetBook.from_document(
        {
            "etherscan": {"max_concurrency": 1, "max_requests_per_day": 10, "max_rps": 10, "retry_attempts": 0},
            "blockscout": {"max_concurrency": 1, "max_requests_per_day": 10, "max_rps": 10, "retry_attempts": 0},
        },
        clock=lambda: NOW,
        sleep=lambda _seconds: None,
    )
    backend = Recording()
    transport = ReadOnlyTransport(endpoints, book, backend, resolve=lambda _host: ["1.1.1.1"])
    client = EtherscanClient(
        transport,
        "etherscan",
        credential=_secret(scan, "ETHERSCAN_API_KEY"),
        credential_env="ETHERSCAN_API_KEY",
        clock=lambda: NOW,
        sleep=lambda _seconds: None,
    )
    operation = client.operation(
        chain_id=1,
        module="account",
        action="txlist",
        address="0x0000000000000000000000000000000000000001",
        start_block=1,
        end_block=2,
        page=1,
        offset=1,
    )
    prepared = transport.encode("etherscan", operation)
    assert PROFILE_KEY in prepared.url
    assert ENV_KEY not in prepared.url
    assert operation.query["apikey"] == PROFILE_KEY

    header_operation = Operation(
        protocol="rest",
        adapter="blockscout",
        http_method="GET",
        path="/api",
        query={"module": "account", "action": "balance"},
        headers=("authorization",),
    )
    result = transport.request("blockscout", header_operation)
    assert result.status == 401
    assert classify_response(401, {}).retryable is False
    assert classify_response(401, {}).reason == "credential_rejected"
    assert len(backend.calls) == 1
    sent = dict(getattr(backend.calls[0], "credential_headers"))
    revealed = sent["authorization"].reveal()
    assert revealed == f"Bearer {PROFILE_KEY}"
    assert ENV_KEY not in revealed
    again = resolve_named("BLOCKSCOUT_API_KEY", secrets, pool)
    assert again.selected_from == "profile"
    assert again.reveal() == PROFILE_KEY
    assert again.shadowed is True


def test_start02_blank_profile_uses_env_and_absent_stays_absent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GUARDIAN_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    secret = tmp_path / "secrets.json"
    _write_profile(secret, "")
    config = _monitor(tmp_path / "monitor.json")
    pool = _environ(secret, ENV_KEY)
    monkeypatch.setenv("MONITOR_SECRET_PROFILE", str(secret))
    monkeypatch.setenv("ETHERSCAN_API_KEY", ENV_KEY)
    report = doctor(mcm_config=config, environ=pool)
    blob = json.dumps(report)
    assert ENV_KEY not in blob
    etherscan = next(row for row in report["credentials"] if row["variable"] == "ETHERSCAN_API_KEY")
    assert etherscan["selected_from"] == "env"
    assert etherscan["present"] is True
    assert etherscan["shadowed"] is False
    scan = SimpleNamespace(public_config=SimpleNamespace(document={
        "private_inputs": {"secret_profile_file_env": "MONITOR_SECRET_PROFILE"},
    }))
    assert _secret(scan, "ETHERSCAN_API_KEY") == ENV_KEY

    pool["ETHERSCAN_API_KEY"] = ""
    monkeypatch.setenv("ETHERSCAN_API_KEY", "")
    missing = doctor(mcm_config=config, environ=pool)
    row = next(item for item in missing["credentials"] if item["variable"] == "ETHERSCAN_API_KEY")
    assert row == {
        "variable": "ETHERSCAN_API_KEY",
        "selected_from": "absent",
        "present": False,
        "shadowed": False,
    }
    assert _secret(scan, "ETHERSCAN_API_KEY") == ""


def test_start02_doh_without_endpoints_is_not_a_qualified_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GUARDIAN_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    report = doctor(environ={"GUARDIAN_RESOLVER_MODE": "doh"})
    assert report["offline"] is True
    assert report["connectivity"]["probed"] is False
    assert report["resolver"]["requested_mode"] == "doh"
    assert report["resolver"]["effective_mode"] is None
    assert report["resolver"]["endpoints"] == []
    assert report["egress_qualification"]["qualified"] is False
    assert report["egress_qualification"]["note"] == "DoH enabled is not a qualified path"
    assert report["providers"] == []
    keyed = doctor(environ={
        "GUARDIAN_RESOLVER_MODE": "doh",
        "GUARDIAN_DOH_ENDPOINTS": "https://user:synthetic-query-secret-START02@cloudflare-dns.com/dns-query",
        "GUARDIAN_DOH_BOOTSTRAP": "cloudflare-dns.com=1.1.1.1",
    })
    keyed_blob = json.dumps(keyed)
    assert "synthetic-query-secret-START02" not in keyed_blob
    assert "user:synthetic" not in keyed_blob
    assert keyed["resolver"]["effective_mode"] is None
    assert keyed["egress_qualification"]["qualified"] is False
    assert "cloudflare-dns.com" in keyed["resolver"]["endpoints"]


class _Calls:
    def __init__(self) -> None:
        self.gets: list[dict[str, object]] = []
        self.posts: list[dict[str, object]] = []
        self.rpcs: list[dict[str, object]] = []


def _install_probe_fakes(monkeypatch: pytest.MonkeyPatch, calls: _Calls, *, status: int = 200) -> None:
    def fake_get(url: str, *, resolve=None, timeout: float = 30):
        calls.gets.append({"url": url, "resolve": resolve, "timeout": timeout})
        if "chainlist" in url:
            body = {"result": [{"chainname": "Ethereum"}]}
            return status, json.dumps(body) if status == 200 else "{}"
        return 200, "sdn"

    def fake_post(url: str, payload: dict, *, timeout: float = 20, resolve=None):
        calls.posts.append({"url": url, "resolve": resolve, "timeout": timeout})
        return 200, {"ok": True}

    class FakeRpc:
        def __init__(self, url: str, *, client=None, timeout: float = 20, resolve=None) -> None:
            calls.rpcs.append({"url": url, "resolve": resolve, "timeout": timeout})

        def call(self, method: str, params: list, *, allow_estimate: bool = False):
            if method == "eth_chainId":
                return "0x38"
            if method == "eth_getBlockByNumber":
                return {"number": "0x1"}
            raise AssertionError(method)

    monkeypatch.setattr("guardian.service.safe_get_text", fake_get)
    monkeypatch.setattr("guardian.service.safe_post_json", fake_post)
    monkeypatch.setattr("guardian.service.RpcClient", FakeRpc)


def _chainlist_urls(calls: _Calls) -> list[str]:
    return [str(item["url"]) for item in calls.gets if "chainlist" in str(item["url"])]


def test_start_fix_live_probe_sends_the_profile_credential(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Profile P wins over environment E, and that same value is what the probe sends."""
    monkeypatch.setenv("GUARDIAN_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("ETHERSCAN_API_KEY", ENV_KEY)
    secret = tmp_path / "secrets.json"
    _write_profile(secret, PROFILE_KEY)
    config = _monitor(tmp_path / "monitor.json")
    pool = _environ(secret, ENV_KEY)
    calls = _Calls()
    _install_probe_fakes(monkeypatch, calls)
    report = doctor(live=True, mcm_config=config, environ=pool)
    blob = json.dumps(report)
    assert PROFILE_KEY not in blob
    assert ENV_KEY not in blob
    etherscan = next(row for row in report["credentials"] if row["variable"] == "ETHERSCAN_API_KEY")
    assert etherscan["selected_from"] == "profile"
    assert etherscan["shadowed"] is True
    urls = _chainlist_urls(calls)
    assert urls == [urls[0]]
    assert PROFILE_KEY in urls[0]
    assert ENV_KEY not in urls[0]
    probe = next(row for row in report["providers"] if row["source_id"] == "etherscan_v2_chainlist")
    assert probe["queried"] is True
    assert probe["level"] == "LIVE_PASS"
    assert report["live_probe"]["kind"] == "legacy_provider_probe"
    assert report["live_probe"]["proves_selected_profile_connectivity"] is False
    assert "does not prove the selected profile's connectivity" in report["live_probe"]["note"]
    assert report["connectivity"]["timeout_seconds"] == 5
    assert report["connectivity"]["timeout_kind"] == "whole_call"
    resolve = calls.gets[0]["resolve"]
    assert resolve is calls.posts[0]["resolve"]
    assert resolve is calls.rpcs[0]["resolve"]
    settings = getattr(resolve, "__self__").settings
    assert settings.mode == "doh"
    assert [item.host for item in settings.endpoints] == ["cloudflare-dns.com", "dns.google"]


def test_start_fix_profile_only_probe_queries_and_401_does_not_switch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GUARDIAN_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("ETHERSCAN_API_KEY", ENV_KEY)
    secret = tmp_path / "secrets.json"
    _write_profile(secret, PROFILE_KEY)
    config = _monitor(tmp_path / "monitor.json")
    pool = _environ(secret, "")
    pool.pop("ETHERSCAN_API_KEY", None)
    calls = _Calls()
    _install_probe_fakes(monkeypatch, calls)
    report = doctor(live=True, mcm_config=config, environ=pool)
    etherscan = next(row for row in report["credentials"] if row["variable"] == "ETHERSCAN_API_KEY")
    assert etherscan["selected_from"] == "profile"
    assert etherscan["present"] is True
    assert etherscan["shadowed"] is False
    urls = _chainlist_urls(calls)
    assert len(urls) == 1
    assert PROFILE_KEY in urls[0]
    assert ENV_KEY not in urls[0]
    probe = next(row for row in report["providers"] if row["source_id"] == "etherscan_v2_chainlist")
    assert probe["queried"] is True

    rejected = _Calls()
    _install_probe_fakes(monkeypatch, rejected, status=401)
    failed = doctor(live=True, mcm_config=config, environ=pool)
    failed_urls = _chainlist_urls(rejected)
    assert failed_urls == [failed_urls[0]]
    assert PROFILE_KEY in failed_urls[0]
    assert ENV_KEY not in failed_urls[0]
    failed_probe = next(row for row in failed["providers"] if row["source_id"] == "etherscan_v2_chainlist")
    assert failed_probe["queried"] is True
    assert failed_probe["http"] == 401
    assert failed_probe["level"] == "LIVE_FAIL"
    again = next(row for row in failed["credentials"] if row["variable"] == "ETHERSCAN_API_KEY")
    assert again["selected_from"] == "profile"


def test_start_fix_probe_deadline_is_wall_clock() -> None:
    from guardian.service import _call_with_deadline

    gate = threading.Event()

    def slow() -> dict[str, str]:
        gate.wait(30)
        return {"level": "LIVE_PASS"}

    try:
        with pytest.raises(TimeoutError, match="probe_deadline"):
            _call_with_deadline(slow, 0.2)
    finally:
        gate.set()


def test_start_fix_invalid_resolver_mode_is_not_echoed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GUARDIAN_STATE_DIR", str(tmp_path / "state"))
    secret = "synthetic-path-key-STARTFIX"
    url = f"https://rpc.example.invalid/v2/{secret}?apikey={secret}"
    report = doctor(environ={"GUARDIAN_RESOLVER_MODE": url})
    blob = json.dumps(report)
    assert report["resolver"]["requested_mode"] == "invalid"
    assert report["resolver"]["effective_mode"] is None
    assert secret not in blob
    assert url not in blob
    assert "example.invalid" not in blob
    assert "apikey=" not in blob.lower()
    env = os.environ.copy()
    env["GUARDIAN_RESOLVER_MODE"] = url
    env["GUARDIAN_STATE_DIR"] = str(tmp_path / "cli-state")
    completed = subprocess.run(
        [sys.executable, "-m", "guardian.cli", "doctor"],
        cwd=ROOT,
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )
    cli_blob = completed.stdout + completed.stderr
    assert completed.returncode == 0, cli_blob
    payload = json.loads(completed.stdout)
    assert payload["resolver"]["requested_mode"] == "invalid"
    assert secret not in cli_blob
    assert url not in cli_blob
    assert "example.invalid" not in cli_blob


def test_start_fix_providers_verify_is_a_labelled_legacy_probe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    from guardian.cli import main

    monkeypatch.setenv("GUARDIAN_STATE_DIR", str(tmp_path / "state"))
    calls = _Calls()
    _install_probe_fakes(monkeypatch, calls)
    code = main(["providers", "verify"])
    payload = json.loads(capsys.readouterr().out)
    assert code == 0
    assert payload["probe_kind"] == "legacy_provider_probe"
    assert payload["proves_selected_profile_connectivity"] is False
    assert payload["note"] == "legacy probe; does not prove the selected profile's connectivity"
    assert isinstance(payload["providers"], list)
    assert any(row["source_id"] == "etherscan_v2_chainlist" for row in payload["providers"])


def _record_live_helpers(monkeypatch: pytest.MonkeyPatch) -> list[object]:
    ops: list[object] = []

    def fake_get(url: str, *, resolve=None, timeout: float = 30):
        ops.append(("GET", resolve, url))
        return 200, json.dumps({"result": [{"chainname": "Ethereum"}]})

    def fake_post(url: str, payload: dict, *, timeout: float = 20, resolve=None):
        ops.append(("POST", resolve, payload))
        return 200, {"ok": True}

    class RecordingRpc:
        def __init__(self, url: str, *, client=None, timeout: float = 20, resolve=None) -> None:
            ops.append(("rpc", resolve, url))

        def call(self, method: str, params: list, *, allow_estimate: bool = False):
            ops.append((method, None))
            if method == "eth_chainId":
                return "0x38"
            if method == "eth_getBlockByNumber":
                return {"number": "0x1"}
            raise AssertionError(method)

    monkeypatch.setattr("guardian.service.safe_get_text", fake_get)
    monkeypatch.setattr("guardian.service.safe_post_json", fake_post)
    monkeypatch.setattr("guardian.service.RpcClient", RecordingRpc)
    return ops


def test_start_fix_bad_doh_does_not_probe(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Requested DoH that cannot be loaded reports the resolver error and opens nothing."""
    monkeypatch.setenv("GUARDIAN_STATE_DIR", str(tmp_path / "state"))
    secret = "synthetic-query-secret-STARTFIX2"
    key = "synthetic-probe-key-STARTFIX2"
    cases = [
        {
            "GUARDIAN_RESOLVER_MODE": "doh",
            "ETHERSCAN_API_KEY": key,
        },
        {
            "GUARDIAN_RESOLVER_MODE": "doh",
            "GUARDIAN_DOH_ENDPOINTS": "https://cloudflare-dns.com/dns-query",
            "GUARDIAN_DOH_BOOTSTRAP": "cloudflare-dns.com=10.1.1.1",
            "ETHERSCAN_API_KEY": key,
        },
        {
            "GUARDIAN_RESOLVER_MODE": "doh",
            "GUARDIAN_DOH_ENDPOINTS": f"https://user:{secret}@cloudflare-dns.com/dns-query",
            "GUARDIAN_DOH_BOOTSTRAP": "cloudflare-dns.com=1.1.1.1",
            "ETHERSCAN_API_KEY": key,
        },
    ]
    for pool in cases:
        ops = _record_live_helpers(monkeypatch)
        report = doctor(live=True, environ=pool)
        blob = json.dumps(report)
        assert ops == []
        assert report["resolver"]["requested_mode"] == "doh"
        assert report["resolver"]["effective_mode"] is None
        assert report["resolver"]["error"]
        assert report["providers"] == []
        assert report["connectivity"]["probed"] is False
        assert secret not in blob
        assert key not in blob
        assert "user:" not in blob


class _CaptureStream:
    def __init__(self, body: bytes) -> None:
        self.buf = bytearray()
        self._in = (
            b"HTTP/1.1 200 OK\r\nContent-Length: "
            + str(len(body)).encode("ascii")
            + b"\r\nConnection: close\r\n\r\n"
            + body
        )
        self._pos = 0
        self.sni: str | None = None
        self.ssl_context: ssl.SSLContext | None = None

    def write(self, data: bytes, timeout: float | None = None) -> None:
        self.buf.extend(data)

    def read(self, max_bytes: int, timeout: float | None = None) -> bytes:
        chunk = self._in[self._pos : self._pos + max_bytes]
        self._pos += len(chunk)
        return chunk

    def close(self) -> None:
        return None

    def start_tls(
        self,
        ssl_context: ssl.SSLContext,
        server_hostname: str | None = None,
        timeout: float | None = None,
    ) -> "_CaptureStream":
        self.sni = server_hostname
        self.ssl_context = ssl_context
        return self

    def get_extra_info(self, info: str) -> None:
        return None


def test_start_fix_probe_helpers_connect_to_the_pinned_ip(monkeypatch: pytest.MonkeyPatch) -> None:
    """GET, POST, and JSON-RPC reach HTTPX/HTTPCore and connect to the validated IP."""
    from httpcore._backends.sync import SyncBackend

    from guardian.providers import RpcClient, safe_get_text, safe_post_json

    pin = "1.1.1.1"
    bodies = [
        b"ok-get",
        b'{"ok":true}',
        b'{"jsonrpc":"2.0","id":1,"result":"0x1"}',
    ]
    connects: list[dict[str, object]] = []

    def connect_tcp(self, host, port, timeout=None, local_address=None, socket_options=None):
        stream = _CaptureStream(bodies.pop(0))
        connects.append({"host": host, "port": port, "timeout": timeout, "stream": stream})
        return stream

    monkeypatch.setattr(SyncBackend, "connect_tcp", connect_tcp)
    resolve = lambda _host: [pin]

    status, text = safe_get_text("https://chain.example.invalid/v2/chainlist", resolve=resolve, timeout=2)
    assert (status, text) == (200, "ok-get")
    posted, body = safe_post_json(
        "https://info.example.invalid/info",
        {"type": "meta"},
        resolve=resolve,
        timeout=2,
    )
    assert posted == 200 and body == {"ok": True}
    result = RpcClient("https://bsc.example.invalid/rpc", resolve=resolve, timeout=2).call(
        "eth_chainId",
        [],
    )
    assert result == "0x1"
    assert bodies == []
    assert [item["host"] for item in connects] == [pin, pin, pin]
    assert [item["port"] for item in connects] == [443, 443, 443]
    hosts = ["chain.example.invalid", "info.example.invalid", "bsc.example.invalid"]
    for item, hostname in zip(connects, hosts, strict=True):
        stream = item["stream"]
        assert isinstance(stream, _CaptureStream)
        assert stream.sni == hostname
        assert stream.ssl_context is not None
        assert stream.ssl_context.check_hostname is True
        assert stream.ssl_context.verify_mode == ssl.CERT_REQUIRED
        written = bytes(stream.buf).lower()
        assert f"host: {hostname}".encode("ascii") in written
        assert pin.encode("ascii") == item["host"].encode("ascii")
        assert hostname.encode("ascii") not in item["host"].encode("ascii")


def test_start_fix_deadline_does_not_start_the_next_rpc(monkeypatch: pytest.MonkeyPatch) -> None:
    """A slow identity read that finishes after the deadline does not start the next RPC."""
    from guardian.service import provider_probes

    deadline_seconds = 0.25
    operations: list[str] = []
    timeouts: list[float] = []
    held_closes: list[float] = []
    released = threading.Event()

    class _Resp:
        def __init__(self, status: int, text: str) -> None:
            self.status_code = status
            self.text = text
            self.headers: dict[str, str] = {}

    class _Client:
        def __init__(self, **kwargs: object) -> None:
            timeout = kwargs.get("timeout")
            assert isinstance(timeout, (int, float))
            timeouts.append(float(timeout))
            self._held = False

        def request(self, method: str, url: str, content: bytes | None = None, **kwargs: object):
            payload: object = None
            if content:
                payload = json.loads(content)
            rpc_method = payload.get("method") if isinstance(payload, dict) else None
            if isinstance(rpc_method, str):
                operations.append(rpc_method)
            else:
                operations.append(method)
            if rpc_method == "eth_chainId":
                self._held = True
                released.wait(30)
                body = json.dumps({"jsonrpc": "2.0", "id": 1, "result": "0x38"})
                return _Resp(200, body)
            if rpc_method == "eth_getBlockByNumber":
                body = json.dumps({"jsonrpc": "2.0", "id": 1, "result": {"number": "0x1"}})
                return _Resp(200, body)
            if method == "GET":
                return _Resp(200, "sdn")
            return _Resp(200, json.dumps({"ok": True}))

        def close(self) -> None:
            if self._held and not released.is_set():
                held_closes.append(time.monotonic())

    monkeypatch.setattr("guardian.providers.httpx.Client", _Client)
    started = time.monotonic()
    try:
        report = provider_probes(
            live_timeout=5.0,
            resolve=lambda _host: ["1.1.1.1"],
            deadline_seconds=deadline_seconds,
        )
        elapsed = time.monotonic() - started
        bsc = next(row for row in report if row["source_id"] == "bsc_official_rpc")
        assert bsc["level"] == "LIVE_FAIL"
        assert "probe_deadline" in bsc["detail"]
        assert elapsed < 1.5
        assert "eth_chainId" in operations
        assert "eth_getBlockByNumber" not in operations
        assert held_closes
        assert timeouts
        assert all(item <= deadline_seconds for item in timeouts)
        released.set()
        time.sleep(0.4)
        assert operations.count("eth_getBlockByNumber") == 0
    finally:
        released.set()
