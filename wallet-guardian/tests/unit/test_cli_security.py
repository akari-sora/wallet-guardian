"""CLI stays read-only and fails with JSON rather than a bare traceback."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def _run(args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "guardian.cli", *args],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )


def test_replay_refuses_send(tmp_path: Path):
    fixture = tmp_path / "send.json"
    fixture.write_text(json.dumps({"method": "eth_sendTransaction"}), encoding="utf-8")
    completed = _run(["replay", "--fixture", str(fixture), "--no-send"])
    payload = json.loads(completed.stdout)
    assert completed.returncode == 4
    assert payload["status"] == "error"
    assert "eth_sendTransaction" not in payload["error"] or "forbidden" in payload["error"] or "cannot" in payload["error"]


def test_arbitrary_intent_is_rejected(tmp_path: Path):
    intent = tmp_path / "intent.json"
    intent.write_text(json.dumps({"type": "arbitrary_contract_call", "calldata": "0x1234"}), encoding="utf-8")
    completed = _run(["action", "plan", "--intent-file", str(intent)])
    payload = json.loads(completed.stdout)
    assert completed.returncode == 4
    assert payload["status"] == "error"


def test_self_check_json(tmp_path: Path):
    completed = _run(["self-check", "--db", str(tmp_path / "self.sqlite")])
    payload = json.loads(completed.stdout)
    assert completed.returncode == 0
    assert payload["monitoring_mode"] == "RUN_ONCE"
    assert payload["agent_can_sign"] is False
    assert payload["webhook_receiver"] is False


def test_dry_run_alert_is_not_delivered():
    completed = _run(["alerts", "test", "--dry-run"])
    payload = json.loads(completed.stdout)
    assert completed.returncode == 0
    assert payload["delivered"] is False
    assert "費用" not in payload["text"] or "未知" in payload["text"] or "沒有送出" in payload["text"]
