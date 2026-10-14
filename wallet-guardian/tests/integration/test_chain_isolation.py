"""P0-2 / latency WP-1. One chain or a missing news file does not stop the round.

Addresses are the synthetic desk addresses. No private key is used.
"""

from __future__ import annotations

import importlib.util
import json
import os
import socket
from pathlib import Path

import httpx
import pytest

from guardian.cli import main
from guardian.errors import SecurityError
from guardian.monitoring import CapabilityCycle
from guardian.storage import Database

_SPEC = importlib.util.spec_from_file_location(
    "chain_isolation_support",
    Path(__file__).with_name("test_mcm_live_entrypoint.py"),
)
_LIVE = importlib.util.module_from_spec(_SPEC)
assert _SPEC is not None and _SPEC.loader is not None
_SPEC.loader.exec_module(_LIVE)

ADDRESS_A = _LIVE.ADDRESS_A
KEY = _LIVE.KEY
TENANT = _LIVE.TENANT
_desk = _LIVE._desk
_outbox = _LIVE._outbox


@pytest.fixture(autouse=True)
def _block_network(monkeypatch: pytest.MonkeyPatch) -> None:
    def blocked(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("network is disabled")

    monkeypatch.setattr(socket, "socket", blocked)
    monkeypatch.setattr(socket, "create_connection", blocked)
    monkeypatch.setattr(socket, "getaddrinfo", blocked)
    monkeypatch.setattr(httpx, "Client", blocked)


def _run(capsys, desk, extra: list[str] | None = None) -> tuple[int, dict]:
    argv = ["run-once", "--mcm-config", str(desk.config), "--no-send", *(extra or [])]
    code = main(argv, backend=desk.backend, clock=lambda: 1_700_000_000.0)
    payload = json.loads(capsys.readouterr().out)
    blob = json.dumps(payload)
    assert KEY not in blob
    assert ADDRESS_A.lower() not in blob.lower()
    return code, payload


def _chain(report: dict) -> list[tuple]:
    rows = []
    for row in report["coverage"]:
        if row.get("capability") == "exposure_news":
            continue
        rows.append((
            row.get("network"),
            row.get("capability"),
            row.get("quality"),
            tuple(row.get("reason_codes") or []),
        ))
    return sorted(rows, key=lambda item: (str(item[0]), str(item[1])))


def _news_reasons(report: dict) -> list[str]:
    found = []
    for row in report["coverage"]:
        if row.get("capability") == "exposure_news":
            found.extend(row.get("reason_codes") or [])
    return found


def test_missing_news_file_keeps_chain_coverage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    (tmp_path / "plain").mkdir()
    plain = _desk(tmp_path / "plain", monkeypatch)
    code_plain, plain_report = _run(capsys, plain)
    (tmp_path / "missing").mkdir()
    missing = _desk(tmp_path / "missing", monkeypatch)
    code, report = _run(capsys, missing, ["--news-input", str(tmp_path / "no-such-news.xml")])
    assert code != 1
    assert report["status"] != "failed"
    assert code == code_plain
    assert _chain(report) == _chain(plain_report)
    assert report["outbox_written"] == plain_report["outbox_written"]
    assert "news_unreadable" in _news_reasons(report)


def test_unreadable_news_inputs_are_gaps(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    (tmp_path / "dir").mkdir()
    desk = _desk(tmp_path / "dir", monkeypatch)
    code, report = _run(capsys, desk, ["--news-input", str(tmp_path)])
    assert code != 1 and report["status"] != "failed"
    assert "news_unreadable" in _news_reasons(report)

    locked = tmp_path / "locked.xml"
    locked.write_text("<rss></rss>", encoding="utf-8")
    os.chmod(locked, 0)
    (tmp_path / "mode").mkdir()
    desk = _desk(tmp_path / "mode", monkeypatch)
    code, report = _run(capsys, desk, ["--news-input", str(locked)])
    assert code != 1 and report["status"] != "failed"
    assert "news_unreadable" in _news_reasons(report)

    huge = tmp_path / "huge.xml"
    huge.write_bytes(b"x" * (2 * 1024 * 1024 + 1))
    (tmp_path / "huge").mkdir()
    desk = _desk(tmp_path / "huge", monkeypatch)
    code, report = _run(capsys, desk, ["--news-input", str(huge)])
    assert code != 1 and report["status"] != "failed"
    assert "news_too_large" in _news_reasons(report)


def test_garbage_news_file_is_unparseable_and_chain_rows_stay(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    (tmp_path / "plain").mkdir()
    plain = _desk(tmp_path / "plain", monkeypatch)
    _, plain_report = _run(capsys, plain)
    garbage = tmp_path / "garbage.xml"
    garbage.write_text("this is not a feed {{{", encoding="utf-8")
    (tmp_path / "garbage").mkdir()
    desk = _desk(tmp_path / "garbage", monkeypatch)
    code, report = _run(capsys, desk, ["--news-input", str(garbage)])
    assert code != 1 and report["status"] != "failed"
    reasons = _news_reasons(report)
    assert "unparseable" in reasons or "news_unparseable" in reasons
    assert _chain(report) == _chain(plain_report)
    assert report["outbox_written"] == plain_report["outbox_written"]


def test_security_error_on_one_chain_does_not_stop_the_other(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    desk = _desk(tmp_path, monkeypatch)
    accounts = Path(os.environ["MONITOR_ACCOUNTS_FILE"])
    document = json.loads(accounts.read_text(encoding="utf-8"))
    document["accounts"][0]["networks"] = ["ethereum-mainnet", "arbitrum-one"]
    accounts.write_text(json.dumps(document) + "\n", encoding="utf-8")
    import guardian.pipeline as pipeline

    original = pipeline._read_evm

    def _read(public_config, private_inputs, transport, account, network, *args, **kwargs):
        if network == "arbitrum-one":
            raise SecurityError("dns_failed", reason="dns_failed")
        return original(public_config, private_inputs, transport, account, network, *args, **kwargs)

    monkeypatch.setattr(pipeline, "_read_evm", _read)
    code, report = _run(capsys, desk)
    assert code != 1
    assert report["status"] != "failed"
    eth = [
        row for row in report["coverage"]
        if row.get("network") == "ethereum-mainnet"
    ]
    arb = [
        row for row in report["coverage"]
        if row.get("network") == "arbitrum-one"
    ]
    assert eth and all("dns_failed" not in (row.get("reason_codes") or []) for row in eth)
    assert any("dns_failed" in (row.get("reason_codes") or []) for row in arb)
    assert report["outbox_written"] >= 1


def test_bad_price_input_is_a_gap_and_chain_rows_stay(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    (tmp_path / "plain").mkdir()
    plain = _desk(tmp_path / "plain", monkeypatch)
    _, plain_report = _run(capsys, plain)
    (tmp_path / "missing").mkdir()
    missing = _desk(tmp_path / "missing", monkeypatch)
    code, report = _run(capsys, missing, ["--price-input", str(tmp_path / "no-such-prices.json")])
    assert code != 1 and report["status"] != "failed"
    assert "price_unreadable" in report["input_gaps"]
    assert _chain(report) == _chain(plain_report)
    assert report["outbox_written"] == plain_report["outbox_written"]

    (tmp_path / "dir").mkdir()
    desk = _desk(tmp_path / "dir", monkeypatch)
    code, report = _run(capsys, desk, ["--price-input", str(tmp_path)])
    assert code != 1 and report["status"] != "failed"
    assert "price_unreadable" in report["input_gaps"]

    locked = tmp_path / "locked.json"
    locked.write_text("[]", encoding="utf-8")
    os.chmod(locked, 0)
    (tmp_path / "mode").mkdir()
    desk = _desk(tmp_path / "mode", monkeypatch)
    code, report = _run(capsys, desk, ["--price-input", str(locked)])
    assert code != 1 and report["status"] != "failed"
    assert "price_unreadable" in report["input_gaps"]

    huge = tmp_path / "huge.json"
    huge.write_bytes(b"x" * (2 * 1024 * 1024 + 1))
    (tmp_path / "huge").mkdir()
    desk = _desk(tmp_path / "huge", monkeypatch)
    code, report = _run(capsys, desk, ["--price-input", str(huge)])
    assert code != 1 and report["status"] != "failed"
    assert "price_too_large" in report["input_gaps"]

    garbage = tmp_path / "garbage.json"
    garbage.write_text("this is not a price file {{{", encoding="utf-8")
    (tmp_path / "garbage").mkdir()
    desk = _desk(tmp_path / "garbage", monkeypatch)
    code, report = _run(capsys, desk, ["--price-input", str(garbage)])
    assert code != 1 and report["status"] != "failed"
    assert "price_unparseable" in report["input_gaps"]
    assert _chain(report) == _chain(plain_report)


def test_runtime_error_on_one_chain_does_not_stop_the_other(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    desk = _desk(tmp_path, monkeypatch)
    accounts = Path(os.environ["MONITOR_ACCOUNTS_FILE"])
    document = json.loads(accounts.read_text(encoding="utf-8"))
    document["accounts"][0]["networks"] = ["ethereum-mainnet", "arbitrum-one"]
    accounts.write_text(json.dumps(document) + "\n", encoding="utf-8")
    import guardian.pipeline as pipeline

    original = pipeline._read_evm

    def _read(public_config, private_inputs, transport, account, network, *args, **kwargs):
        if network == "arbitrum-one":
            raise RuntimeError("chain read failed")
        return original(public_config, private_inputs, transport, account, network, *args, **kwargs)

    monkeypatch.setattr(pipeline, "_read_evm", _read)
    code, report = _run(capsys, desk)
    assert code != 1
    assert report["status"] != "failed"
    eth = [row for row in report["coverage"] if row.get("network") == "ethereum-mainnet"]
    arb = [row for row in report["coverage"] if row.get("network") == "arbitrum-one"]
    isolated = {"balances.native", "history.native_external", "history.token", "permissions.standard"}
    assert all(
        "read_error" not in (row.get("reason_codes") or [])
        for row in eth
        if row.get("capability") in isolated
    )
    assert any(
        row.get("capability") in isolated and "read_error" in (row.get("reason_codes") or [])
        for row in arb
    )


def test_pending_alert_from_one_chain_survives_another_chain_failing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    """A leaf that already stored a candidate is replayed after the other chain fails."""
    desk = _desk(tmp_path, monkeypatch)
    accounts = Path(os.environ["MONITOR_ACCOUNTS_FILE"])
    document = json.loads(accounts.read_text(encoding="utf-8"))
    document["accounts"][0]["networks"] = ["ethereum-mainnet", "arbitrum-one"]
    accounts.write_text(json.dumps(document) + "\n", encoding="utf-8")
    code, report = _run(capsys, desk)
    assert code == (3 if report.get("status") == "degraded" else 0)
    desk.backend.show_material()
    import guardian.pipeline as pipeline

    original = pipeline._read_evm

    def _read(public_config, private_inputs, transport, account, network, *args, **kwargs):
        if network == "arbitrum-one":
            raise RuntimeError("chain read failed")
        return original(public_config, private_inputs, transport, account, network, *args, **kwargs)

    monkeypatch.setattr(pipeline, "_read_evm", _read)
    original_finish = CapabilityCycle.finish

    def interrupted(self, **kwargs):
        raise RuntimeError("commit_b_interrupted")

    monkeypatch.setattr(CapabilityCycle, "finish", interrupted)
    code, failed = _run(capsys, desk)
    assert code == 1
    assert failed["status"] == "error"
    assert "native_eth" not in {row["payload"].get("kind") for row in _outbox(desk)}
    database = Database(desk.state / "monitor.sqlite")
    try:
        pending = database.list_pending_alert_candidates(TENANT)
    finally:
        database.close()
    assert pending
    monkeypatch.setattr(pipeline, "_read_evm", original)
    monkeypatch.setattr(CapabilityCycle, "finish", original_finish)
    desk.backend.native_rows = []
    desk.backend.token_rows = []
    desk.backend.logs = []
    code, recovered = _run(capsys, desk)
    assert code == (3 if recovered.get("status") == "degraded" else 0)
    assert recovered["status"] != "failed"
    assert "native_eth" in {row["payload"].get("kind") for row in _outbox(desk)}
