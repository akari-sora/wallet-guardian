"""MCM-SHARE-02 A. Bitcoin Esplora through the real run-once CLI.

Fixture HTTP only: no socket opens. Balances and history are satoshi integers.
The addresses are synthetic public values, never a user wallet.
"""

from __future__ import annotations

import json
import os
import socket
from pathlib import Path
from urllib.parse import urlsplit

import httpx
import pytest

from guardian.cli import main
from guardian.sources.esplora import PAGE_CAP
from guardian.sources.fixtures import RawResponse
from guardian.storage import Database

ROOT = Path(__file__).resolve().parents[2]
EXAMPLE = ROOT / "examples" / "monitor.nokeys.json"
WALLET_ID = "55555555-5555-4555-8555-555555555555"
WATCH = "bc1qqgpqyqszqgpqyqszqgpqyqszqgpqyqsz4desz8"
OTHER = "bc1qqvpsxqcrqvpsxqcrqvpsxqcrqvpsxqcr5ac3gx"
TIP = 800000
TIP_HASH = "11" * 32
OUT_TX = "aa" * 32
IN_TX = "bb" * 32
MEMPOOL_TX = "cc" * 32
SHALLOW_TX = "dd" * 32
CONFIRMED = 110_000


@pytest.fixture(autouse=True)
def _block_network(monkeypatch: pytest.MonkeyPatch) -> None:
    def blocked(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("network is disabled")

    monkeypatch.setattr(socket, "socket", blocked)
    monkeypatch.setattr(socket, "create_connection", blocked)
    monkeypatch.setattr(socket, "getaddrinfo", blocked)
    monkeypatch.setattr(httpx, "Client", blocked)


def _tx(txid: str, height: int | None, vins: list, vouts: list, *, confirmed: bool) -> dict:
    status: dict = {"confirmed": confirmed}
    if height is not None:
        status["block_height"] = height
        status["block_hash"] = f"{height:064x}"
    return {"fee": 1000, "status": status, "txid": txid, "vin": vins, "vout": vouts}


def _vin(address: str, value: int) -> dict:
    return {"prevout": {"scriptpubkey_address": address, "value": value}}


def _vout(address: str, value: int) -> dict:
    return {"scriptpubkey_address": address, "value": value}


def _filler(number: int) -> dict:
    return _tx(
        f"{number:064x}",
        700000,
        [_vin(OTHER, 1)],
        [_vout(OTHER, 1)],
        confirmed=True,
    )


def _happy_pages() -> tuple[list, list]:
    outgoing = _tx(
        OUT_TX,
        799994,
        [_vin(WATCH, 150_000)],
        [_vout(WATCH, 110_000), _vout(OTHER, 39_000)],
        confirmed=True,
    )
    incoming = _tx(
        IN_TX,
        799980,
        [_vin(OTHER, 150_000)],
        [_vout(WATCH, 150_000)],
        confirmed=True,
    )
    return [outgoing, *(_filler(number) for number in range(1, 25))], [incoming]


def _stats(funded: int, spent: int, mempool_funded: int, mempool_spent: int) -> dict:
    return {
        "chain_stats": {"funded_txo_sum": funded, "spent_txo_sum": spent},
        "mempool_stats": {"funded_txo_sum": mempool_funded, "spent_txo_sum": mempool_spent},
    }


class EsploraBackend:
    def __init__(self, mode: str = "happy", *, status: int = 200) -> None:
        self.mode = mode
        self.status = status
        self.calls: list[tuple[str, str]] = []

    def send(self, call: object) -> RawResponse:
        method = str(getattr(call, "method", ""))
        url = str(getattr(call, "url", ""))
        self.calls.append((method, url))
        host = urlsplit(url).hostname or ""
        if host != "esplora.example.invalid":
            raise AssertionError(host)
        path = urlsplit(url).path
        if method != "GET" or "tx/push" in path or path.rstrip("/").endswith("/tx"):
            raise AssertionError(path)
        status, body = self._body(path)
        if isinstance(body, str):
            encoded = body.encode()
        else:
            encoded = json.dumps(body).encode()
        return RawResponse(status, {}, encoded)

    def _body(self, path: str) -> tuple[int, object]:
        if path.endswith("/blocks/tip/height"):
            return 200, str(TIP)
        if path.endswith("/blocks/tip/hash"):
            return 200, TIP_HASH
        marker = f"/address/{WATCH}"
        if path.endswith(marker):
            if self.mode == "http_error":
                return self.status, {"error": "fixture"}
            if self.mode == "capped":
                return 200, _stats(42, 0, 0, 0)
            if self.mode == "pending":
                return 200, _stats(1000, 0, 5000, 0)
            return 200, _stats(260_000, 150_000, 5000, 0)
        if path.endswith(f"{marker}/txs/mempool"):
            if self.mode == "pending":
                forced = _tx(
                    MEMPOOL_TX,
                    TIP,
                    [_vin(OTHER, 5000)],
                    [_vout(WATCH, 5000)],
                    confirmed=True,
                )
                return 200, [forced]
            return 200, []
        if "/txs/chain" in path:
            if self.mode == "capped":
                return 200, [_filler(number) for number in range(1, 26)]
            if self.mode == "pending":
                shallow = _tx(
                    SHALLOW_TX,
                    799999,
                    [_vin(OTHER, 1000)],
                    [_vout(WATCH, 1000)],
                    confirmed=True,
                )
                return 200, [shallow]
            first, second = _happy_pages()
            if path.endswith("/txs/chain"):
                return 200, first
            return 200, second
        raise AssertionError(path)


def _desk(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, address: str = WATCH) -> Path:
    document = json.loads(EXAMPLE.read_text(encoding="utf-8"))
    document["enabled_networks"] = ["bitcoin-mainnet"]
    document["plugins"] = []
    document["routing"] = [row for row in document["routing"] if row["network"] == "bitcoin-mainnet"]
    config = tmp_path / "monitor.json"
    config.write_text(json.dumps(document), encoding="utf-8")
    accounts = {"schema_version": "guardian.accounts.v1", "tenant_id": "local", "accounts": [{
        "address": address,
        "family": "bitcoin",
        "label": "btc-a",
        "networks": ["bitcoin-mainnet"],
        "relationship": "watch_only",
        "wallet_id": WALLET_ID,
    }]}
    _private(tmp_path / "accounts.json", json.dumps(accounts))
    _private(tmp_path / "secrets.json", json.dumps({"ESPLORA_API_URL": "https://esplora.example.invalid/api"}))
    monkeypatch.setenv("MONITOR_ACCOUNTS_FILE", str(tmp_path / "accounts.json"))
    monkeypatch.setenv("MONITOR_SECRET_PROFILE", str(tmp_path / "secrets.json"))
    monkeypatch.setenv("MONITOR_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.delenv("ESPLORA_API_URL", raising=False)
    return config


def _private(path: Path, text: str) -> None:
    fd = os.open(path, os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(text + "\n")


def _run(capsys: pytest.CaptureFixture[str], config: Path, backend: EsploraBackend) -> tuple[int, dict]:
    code = main(["run-once", "--mcm-config", str(config), "--no-send"], backend=backend)
    payload = json.loads(capsys.readouterr().out)
    blob = json.dumps(payload)
    assert WATCH not in blob
    assert OTHER not in blob
    if payload.get("status") != "error":
        assert payload["live_verified"] is False
        assert payload.get("live_pass") is not True
    return code, payload


def _coverage(report: dict, capability: str) -> dict:
    found = [
        row for row in report["coverage"]
        if row["network"] == "bitcoin-mainnet" and row["capability"] == capability
    ]
    assert len(found) == 1
    return found[0]


def _stored(tmp_path: Path) -> tuple[list[dict], list[dict]]:
    database = Database(tmp_path / "state" / "monitor.sqlite")
    try:
        return database.chain_events_for("local"), database.alerts_for("local")
    finally:
        database.close()


def _paths(backend: EsploraBackend) -> list[str]:
    return [urlsplit(url).path for _method, url in backend.calls]


def test_bitcoin_cli_balance_history_and_restart_dedup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    """Confirmed sats ignore mempool. A second run stores the same events once."""
    config = _desk(tmp_path, monkeypatch)
    backend = EsploraBackend()
    code, report = _run(capsys, config, backend)
    assert code == 0, report
    assert report["status"] == "ok"
    assert report["native_reads"] == [{"network": "bitcoin-mainnet", "raw_amount": str(CONFIRMED), "wallet_id": WALLET_ID}]
    balance = _coverage(report, "balances.native")
    history = _coverage(report, "history.native_external")
    assert balance["quality"] == "complete_in_declared_scope"
    assert balance["scope"]["declared"] == "address_set_not_hd_wallet"
    assert "mempool_funded_sats:5000" in balance["evidence_refs"]
    assert "mempool_spent_sats:0" in balance["evidence_refs"]
    assert history["scope"]["declared"] == "address_set_not_hd_wallet"
    assert history["scope"]["pagination_complete"] is True
    assert _coverage(report, "permissions.erc20")["support"] == "not_applicable"
    assert "no_tokens" in _coverage(report, "balances.fungible")["reason_codes"]
    assert "no_ordinals" in _coverage(report, "balances.nft")["reason_codes"]
    assert "no_runes" in _coverage(report, "history.fungible")["reason_codes"]
    assert "no_lightning" in _coverage(report, "positions")["reason_codes"]
    assert "no_xpub" in _coverage(report, "history.native_internal")["reason_codes"]
    assert _coverage(report, "positions")["quality"] != "complete_in_declared_scope"
    events, alerts = _stored(tmp_path)
    found = {(row["tx_id"], row["kind"], row["raw_amount"], row["finality"]) for row in events}
    assert found == {
        (OUT_TX, "asset_out", "150000", "confirmed"),
        (OUT_TX, "asset_in", "110000", "confirmed"),
        (IN_TX, "asset_in", "150000", "confirmed"),
    }
    assert all(row["kind"] != "fee" for row in events)
    assert all(row["raw_amount"] not in {"1000", "39000", "5000"} for row in events)
    assert all(row["finality"] != "finalized" for row in events)
    identities = [row["identity"] for row in events]
    assert len(identities) == len(set(identities)) == 3
    chain_paths = [path for path in _paths(backend) if "/txs/chain" in path]
    assert len(chain_paths) == 2
    assert chain_paths[1].endswith("/" + f"{24:064x}")
    assert all(method == "GET" for method, _url in backend.calls)

    again = EsploraBackend()
    code, second = _run(capsys, config, again)
    assert code == 0, second
    replayed, replay_alerts = _stored(tmp_path)
    assert [row["identity"] for row in replayed] == identities
    assert len(replay_alerts) == len(alerts)
    assert second["outbox_written"] == 0


def test_bitcoin_cli_page_cap_is_partial(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    """A full page at the cap is partial. The answered balance stays the confirmed number."""
    config = _desk(tmp_path, monkeypatch)
    backend = EsploraBackend("capped")
    code, report = _run(capsys, config, backend)
    assert code == 0, report
    assert report["native_reads"] == [{"network": "bitcoin-mainnet", "raw_amount": "42", "wallet_id": WALLET_ID}]
    history = _coverage(report, "history.native_external")
    assert history["quality"] == "partial"
    assert history["quality"] != "complete_in_declared_scope"
    assert history["scope"]["pagination_complete"] is False
    assert history["scope"]["cursor"]
    assert "page_cap" in history["reason_codes"]
    chain_paths = [path for path in _paths(backend) if "/txs/chain" in path]
    assert len(chain_paths) == PAGE_CAP
    events, _alerts = _stored(tmp_path)
    assert events == []


@pytest.mark.parametrize(("status", "reason"), [(429, "rate_limited"), (500, "http_5xx")])
def test_bitcoin_cli_http_failure_is_not_zero(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], status: int, reason: str) -> None:
    """429 and 500 stay blocked. They are not a zero balance and not a completed history."""
    config = _desk(tmp_path, monkeypatch)
    backend = EsploraBackend("http_error", status=status)
    code, report = _run(capsys, config, backend)
    assert code == 1, report
    assert report["status"] == "failed"
    assert report["native_reads"] == []
    blob = json.dumps(report)
    assert '"raw_amount": "0"' not in blob
    assert '"raw_amount": 0' not in blob
    balance = _coverage(report, "balances.native")
    history = _coverage(report, "history.native_external")
    assert balance["quality"] == "error"
    assert reason in balance["reason_codes"]
    assert "blocked" in balance["reason_codes"]
    assert history["quality"] == "error"
    assert reason in history["reason_codes"]
    assert not any("/txs/" in path for path in _paths(backend))
    stats = [path for path in _paths(backend) if path.endswith(f"/address/{WATCH}")]
    assert len(stats) == 3


def test_bitcoin_cli_mempool_and_shallow_stay_pending(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    """Mempool and fewer than 6 confirmations are observed pending, even if the payload says confirmed."""
    config = _desk(tmp_path, monkeypatch)
    backend = EsploraBackend("pending")
    code, report = _run(capsys, config, backend)
    assert code == 0, report
    assert report["native_reads"] == [{"network": "bitcoin-mainnet", "raw_amount": "1000", "wallet_id": WALLET_ID}]
    history = _coverage(report, "history.native_external")
    assert history["quality"] != "complete_in_declared_scope"
    assert "pending" in history["reason_codes"]
    events, _alerts = _stored(tmp_path)
    by_tx = {row["tx_id"]: row for row in events}
    assert by_tx[SHALLOW_TX]["finality"] == "observed"
    assert by_tx[SHALLOW_TX]["finality"] not in {"confirmed", "finalized"}
    assert by_tx[SHALLOW_TX]["block_number"] == "799999"
    assert by_tx[MEMPOOL_TX]["finality"] == "observed"
    assert by_tx[MEMPOOL_TX]["block_number"] is None
    assert by_tx[MEMPOOL_TX]["tx_status"] == "unknown"
    assert all(row["finality"] != "finalized" for row in events)


def test_bitcoin_cli_is_get_only(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    """The scan never POSTs and never calls broadcast."""
    config = _desk(tmp_path, monkeypatch)
    backend = EsploraBackend()
    code, report = _run(capsys, config, backend)
    assert code == 0, report
    assert backend.calls
    for method, url in backend.calls:
        path = urlsplit(url).path
        assert method == "GET"
        assert "tx/push" not in path
        assert not path.rstrip("/").endswith("/tx")
        assert method != "POST"


def test_bitcoin_cli_bad_address_makes_no_request(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    """A bad address is rejected before the transport is used."""
    config = _desk(tmp_path, monkeypatch, address="not-a-bitcoin-address")
    backend = EsploraBackend()
    code, report = _run(capsys, config, backend)
    assert code == 2, report
    assert report["status"] == "error"
    assert backend.calls == []
