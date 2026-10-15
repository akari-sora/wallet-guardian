"""R-4 writes the disclosure plan to stderr before the first request.

Synthetic addresses and fixture transport only. No live network.
"""

from __future__ import annotations

import importlib.util
import io
import json
import os
import stat
import sys
from pathlib import Path

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "relations_r4_helpers",
    Path(__file__).with_name("test_relations_r1.py"),
)
assert _SPEC is not None and _SPEC.loader is not None
r1 = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(r1)

ETH = "ethereum-mainnet"
TOKEN = "r4hostsecret"
HOST = TOKEN + ".example.invalid"


class _FirstSend:
    """Remember stderr as it stood when the first request was handed over."""

    def __init__(self, inner, stream: io.StringIO) -> None:
        self._inner = inner
        self._stream = stream
        self.at_first: str | None = None

    def add(self, predicate, response) -> None:
        self._inner.add(predicate, response)

    def send(self, call):
        if self.at_first is None:
            self.at_first = self._stream.getvalue()
        return self._inner.send(call)

    def __getattr__(self, name: str):
        return getattr(self._inner, name)


class _Blocked(io.StringIO):
    def write(self, text: str) -> int:
        if "disclosure_plan" in text:
            raise OSError("disclosure_blocked")
        return super().write(text)


def _clock() -> float:
    return r1.CLOCK


def _install(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, token: str | None = None):
    host = HOST if token else "eth.blockscout.example.invalid"
    sources, routes, secrets = r1.blockscout_profile(
        ETH, "blockscout-eth", "BLOCKSCOUT_ETH_URL", "https://" + host,
    )
    if token:
        secrets = dict(secrets)
        secrets["RELATIONS_PLAN_TOKEN"] = token
    return r1.install(
        tmp_path, monkeypatch,
        networks=[ETH], sources=sources, routes=routes, secrets=secrets,
        address=r1.SEED, family="evm", account_networks=[ETH],
    )


def _argv(config: Path, command: str, *extra: str) -> list[str]:
    if command == "inbound":
        return r1.inbound_argv(config, *extra, "--seed", r1.SEED, "--network", ETH)
    if command == "expand":
        return r1.expand_argv(
            config, *extra, "--payer", r1.PAYER, "--network", ETH, "--seed", r1.SEED,
        )
    raise AssertionError(command)


def _stderr(monkeypatch: pytest.MonkeyPatch) -> io.StringIO:
    stream = io.StringIO()
    monkeypatch.setattr(sys, "stderr", stream)
    return stream


def _object(text: str) -> dict:
    body = json.loads(text)
    assert isinstance(body, dict)
    return body


def _plan_line(text: str) -> dict:
    assert text.endswith("\n")
    assert text.count("\n") == 1
    parsed = json.loads(text)
    assert list(parsed) == ["disclosure_plan"]
    plan = parsed["disclosure_plan"]
    assert isinstance(plan, dict)
    return plan


def _run(capsys: pytest.CaptureFixture[str], argv: list[str], backend) -> tuple[int, dict, str]:
    code = r1.main(argv, backend=backend, clock=_clock)
    text = capsys.readouterr().out
    return code, _object(text), text


def _labels(state: Path) -> Path:
    path = state / "relations-labels.json"
    descriptor = os.open(path, os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o644)
    with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
        handle.write('{"labels":[],"schema_version":"guardian.relations.labels.v1"}\n')
    if os.name == "posix":
        os.chmod(path, 0o644)
    return path


@pytest.mark.parametrize("command", ["inbound", "expand"])
def test_yes_writes_one_redacted_plan_before_the_first_request(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    command: str,
) -> None:
    config = tmp_path / "monitor.json"
    backend = _install(tmp_path, monkeypatch, token=TOKEN)
    stderr = _stderr(monkeypatch)
    snap = _FirstSend(backend, stderr)
    r1.fill_tails(snap, [], [])
    code, body, text = _run(capsys, _argv(config, command, "--yes"), snap)
    assert code == 0, body
    assert body["status"] == "complete"
    assert snap.calls
    assert snap.at_first is not None
    assert snap.at_first == stderr.getvalue()
    disclosed = _plan_line(snap.at_first)
    assert list(disclosed) == ["note", "queries", "unqueried"]
    assert list(disclosed["queries"][0]) == ["addresses", "direction", "host", "network", "source_id"]
    assert disclosed["queries"][0]["host"] == "***." + "example.invalid"
    assert TOKEN not in snap.at_first
    assert "***" in snap.at_first
    assert "disclosure_plan" not in text
    assert TOKEN not in text
    assert body["plan"] == disclosed
    assert TOKEN in snap.calls[0].url
    dry_err = _stderr(monkeypatch)
    dry_backend = r1.PathBackend()
    dry_code, dry_body, dry_text = _run(capsys, _argv(config, command, "--dry-run"), dry_backend)
    assert dry_code == 0, dry_body
    assert dry_body["status"] == "dry_run"
    assert dry_body["reads"] == []
    assert dry_backend.calls == []
    assert "disclosure_plan" not in dry_err.getvalue()
    assert disclosed == dry_body["plan"]
    assert TOKEN not in dry_text
    assert "***" in dry_text


