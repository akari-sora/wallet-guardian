"""START-02. Profile-then-env matches doctor output and the request that is sent."""

from __future__ import annotations

import json
import socket
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
