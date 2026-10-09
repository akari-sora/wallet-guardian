"""MCM-SHARE-02. The public CLI reads a Solana wallet through the real run-once path.

Fixture RPC only: no socket opens. Balances, token accounts, signature history,
and stake discovery all run through ``guardian.cli.main``. The addresses are
synthetic public values, never a user wallet.
"""

from __future__ import annotations

import json
import os
import socket
from pathlib import Path

import httpx
import pytest

from guardian.cli import main
from guardian.protocols.pins import STAKE_PROGRAM, STAKE_STAKER_OFFSET, STAKE_WITHDRAWER_OFFSET
from guardian.sources.fixtures import RawResponse

ROOT = Path(__file__).resolve().parents[2]
EXAMPLE = ROOT / "examples" / "monitor.nokeys.json"
WALLET_ID = "44444444-4444-4444-8444-444444444444"
OWNER = "GvcqXpXpdjeM9VjC3YS8a4HZjCMwY7tuqmtBJ1QoWovM"
STAKE_STAKER_ONLY = "PciinCKxTncBkCQhYVqFETwRDyoE36iY37YGbiNpqyZ"
STAKE_BOTH = "7bnVhGN9dJreAWit5z6d1FPwGkBCK8sH2Ybaoh1eQGx"
VOTE = "J1to3BCFP8M2EcgS2VWRRuUzGcZScpnBVtcFYfqHpyqV"
MAINNET_GENESIS = "5eykt4UsFv8P8NJdTREpY1vzqKqZKvdpKuc147dw2N9d"
LAMPORTS = 4_891_136
SOL_BALANCE = 192_426_844
TOKEN_PROGRAM = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"