@pytest.mark.parametrize("command", ["inbound", "expand"])
def test_missing_yes_writes_no_disclosure_line(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    command: str,
) -> None:
    backend = _install(tmp_path, monkeypatch)
    stderr = _stderr(monkeypatch)
    r1.fill_tails(backend, [], [])
    code, body, _text = _run(capsys, _argv(tmp_path / "monitor.json", command), backend)
    assert code == 2
    assert body["status"] == "consent_required"
    assert body["reads"] == []
    assert backend.calls == []
    assert "disclosure_plan" not in stderr.getvalue()


@pytest.mark.parametrize("command", ["inbound", "expand"])
@pytest.mark.parametrize("yes", [False, True])
def test_dry_run_writes_no_disclosure_line(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    command: str,
    yes: bool,
) -> None:
    backend = _install(tmp_path, monkeypatch)
    stderr = _stderr(monkeypatch)
    r1.fill_tails(backend, [], [])
    extra = ("--dry-run", "--yes") if yes else ("--dry-run",)
    code, body, _text = _run(capsys, _argv(tmp_path / "monitor.json", command, *extra), backend)
    assert code == 0, body
    assert body["status"] == "dry_run"
    assert body["reads"] == []
    assert backend.calls == []
    assert "disclosure_plan" not in stderr.getvalue()


def test_max_nodes_writes_no_disclosure_line(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = _install(tmp_path, monkeypatch)
    stderr = _stderr(monkeypatch)
    r1.fill_tails(backend, [], [])
    code, body, _text = _run(capsys, r1.inbound_argv(
        tmp_path / "monitor.json", "--yes", "--max-nodes", "1", "--seed", r1.SEED, "--seed", r1.OTHER,
        "--network", ETH,
    ), backend)
    assert code == 2
    assert body["error_type"] == "ConfigError"
    assert body["error"] == "max_nodes"
    assert backend.calls == []
    assert "disclosure_plan" not in stderr.getvalue()


def test_bad_address_writes_no_disclosure_line(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = _install(tmp_path, monkeypatch)
    stderr = _stderr(monkeypatch)
    r1.fill_tails(backend, [], [])
    code, body, _text = _run(capsys, r1.inbound_argv(
        tmp_path / "monitor.json", "--yes", "--seed", "", "--network", ETH,
    ), backend)
    assert code == 2
    assert body["error_type"] == "ConfigError"
    assert body["error"] == "bad_address"
    assert backend.calls == []
    assert "disclosure_plan" not in stderr.getvalue()


def test_label_file_mode_writes_no_disclosure_line(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = _install(tmp_path, monkeypatch)
    path = _labels(tmp_path / "state")
    assert stat.S_IMODE(path.stat().st_mode) & 0o077
    stderr = _stderr(monkeypatch)
    r1.fill_tails(backend, [], [])
    code, body, _text = _run(capsys, _argv(tmp_path / "monitor.json", "inbound", "--yes"), backend)
    assert code == 2
    assert body["error_type"] == "ConfigError"
    assert body["error"] == "labels_file_mode"
    assert backend.calls == []
    assert "disclosure_plan" not in stderr.getvalue()


def test_stderr_oserror_sends_nothing(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = _install(tmp_path, monkeypatch)
    r1.fill_tails(backend, [], [])
    monkeypatch.setattr(sys, "stderr", _Blocked())
    code, body, _text = _run(capsys, _argv(tmp_path / "monitor.json", "inbound", "--yes"), backend)
    assert code == 1
    assert body["status"] == "error"
    assert body["error_type"] == "OSError"
    assert body["error"] == "disclosure_blocked"
    assert backend.calls == []
