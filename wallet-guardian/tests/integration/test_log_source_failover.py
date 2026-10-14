"""P0 log classification. A refusing source is tried once, then the next source.

Addresses are the synthetic desk address. No private key is used.
"""

from __future__ import annotations

import importlib.util
import json
import os
import socket
from pathlib import Path
from urllib.parse import urlsplit

import httpx
import pytest

from guardian.cli import main
from guardian.sources.fixtures import RawResponse

_SPEC = importlib.util.spec_from_file_location(
    "log_failover_support",
    Path(__file__).with_name("test_mcm_live_entrypoint.py"),
)
_LIVE = importlib.util.module_from_spec(_SPEC)
assert _SPEC is not None and _SPEC.loader is not None
_SPEC.loader.exec_module(_LIVE)

_desk = _LIVE._desk
_cursor = _LIVE._cursor


@pytest.fixture(autouse=True)
def _block_network(monkeypatch: pytest.MonkeyPatch) -> None:
    def blocked(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("network is disabled")

    monkeypatch.setattr(socket, "socket", blocked)
    monkeypatch.setattr(socket, "create_connection", blocked)
    monkeypatch.setattr(socket, "getaddrinfo", blocked)
    monkeypatch.setattr(httpx, "Client", blocked)


class _SplitLogs(_LIVE.ChainBackend):
    def __init__(self, refusal: str) -> None:
        super().__init__()
        self.head = 0
        self.refusal = refusal

    def send(self, call: object):
        url = str(getattr(call, "url", ""))
        body = getattr(call, "body", b"") or b""
        if body:
            payload = json.loads(body.decode("utf-8"))
            if payload.get("method") == "eth_getLogs" and self._refuse(url):
                host = urlsplit(url).hostname or ""
                params = payload.get("params") if isinstance(payload.get("params"), list) else []
                record = {"method": "eth_getLogs", "host": host}
                if params and isinstance(params[0], dict):
                    record["from_block"] = int(str(params[0].get("fromBlock", "0x0")), 16)
                    record["to_block"] = int(str(params[0].get("toBlock", "0x0")), 16)
                self.rpc.append(record)
                return RawResponse(200, {}, json.dumps({
                    "id": 1,
                    "jsonrpc": "2.0",
                    "error": {"code": -32701, "message": "specify an address"}
                    if self.refusal != "limit"
                    else {"code": -32005, "message": "limit exceeded"},
                }).encode())
        return super().send(call)

    def _refuse(self, url: str) -> bool:
        host = urlsplit(url).hostname or ""
        if self.refusal == "all":
            return True
        return host != "logs.example.invalid"


def _arm(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, refusal: str):
    desk = _desk(tmp_path, monkeypatch)
    document = json.loads(desk.config.read_text(encoding="utf-8"))
    document["sources"]["rpc-ethereum-logs"] = {
        "adapter": "evm_rpc",
        "budget_group": "rpc-ethereum-logs",
        "enabled": True,
        "endpoint_env": "RPC_ETHEREUM_LOGS_URL",
    }
    document["budgets"]["rpc-ethereum-logs"] = dict(document["budgets"]["rpc-ethereum-mainnet"])
    for route in document["routing"]:
        if route.get("network") == "ethereum-mainnet" and route.get("capability") in {
            "history.token",
            "permissions.standard",
        }:
            route["source_ids"] = ["rpc-ethereum-mainnet", "rpc-ethereum-logs"]
    desk.config.write_text(json.dumps(document), encoding="utf-8")
    secret_path = Path(os.environ["MONITOR_SECRET_PROFILE"])
    secrets = json.loads(secret_path.read_text(encoding="utf-8"))
    secrets["RPC_ETHEREUM_LOGS_URL"] = "https://logs.example.invalid/rpc"
    secret_path.write_text(json.dumps(secrets) + "\n", encoding="utf-8")
    desk.backend = _SplitLogs(refusal)
    return desk


def _run(capsys, desk) -> dict:
    code = main(
        ["run-once", "--mcm-config", str(desk.config), "--no-send"],
        backend=desk.backend,
        clock=lambda: 1_700_000_000.0,
    )
    payload = json.loads(capsys.readouterr().out)
    expected = 3 if payload.get("status") == "degraded" else 0
    assert code == expected, payload
    return payload


def _logs(backend, host: str) -> list[dict]:
    return [row for row in backend.rpc if row.get("method") == "eth_getLogs" and row.get("host") == host]


def _row(report: dict, capability: str) -> dict:
    return next(row for row in report["coverage"] if row.get("capability") == capability)


def test_filter_refusal_uses_the_next_source_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    desk = _arm(tmp_path, monkeypatch, "address")
    report = _run(capsys, desk)
    assert len(_logs(desk.backend, "ethereum.example.invalid")) <= 1
    assert _logs(desk.backend, "logs.example.invalid")
    row = _row(report, "history.fungible")
    assert row["quality"] == "complete_in_declared_scope"
    assert _cursor(desk, "history.fungible") is not None
    desk.backend.rpc.clear()
    again = _run(capsys, desk)
    assert _logs(desk.backend, "ethereum.example.invalid") == []
    assert _row(again, "history.fungible")["quality"] == "complete_in_declared_scope"


def test_method_disabled_uses_the_next_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    desk = _arm(tmp_path, monkeypatch, "limit")
    report = _run(capsys, desk)
    assert len(_logs(desk.backend, "ethereum.example.invalid")) <= 1
    assert _row(report, "history.fungible")["quality"] == "complete_in_declared_scope"


def test_every_log_source_refused_blocks_without_advancing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    desk = _arm(tmp_path, monkeypatch, "all")
    report = _run(capsys, desk)
    row = _row(report, "history.fungible")
    assert row["quality"] == "error"
    assert "filter_requires_address" in row["reason_codes"]
    assert "blocked" in row["reason_codes"]
    assert row["quality"] != "complete_in_declared_scope"
    assert "empty" not in row["reason_codes"]
    assert _cursor(desk, "history.fungible") is None
    assert len(_logs(desk.backend, "ethereum.example.invalid")) <= 1
    assert len(_logs(desk.backend, "logs.example.invalid")) <= 1