@pytest.fixture(autouse=True)
def _block_network(monkeypatch: pytest.MonkeyPatch) -> None:
    def blocked(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("network is disabled")

    monkeypatch.setattr(socket, "socket", blocked)
    monkeypatch.setattr(socket, "create_connection", blocked)
    monkeypatch.setattr(socket, "getaddrinfo", blocked)
    monkeypatch.setattr(httpx, "Client", blocked)


def _stake_value(lamports: int, delegated: int) -> dict:
    return {
        "data": {
            "parsed": {
                "info": {
                    "meta": {"authorized": {"staker": OWNER, "withdrawer": OWNER}},
                    "stake": {"delegation": {
                        "activationEpoch": "1",
                        "deactivationEpoch": "18446744073709551615",
                        "stake": str(delegated),
                        "voter": VOTE,
                        "warmupCooldownRate": 0.25,
                    }},
                },
                "type": "delegated",
            },
            "program": "stake",
            "space": 200,
        },
        "executable": False,
        "lamports": lamports,
        "owner": STAKE_PROGRAM,
    }


class SolanaBackend:
    def __init__(self) -> None:
        self.calls: list[tuple[str, list]] = []
        self.stake_present = True
        self.fail_program_accounts = False

    def methods(self) -> list[str]:
        return [name for name, _params in self.calls]

    def send(self, call: object) -> RawResponse:
        payload = json.loads((getattr(call, "body", b"") or b"{}").decode("utf-8"))
        method = str(payload.get("method"))
        params = payload.get("params") if isinstance(payload.get("params"), list) else []
        self.calls.append((method, params))
        result = self._result(method, params)
        if result is _ERROR:
            body = {"jsonrpc": "2.0", "id": payload.get("id", 1), "error": {"code": -32000, "message": "fixture-failure"}}
        else:
            body = {"jsonrpc": "2.0", "id": payload.get("id", 1), "result": result}
        return RawResponse(200, {}, json.dumps(body).encode())

    def _result(self, method: str, params: list) -> object:
        context = {"apiVersion": "4.3.0", "slot": 454_000_000}
        if method == "getGenesisHash":
            return MAINNET_GENESIS
        if method == "getBalance":
            return {"context": context, "value": SOL_BALANCE}
        if method == "getSlot":
            return context["slot"]
        if method == "getTokenAccountsByOwner":
            return {"context": context, "value": []}
        if method == "getSignaturesForAddress":
            return []
        if method == "getProgramAccounts":
            if self.fail_program_accounts:
                return _ERROR
            if not self.stake_present:
                return []
            offset = params[1]["filters"][0]["memcmp"]["offset"]
            if offset == STAKE_STAKER_OFFSET:
                return [
                    {"pubkey": STAKE_STAKER_ONLY, "account": _stake_value(LAMPORTS, LAMPORTS - 2_282_880)},
                    {"pubkey": STAKE_BOTH, "account": _stake_value(2 * LAMPORTS, 2 * LAMPORTS - 2_282_880)},
                ]
            if offset == STAKE_WITHDRAWER_OFFSET:
                return [{"pubkey": STAKE_BOTH, "account": _stake_value(2 * LAMPORTS, 2 * LAMPORTS - 2_282_880)}]
            return []
        return []


_ERROR = object()


def _desk(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    document = json.loads(EXAMPLE.read_text(encoding="utf-8"))
    document["enabled_networks"] = ["solana-mainnet"]
    document["plugins"] = ["solana-stake"]
    document["routing"] = [row for row in document["routing"] if row["network"] == "solana-mainnet"]
    config = tmp_path / "monitor.json"
    config.write_text(json.dumps(document), encoding="utf-8")
    accounts = {"schema_version": "guardian.accounts.v1", "tenant_id": "local", "accounts": [{
        "address": OWNER, "family": "solana", "label": "sol-a", "networks": ["solana-mainnet"],
        "relationship": "watch_only", "wallet_id": WALLET_ID,
    }]}
    _private(tmp_path / "accounts.json", json.dumps(accounts))
    _private(tmp_path / "secrets.json", json.dumps({"RPC_SOLANA_MAINNET_URL": "https://solana.example.invalid/rpc"}))
    monkeypatch.setenv("MONITOR_ACCOUNTS_FILE", str(tmp_path / "accounts.json"))
    monkeypatch.setenv("MONITOR_SECRET_PROFILE", str(tmp_path / "secrets.json"))
    monkeypatch.setenv("MONITOR_STATE_DIR", str(tmp_path / "state"))
    for name in ("HELIUS_API_KEY", "ETHERSCAN_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    return config


def _private(path: Path, text: str) -> None:
    fd = os.open(path, os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(text + "\n")


def _run(capsys: pytest.CaptureFixture[str], config: Path, backend: SolanaBackend) -> dict:
    code = main(["run-once", "--mcm-config", str(config), "--no-send"], backend=backend)
    payload = json.loads(capsys.readouterr().out)
    assert code == 0, payload
    assert OWNER not in json.dumps(payload)
    assert payload["live_verified"] is False
    return payload


def _position(report: dict) -> dict:
    found = [row for row in report["positions"] if row["plugin_id"] == "solana_stake"]
    assert len(found) == 1
    return found[0]


def test_solana_cli_reads_balance_tokens_history_and_stake(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    """The wallet address alone finds its stake accounts as staker or withdrawer, once each."""
    config = _desk(tmp_path, monkeypatch)
    backend = SolanaBackend()
    report = _run(capsys, config, backend)
    assert report["status"] == "ok"
    assert report["native_reads"] == [{"network": "solana-mainnet", "raw_amount": str(SOL_BALANCE), "wallet_id": WALLET_ID}]
    methods = set(backend.methods())
    assert {"getGenesisHash", "getBalance", "getTokenAccountsByOwner", "getSignaturesForAddress", "getProgramAccounts"} <= methods
    offsets = sorted(
        params[1]["filters"][0]["memcmp"]["offset"]
        for name, params in backend.calls if name == "getProgramAccounts"
    )
    assert offsets == [STAKE_STAKER_OFFSET, STAKE_WITHDRAWER_OFFSET]
    assert all(params[0] == STAKE_PROGRAM for name, params in backend.calls if name == "getProgramAccounts")
    position = _position(report)
    assert position["status"] == "observed"
    receipts = {leg["identity"]: leg["raw_amount"] for leg in position["legs"] if leg["kind"] == "receipt"}
    assert receipts == {STAKE_STAKER_ONLY: LAMPORTS, STAKE_BOTH: 2 * LAMPORTS}
    assert "stake_accounts_not_provided" not in json.dumps(position)
    assert type(next(iter(receipts.values()))) is int
    write_methods = {"sendTransaction", "requestAirdrop", "simulateTransaction"}
    assert not write_methods & methods
    assert report.get("live_pass") is not True


def test_solana_cli_no_stake_is_none_observed_and_failure_is_not_zero(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    """No stake is an observed empty set. A failed read is unknown, never an empty stake."""
    config = _desk(tmp_path, monkeypatch)
    backend = SolanaBackend()
    backend.stake_present = False
    empty = _position(_run(capsys, config, backend))
    assert empty["status"] == "none_observed"
    assert empty["legs"] == []

    failing = SolanaBackend()
    failing.fail_program_accounts = True
    broken = _position(_run(capsys, config, failing))
    assert broken["status"] == "unknown"
    assert broken["legs"] == []
    assert broken["reason_codes"]
