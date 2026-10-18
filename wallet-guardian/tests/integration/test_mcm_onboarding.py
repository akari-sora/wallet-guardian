"""MCM-SHARE-01 S5. Real CLI, offline fixtures, synthetic addresses only."""

from __future__ import annotations

import hashlib
import json
import os
import re
import socket
import stat
from pathlib import Path
from urllib.parse import urlsplit

import httpx
import pytest

from guardian.cli import main
from guardian.sources.fixtures import RawResponse

ROOT = Path(__file__).resolve().parents[2]
README = ROOT / "README.md"
ADDRESS = "0x" + ("0" * 39) + "1"
FAKE_KEY = "synthetic-env-key-not-for-files"
_RECORDED_AT = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")


@pytest.fixture(autouse=True)
def _block_network(monkeypatch: pytest.MonkeyPatch) -> None:
    def blocked(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("network is disabled")

    monkeypatch.setattr(socket, "socket", blocked)
    monkeypatch.setattr(socket, "create_connection", blocked)
    monkeypatch.setattr(socket, "getaddrinfo", blocked)
    monkeypatch.setattr(httpx, "Client", blocked)


class _OfflineBackend:
    """Answers RPC from memory. A real socket is a test failure."""

    def __init__(self) -> None:
        self.calls: list[object] = []

    def send(self, call: object) -> RawResponse:
        self.calls.append(call)
        body = getattr(call, "body", b"") or b""
        if not body:
            payload = {"message": "No transactions found", "result": [], "status": "0"}
            return RawResponse(200, {}, json.dumps(payload).encode())
        request = json.loads(body)
        if isinstance(request, list):
            encoded = [_rpc_result(item) for item in request]
        else:
            encoded = _rpc_result(request)
        return RawResponse(200, {}, json.dumps(encoded).encode())


def _rpc_result(request: dict) -> dict:
    method = str(request.get("method") or "")
    if method == "eth_chainId":
        result: object = "0x1"
    elif method == "eth_blockNumber":
        result = "0x10"
    elif method == "eth_getBalance":
        result = "0x1"
    elif method == "eth_getBlockByNumber":
        result = {"hash": "0x" + ("11" * 32), "number": "0x10", "timestamp": "0x65000000"}
    elif method == "eth_getLogs":
        result = []
    elif method == "eth_call":
        result = "0x"
    else:
        result = "0x0"
    return {"id": request.get("id", 1), "jsonrpc": "2.0", "result": result}


def _answers(**overrides: object) -> dict:
    document = {
        "accounts": [
            {
                "address": ADDRESS,
                "family": "evm",
                "label": "synthetic-desk",
                "networks": ["ethereum-mainnet"],
                "relationship": "watch_only",
            }
        ],
        "consent": True,
        "key_mode": "none",
        "plugins": [],
        "tenant_id": "local",
    }
    document.update(overrides)
    return document


def _write_answers(path: Path, document: dict) -> Path:
    path.write_text(json.dumps(document), encoding="utf-8")
    return path


def _invoke(capsys: pytest.CaptureFixture[str], argv: list[str], *, backend: object | None = None) -> tuple[int, dict]:
    code = main(argv, backend=backend)
    payload = json.loads(capsys.readouterr().out)
    return code, payload


def _apply_private_env(monkeypatch: pytest.MonkeyPatch, payload: dict) -> None:
    for name, value in payload["env"].items():
        monkeypatch.setenv(name, value)


def _arm_endpoints(monkeypatch: pytest.MonkeyPatch, sharing: list[dict]) -> None:
    for row in sharing:
        if row.get("enabled") is not True:
            continue
        env_name = row.get("endpoint_env")
        if isinstance(env_name, str) and env_name:
            monkeypatch.setenv(env_name, f"https://{row['source_id']}.example.invalid/rpc")


def _drop_credentials(monkeypatch: pytest.MonkeyPatch, config: dict) -> None:
    for source in config["sources"].values():
        name = source.get("credential_env")
        if isinstance(name, str):
            # An empty value stays absent. It also blocks .env.local from refilling the name.
            monkeypatch.setenv(name, "")


def _readme_commands() -> list[list[str]]:
    commands: list[list[str]] = []
    fence = False
    language = ""
    for line in README.read_text(encoding="utf-8").splitlines():
        if line.startswith("```"):
            if not fence:
                language = line[3:].strip().lower()
                fence = True
            else:
                fence = False
                language = ""
            continue
        if not fence or language not in {"bash", "sh", "shell"}:
            continue
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.startswith("uv run "):
            stripped = stripped[len("uv run "):].strip()
        if stripped.startswith("guardian"):
            commands.append(stripped.split())
    return commands


def test_mcm_share_s5_01_no_key_init(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    """MCM-SHARE-S5-01. No-key init validates, and run-once --no-send stays offline."""
    private = tmp_path / "private"
    answers = _write_answers(tmp_path / "answers.json", _answers())
    code, payload = _invoke(capsys, ["init", "--dir", str(private), "--from", str(answers), "--yes"])
    assert code == 0, payload
    assert payload["status"] == "ok"
    assert payload["wrote"] is True
    assert payload["key_mode"] == "none"
    monitor_path = private / "monitor.json"
    config = json.loads(monitor_path.read_text(encoding="utf-8"))
    assert config["operator_consent"]["key_mode"] == "none"
    _apply_private_env(monkeypatch, payload)
    _arm_endpoints(monkeypatch, payload["data_sharing"])
    _drop_credentials(monkeypatch, config)
    code, validated = _invoke(capsys, ["config", "validate", "--mcm-config", str(monitor_path)])
    assert code == 0, validated
    assert validated["status"] == "ok"
    assert validated["wallets"] == 1
    hinted_sources = [row for row in validated["sources"] if row.get("blocked_reason") == "credential_missing"]
    assert hinted_sources
    assert all("docs/api-keys.md" in row["credential_hint"] for row in hinted_sources)
    assert any("ETHERSCAN_API_KEY" in row["credential_hint"] for row in hinted_sources)
    backend = _OfflineBackend()
    code, report = _invoke(
        capsys,
        ["run-once", "--mcm-config", str(monitor_path), "--no-send"],
        backend=backend,
    )
    assert code == (3 if report.get("status") == "degraded" else 0), report
    assert report["status"] in {"ok", "degraded"}
    assert report["status"] != "error"
    assert report["sent"] == 0
    assert report["delivery_status"] == "no_send"
    assert report["misconfigured"] is False
    assert report["live_verified"] is False
    assert report["indexer_disclosures"] == []
    assert report["native_reads"]
    assert report["native_reads"][0]["raw_amount"] == "1"
    hosts = {(urlsplit(str(getattr(call, "url", ""))).hostname or "") for call in backend.calls}
    assert any(host.startswith("rpc-ethereum-mainnet.") for host in hosts)
    assert not any("etherscan" in host or "blockscout" in host for host in hosts)
    code, coverage = _invoke(capsys, ["coverage", "--mcm-config", str(monitor_path)])
    assert code == 0, coverage
    assert coverage["status"] == "ok"
    missing = [row for row in coverage["rows"] if "credential_missing" in (row.get("reason_codes") or [])]
    assert missing
    for row in missing:
        assert row["credential_hint"]
        assert "docs/api-keys.md" in row["credential_hint"]
        assert row["quality"] != "error"
        assert row["quality"] != "complete_in_declared_scope"
    native = [row for row in coverage["rows"] if row["capability"] == "balances.native"]
    assert native
    assert native[0]["quality"] == "complete_in_declared_scope"


def test_mcm_share_s5_02_explicit_data_sharing(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """MCM-SHARE-S5-02. The summary names every source, and nothing is written without consent."""
    private = tmp_path / "private"
    answers = _answers()
    path = _write_answers(tmp_path / "answers.json", answers)
    code, payload = _invoke(capsys, ["init", "--dir", str(private), "--from", str(path)])
    assert code == 2
    assert payload["status"] == "consent_required"
    assert payload["wrote"] is False
    assert not (private / "monitor.json").exists()
    sharing = payload["data_sharing"]
    by_id = {row["source_id"]: row for row in sharing}
    assert "rpc-ethereum-mainnet" in by_id
    assert "etherscan" in by_id
    assert "goplus" in by_id
    assert "chainabuse" in by_id
    for row in sharing:
        assert row["recipient"]
        assert isinstance(row["sends_addresses"], bool)
        assert row["sends_hostnames"] is False
        assert "address" in row["data"].lower()
    assert by_id["rpc-ethereum-mainnet"]["sends_addresses"] is True
    assert by_id["etherscan"]["enabled"] is True
    assert "not sent" in by_id["etherscan"]["data"].lower()
    assert by_id["goplus"]["enabled"] is False
    assert by_id["goplus"]["sends_addresses"] is False
    assert by_id["goplus"]["third_party_user_address_lookup"] is True
    assert by_id["chainabuse"]["enabled"] is False
    assert by_id["chainabuse"]["sends_addresses"] is False
    assert payload["third_party_user_address_lookups"] == "off"

    denied = _write_answers(tmp_path / "denied.json", _answers(consent=False))
    code, refused = _invoke(capsys, ["init", "--dir", str(tmp_path / "denied"), "--from", str(denied), "--yes"])
    assert code == 2
    assert refused["wrote"] is False
    assert not (tmp_path / "denied" / "monitor.json").exists()

    code, written = _invoke(capsys, ["init", "--dir", str(private), "--from", str(path), "--yes"])
    assert code == 0, written
    assert written["wrote"] is True
    config = json.loads((private / "monitor.json").read_text(encoding="utf-8"))
    assert config["privacy"]["third_party_wallet_queries"] == "deny_until_local_consent"
    assert "goplus" not in config["sources"]
    assert "chainabuse" not in config["sources"]
    consent = config["operator_consent"]
    assert consent["data_sharing_acknowledged"] is True
    assert consent["third_party_user_address_lookups"] == "off"
    assert _RECORDED_AT.fullmatch(consent["recorded_at"])


def test_init_bitcoin_keyless_disclosure(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """The consent preview discloses address-set reads to the configured Esplora host."""
    private = tmp_path / "private"
    answers = _answers(accounts=[{
        "address": "bc1qqszqgpqyqszqgpqyqszqgpqyqszqgpqy5dl506",
        "family": "bitcoin",
        "label": "synthetic-bitcoin",
        "networks": ["bitcoin-mainnet"],
        "relationship": "watch_only",
    }])
    path = _write_answers(tmp_path / "answers.json", answers)
    code, payload = _invoke(capsys, ["init", "--dir", str(private), "--from", str(path)])
    assert code == 2
    assert payload["status"] == "consent_required"
    assert payload["wrote"] is False
    assert not private.exists()
    row = next(row for row in payload["data_sharing"] if row["adapter"] == "esplora")
    assert row["enabled"] is True
    assert row["credential_env"] is None
    assert row["sends_addresses"] is True
    assert row["endpoint_env"] == "BITCOIN_MAINNET_URL"
    assert row["recipient"] == "the Esplora host named by BITCOIN_MAINNET_URL"
    assert "Bitcoin addresses are sent" in row["data"]
    assert "keyless" in row["data"]
    assert "Xpubs are never sent" in row["data"]
    assert "not queried" not in row["data"]
    assert "credential_missing" not in row["data"]


def test_mcm_share_s5_03_private_perms(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    """MCM-SHARE-S5-03. Private files stay owner-only, and env key values are not written."""
    monkeypatch.setenv("ETHERSCAN_API_KEY", FAKE_KEY)
    monkeypatch.setenv("GOPLUS_API_KEY", FAKE_KEY)
    monkeypatch.setenv("RPC_ETHEREUM_MAINNET_URL", "https://rpc.example.invalid/" + FAKE_KEY)
    private = tmp_path / "private"
    answers = _write_answers(tmp_path / "answers.json", _answers())
    code, payload = _invoke(capsys, ["init", "--dir", str(private), "--from", str(answers), "--yes"])
    assert code == 0, payload
    out = json.dumps(payload)
    assert FAKE_KEY not in out
    texts = []
    files = [path for path in private.rglob("*") if path.is_file()]
    assert files
    for path in files:
        texts.append(path.read_text(encoding="utf-8"))
    assert FAKE_KEY not in "\n".join(texts)
    secrets = json.loads((private / "secret-profile.json").read_text(encoding="utf-8"))
    assert secrets["ETHERSCAN_API_KEY"] == ""
    assert all(value == "" for value in secrets.values())
    if os.name != "posix":
        return
    assert stat.S_IMODE(private.stat().st_mode) == 0o700
    assert stat.S_IMODE((private / "state").stat().st_mode) == 0o700
    for path in files:
        assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_mcm_share_s5_04_no_overwrite(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    """MCM-SHARE-S5-04. A rerun refuses; --force backs up config and leaves the state database alone."""
    monkeypatch.setenv("ETHERSCAN_API_KEY", FAKE_KEY)
    private = tmp_path / "private"
    first_answers = _write_answers(tmp_path / "first.json", _answers())
    code, payload = _invoke(capsys, ["init", "--dir", str(private), "--from", str(first_answers), "--yes"])
    assert code == 0, payload
    state = Path(payload["env"]["MONITOR_STATE_DIR"])
    database = state / "monitor.sqlite"
    body = b"synthetic-state-do-not-touch"
    database.write_bytes(body)
    digest = hashlib.sha256(body).hexdigest()
    code, refused = _invoke(capsys, ["init", "--dir", str(private), "--from", str(first_answers), "--yes"])
    assert code == 2
    assert refused["status"] == "refused"
    assert refused["wrote"] is False
    assert database.read_bytes() == body
    assert hashlib.sha256(database.read_bytes()).hexdigest() == digest
    assert not list(private.glob("*.bak-*"))
    assert "synthetic-desk" in (private / "accounts.json").read_text(encoding="utf-8")
    second = _answers()
    second["accounts"][0]["label"] = "synthetic-desk-two"
    second_answers = _write_answers(tmp_path / "second.json", second)
    code, forced = _invoke(
        capsys,
        ["init", "--dir", str(private), "--from", str(second_answers), "--yes", "--force"],
    )
    assert code == 0, forced
    assert forced["backups"]
    backup_name = next(item["backup"] for item in forced["backups"] if item["file"] == "accounts.json")
    backup_text = (private / backup_name).read_text(encoding="utf-8")
    assert "synthetic-desk" in backup_text
    assert "synthetic-desk-two" not in backup_text
    assert "synthetic-desk-two" in (private / "accounts.json").read_text(encoding="utf-8")
    assert database.read_bytes() == body
    assert hashlib.sha256(database.read_bytes()).hexdigest() == digest
    combined = "\n".join(path.read_text(encoding="utf-8") for path in private.rglob("*") if path.is_file())
    assert FAKE_KEY not in combined


def test_mcm_share_s5_05_no_auto_enable(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """MCM-SHARE-S5-05. Init does not install a service, enable a sender, or derive addresses."""
    private = tmp_path / "private"
    answers = _write_answers(tmp_path / "answers.json", _answers())
    code, payload = _invoke(capsys, ["init", "--dir", str(private), "--from", str(answers), "--yes"])
    assert code == 0, payload
    config = json.loads((private / "monitor.json").read_text(encoding="utf-8"))
    accounts = json.loads((private / "accounts.json").read_text(encoding="utf-8"))
    delivery = json.loads((private / "delivery-profile.json").read_text(encoding="utf-8"))
    assert config["plugins"] == []
    assert config["delivery"]["mode"] == "outbox_only"
    assert config["execution"]["sign"] is False
    assert config["execution"]["broadcast"] is False
    assert delivery["mode"] == "outbox_only"
    assert delivery["single_sender_handoff"] is False
    assert payload["sender_started"] is False
    assert "systemd" in payload["not_auto_enabled"]
    assert "address_derivation" in payload["not_auto_enabled"]
    assert len(accounts["accounts"]) == 1
    assert accounts["accounts"][0]["address"].lower() == ADDRESS.lower()
    assert accounts["accounts"][0]["networks"] == ["ethereum-mainnet"]
    assert not list(private.rglob("*.service"))
    assert not list(private.rglob("*.timer"))
    before = sorted(path.relative_to(private).as_posix() for path in private.rglob("*"))
    code, planned = _invoke(
        capsys,
        ["install-service", "--dry-run", "--mcm-config", str(private / "monitor.json")],
    )
    assert code == 1
    assert planned["dry_run"] is True
    assert planned["status"] in {"ok", "warn"}
    assert planned["installed"] is False
    assert planned["applied"] is False
    assert planned["sender_started"] is False
    assert planned["systemctl_called"] is False
    assert planned["fetches_code"] is False
    names = {unit["name"] for unit in planned["planned_units"]}
    assert names == {
        "guardian-mcm-shadow.service",
        "guardian-mcm-shadow.timer",
        "guardian-legacy-collector.service",
        "guardian-legacy-collector.timer",
        "guardian-watchdog.service",
        "guardian-watchdog.timer",
    }
    after = sorted(path.relative_to(private).as_posix() for path in private.rglob("*"))
    assert after == before
    commands = _readme_commands()
    rendered = [" ".join(command) for command in commands]
    assert any(item.startswith("guardian init ") for item in rendered)
    assert any("run-once" in item and "--no-send" in item for item in rendered)
    assert any(item.startswith("guardian coverage ") for item in rendered)
    assert any("install-service" in item and "--dry-run" in item for item in rendered)
    for command in commands:
        argv = command[1:]
        if "--help" not in argv and "-h" not in argv:
            argv = [*argv, "--help"]
        with pytest.raises(SystemExit) as caught:
            main(argv)
        assert int(caught.value.code or 0) == 0
        help_text = capsys.readouterr().out.lower()
        assert "usage" in help_text
        assert command[1] in help_text


def test_mcm_share_s5_06_example_answers_file_initializes(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """Review fix: the README points at a shipped example answers file; it must really work."""
    root = Path(__file__).resolve().parents[2]
    example = root / "examples" / "answers.example.json"
    assert example.is_file()
    assert json.loads(example.read_text(encoding="utf-8"))["accounts"][0]["address"] == "0x" + "0" * 39 + "1"
    private = tmp_path / "priv"
    code = main(["init", "--dir", str(private), "--from", str(example), "--yes"])
    capsys.readouterr()
    assert code == 0
    assert (private / "monitor.json").is_file()
    # POSIX records 0600. Windows does not enforce those bits (docs/install.md).
    if os.name == "posix":
        assert stat.S_IMODE((private / "monitor.json").stat().st_mode) == 0o600
