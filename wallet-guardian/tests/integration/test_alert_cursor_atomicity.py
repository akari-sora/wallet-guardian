"""P0-1. A leaf commit that never reaches the round's alert commit is replayed.

Sockets stay blocked. Addresses are the synthetic desk addresses from the
live-entrypoint fixture. No private key is used.
"""

from __future__ import annotations

import fcntl
import importlib.util
import json
import os
import socket
from pathlib import Path

import httpx
import pytest

from guardian.cli import main
from guardian.monitoring import CapabilityCycle
from guardian.storage import Database

_SPEC = importlib.util.spec_from_file_location(
    "alert_cursor_support",
    Path(__file__).with_name("test_mcm_live_entrypoint.py"),
)
_LIVE = importlib.util.module_from_spec(_SPEC)
assert _SPEC is not None and _SPEC.loader is not None
_SPEC.loader.exec_module(_LIVE)

ADDRESS_A = _LIVE.ADDRESS_A
KEY = _LIVE.KEY
TENANT = _LIVE.TENANT
_cli = _LIVE._cli
_cursor = _LIVE._cursor
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


def test_interrupted_finish_replays_native_alert_without_rereading(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    """COMMIT A stores the transfer. COMMIT B does not run. The next round still alerts."""
    desk = _desk(tmp_path, monkeypatch)
    argv = ["run-once", "--mcm-config", str(desk.config), "--no-send"]
    first = _cli(capsys, desk, argv)
    assert first["baseline"] is True
    before = {row["payload"].get("kind") for row in _outbox(desk)}
    assert "native_eth" not in before

    desk.backend.show_material()
    cursor_before = _cursor(desk, "history.native_external")

    original_finish = CapabilityCycle.finish

    def interrupted(self, **kwargs):
        raise RuntimeError("commit_b_interrupted")

    monkeypatch.setattr(CapabilityCycle, "finish", interrupted)
    code = main(argv, backend=desk.backend, clock=lambda: 1_700_000_000.0)
    failed = json.loads(capsys.readouterr().out)
    assert code == 1
    assert failed["status"] == "error"
    assert "native_eth" not in {row["payload"].get("kind") for row in _outbox(desk)}
    advanced = _cursor(desk, "history.native_external")
    assert advanced is not None
    assert cursor_before is None or int(advanced) >= int(cursor_before)
    database = Database(desk.state / "monitor.sqlite")
    try:
        pending = database.list_pending_alert_candidates(TENANT)
    finally:
        database.close()
    assert pending

    monkeypatch.setattr(CapabilityCycle, "finish", original_finish)
    desk.backend.native_rows = []
    desk.backend.token_rows = []
    desk.backend.logs = []
    recovered = _cli(capsys, desk, argv)
    assert recovered["status"] != "failed"
    kinds = {row["payload"].get("kind") for row in _outbox(desk)}
    assert "native_eth" in kinds
    database = Database(desk.state / "monitor.sqlite")
    try:
        left = database.list_pending_alert_candidates(TENANT)
    finally:
        database.close()
    assert left == []
    blob = json.dumps(recovered)
    assert KEY not in blob
    assert ADDRESS_A.lower() not in blob.lower()


def test_overlapping_run_once_is_locked_and_does_not_move_cursor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    """A second run-once that cannot take the lock leaves the cursor where it was."""
    desk = _desk(tmp_path, monkeypatch)
    argv = ["run-once", "--mcm-config", str(desk.config), "--no-send"]
    _cli(capsys, desk, argv)
    cursor_before = _cursor(desk, "history.native_external")
    outbox_before = len(_outbox(desk))
    lock_path = desk.state / "monitor.sqlite.run-once.lock"
    descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        code = main(argv, backend=desk.backend, clock=lambda: 1_700_000_100.0)
        payload = json.loads(capsys.readouterr().out)
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)
    assert code == 2
    assert payload["error"] == "run_once_locked"
    assert _cursor(desk, "history.native_external") == cursor_before
    assert len(_outbox(desk)) == outbox_before
