"""SAFE-04. The installed CLI runs from a fresh environment with a fixture transport."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest
from eth_utils import to_checksum_address

ROOT = Path(__file__).resolve().parents[2]

ADDRESS = to_checksum_address("0x" + ("04" * 20))
SECRET = "synthetic-canary-SAFE04-9f3a7c2e"
_PROBE = r"""
import json
import os
import socket
import sys
import time
from pathlib import Path
from urllib.parse import urlsplit

import httpx

import guardian
from guardian.cli import main
from guardian.configuration import file_permission_bits
from guardian.resources import resource_path
from guardian.sources.fixtures import RawResponse


def blocked(*_args, **_kwargs):
    raise RuntimeError("network is disabled")

socket.socket = blocked
socket.create_connection = blocked
socket.getaddrinfo = blocked
httpx.Client = blocked

spec = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
home = Path(spec["home"])
private = Path(spec["private"])
os.environ["HOME"] = str(home)
os.environ["USERPROFILE"] = str(home)
os.environ["XDG_CONFIG_HOME"] = str(home / "config")
for name in (
    "MONITOR_ACCOUNTS_FILE",
    "MONITOR_DELIVERY_PROFILE",
    "MONITOR_SECRET_PROFILE",
    "MONITOR_STATE_DIR",
    "ETHERSCAN_API_KEY",
    "GOPLUS_API_KEY",
    "CHAINABUSE_API_KEY",
    "HELIUS_API_KEY",
):
    os.environ.pop(name, None)


class Backend:
    def __init__(self):
        self.calls = 0

    def send(self, call):
        self.calls += 1
        body = getattr(call, "body", b"") or b""
        url = str(getattr(call, "url", ""))
        host = (urlsplit(url).hostname or "").lower()
        if not body:
            payload = {"message": "No transactions found", "result": [], "status": "0"}
            return RawResponse(200, {}, json.dumps(payload).encode())
        request = json.loads(body)
        items = request if isinstance(request, list) else [request]

        def one(item):
            method = str(item.get("method") or "")
            if method == "eth_chainId":
                result = _chain(host)
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
            return {"id": item.get("id", 1), "jsonrpc": "2.0", "result": result}

        encoded = [one(item) for item in items] if isinstance(request, list) else one(request)
        return RawResponse(200, {}, json.dumps(encoded).encode())


def _chain(host):
    table = (
        ("bsc", "0x38"),
        ("arbitrum", "0xa4b1"),
        ("optimism", "0xa"),
        ("polygon", "0x89"),
        ("avalanche", "0xa86a"),
        ("base", "0x2105"),
    )
    for name, value in table:
        if name in host:
            return value
    return "0x1"


class Spy:
    def __init__(self):
        self.calls = 0

    def send(self, message):
        self.calls += 1
        raise AssertionError("sender invoked")


def run(argv, **kwargs):
    import io
    from contextlib import redirect_stdout

    buf = io.StringIO()
    with redirect_stdout(buf):
        code = main(argv, **kwargs)
    text = buf.getvalue()
    payload = json.loads(text) if text.strip() else {}
    return code, payload


answers = private.parent / "answers.json"
answers.write_text(json.dumps(spec["answers"]), encoding="utf-8")
code, first = run(["init", "--dir", str(private), "--from", str(answers), "--yes"])
assert code == 0, first
names = ("accounts.json", "monitor.json", "secret-profile.json", "env-paths.json", "delivery-profile.json")
before = {name: (private / name).read_bytes() for name in names}
code, second = run(["init", "--dir", str(private), "--from", str(answers), "--yes"])
assert code == 2, second
assert second["status"] == "refused"
assert second["wrote"] is False
after = {name: (private / name).read_bytes() for name in names}
assert before == after

for key, value in first["env"].items():
    os.environ[key] = value
for row in first["data_sharing"]:
    if row.get("enabled") is True and isinstance(row.get("endpoint_env"), str) and row["endpoint_env"]:
        os.environ[row["endpoint_env"]] = "https://{0}.example.invalid/rpc".format(row["source_id"])
config = json.loads((private / "monitor.json").read_text(encoding="utf-8"))
for source in config["sources"].values():
    name = source.get("credential_env")
    if isinstance(name, str):
        os.environ[name] = ""

backend = Backend()
spy = Spy()
started = time.perf_counter()
code, report = run(
    ["run-once", "--mcm-config", str(private / "monitor.json"), "--no-send"],
    backend=backend,
    sender=spy,
)
elapsed = time.perf_counter() - started
assert code == 0, report
assert spy.calls == 0
assert report["sent"] == 0
assert report.get("live_pass") is not True
assert int(report["native_reads"][0]["raw_amount"]) > 0
assert 0 < backend.calls < 2000
assert elapsed < 60
report_text = json.dumps(report)
assert spec["address"].lower() not in report_text.lower()
assert spec["secret"] not in report_text

schema = resource_path("schemas", "monitor.schema.json")
loaded = json.loads(schema.read_text(encoding="utf-8"))
assert loaded.get("type") == "object"
guardian_file = Path(guardian.__file__).resolve()
assert "site-packages" in guardian_file.parts
checkout = spec["checkout"].lower()
assert checkout not in str(guardian_file).lower()
package_blob = b""
for path in guardian_file.parent.rglob("*"):
    if path.is_file() and path.stat().st_size < 2_000_000:
        package_blob += path.read_bytes()
assert spec["address"].encode() not in package_blob
assert spec["secret"].encode() not in package_blob
accounts = (private / "accounts.json").read_text(encoding="utf-8")
assert spec["address"] in accounts
assert spec["secret"] not in accounts
assert "site-packages" not in str(private.resolve()).lower()
result = {
    "accounts_has_address": spec["address"] in accounts,
    "binding": report["runtime"]["binding"],
    "calls": backend.calls,
    "elapsed": elapsed,
    "guardian_file": str(guardian_file),
    "idempotent": True,
    "permission_bits": file_permission_bits(private / "accounts.json"),
    "raw_amount": report["native_reads"][0]["raw_amount"],
    "schema_type": loaded.get("type"),
    "sent": report["sent"],
    "spy_calls": spy.calls,
    "status": report["status"],
}
Path(spec["result"]).write_text(json.dumps(result), encoding="utf-8")
print(json.dumps(result))
"""


def _outside_checkout() -> Path:
    from guardian.configuration import path_inside_work_tree

    path = Path(tempfile.mkdtemp(prefix="wg-safe04-"))
    if path_inside_work_tree(path):
        shutil.rmtree(path, ignore_errors=True)
        raise RuntimeError("temporary directory is inside the git work tree")
    return path


def _child_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    # Minimal environment: no operator settings or credentials are inherited.
    env = {key: os.environ[key] for key in ("PATH", "SYSTEMROOT", "LANG", "LC_ALL", "UV_CACHE_DIR", "UV_PYTHON_INSTALL_DIR") if key in os.environ}
    if "UV_CACHE_DIR" not in env:
        cache = subprocess.run(["uv", "cache", "dir"], text=True, capture_output=True, check=False).stdout.strip()
        if cache:
            env["UV_CACHE_DIR"] = cache
    env["UV_OFFLINE"] = "1"
    if extra:
        env.update(extra)
    return env


def _run(
    command: list[str],
    *,
    cwd: Path,
    env: dict[str, str] | None = None,
    timeout: int = 180,
) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        command,
        cwd=cwd,
        env=env if env is not None else _child_env(),
        text=True,
        capture_output=True,
        timeout=timeout,
        check=False,
    )
    return result


def _venv_python(venv: Path) -> Path:
    if os.name == "nt":
        return venv / "Scripts" / "python.exe"
    return venv / "bin" / "python"


def _wheel(dist: Path) -> Path:
    wheels = sorted(dist.glob("*.whl"))
    assert len(wheels) == 1
    return wheels[0]


def test_mcm_safe_04_clean_installed_cli() -> None:
    """SAFE-04. A fresh install reads a nonzero balance and sends nothing."""
    work = _outside_checkout()
    try:
        dist = work / "dist"
        built = _run(
            [sys.executable, str(ROOT / "tools" / "build_release.py"), "--out-dir", str(dist)],
            cwd=ROOT,
            timeout=240,
        )
        if built.returncode != 0:
            blob = built.stderr + built.stdout
            if "network was disabled" in blob:
                pytest.skip("wheel build is not available offline")
            raise AssertionError(blob)
        wheel = _wheel(dist)
        venv = work / "venv"
        created = _run(["uv", "venv", "--python", sys.executable, "--offline", str(venv)], cwd=work)
        assert created.returncode == 0, created.stderr
        python = _venv_python(venv)
        constraints = dist / "constraints.txt"
        installed = _run(
            ["uv", "pip", "install", "--offline", "--python", str(python), "--constraints", str(constraints), str(wheel)],
            cwd=work,
        )
        # An offline install that cannot be satisfied is an unmet gate, never an online retry.
        assert installed.returncode == 0, "SAFE-04 gate unmet (pre-stage dependencies in the uv cache): " + installed.stderr
        home = work / "home"
        private = work / "private"
        home.mkdir()
        probe = work / "probe.py"
        probe.write_text(_PROBE, encoding="utf-8")
        spec_path = work / "spec.json"
        result_path = work / "result.json"
        spec_path.write_text(
            json.dumps({
                "address": ADDRESS,
                "answers": {
                    "accounts": [{
                        "address": ADDRESS,
                        "family": "evm",
                        "label": "synthetic-desk",
                        "networks": ["ethereum-mainnet"],
                        "relationship": "watch_only",
                    }],
                    "consent": True,
                    "key_mode": "none",
                    "plugins": [],
                    "tenant_id": "local",
                },
                "checkout": str(ROOT),
                "home": str(home),
                "private": str(private),
                "result": str(result_path),
                "secret": SECRET,
            }),
            encoding="utf-8",
        )
        env = _child_env({"HOME": str(home), "USERPROFILE": str(home), "WG_UNUSED_CANARY": SECRET})
        for key in (
            "MONITOR_ACCOUNTS_FILE",
            "MONITOR_DELIVERY_PROFILE",
            "MONITOR_SECRET_PROFILE",
            "MONITOR_STATE_DIR",
            "PYTHONPATH",
        ):
            env.pop(key, None)
        completed = _run([str(python), str(probe), str(spec_path)], cwd=work, env=env, timeout=180)
        assert completed.returncode == 0, completed.stderr + completed.stdout
        assert SECRET not in completed.stdout
        assert ADDRESS.lower() not in completed.stdout.lower()
        result = json.loads(result_path.read_text(encoding="utf-8"))
        assert result["binding"] == "installed"
        assert result["idempotent"] is True
        assert result["accounts_has_address"] is True
        assert result["schema_type"] == "object"
        assert result["sent"] == 0
        assert result["spy_calls"] == 0
        assert int(result["raw_amount"]) > 0
        assert 0 < result["calls"] < 2000
        assert result["elapsed"] < 60
        assert "site-packages" in Path(result["guardian_file"]).parts
        assert str(ROOT).lower() not in result["guardian_file"].lower()
        if os.name == "posix":
            assert result["permission_bits"] == 0o600
        else:
            assert result["permission_bits"] is None
        accounts = (private / "accounts.json").read_text(encoding="utf-8")
        secrets = (private / "secret-profile.json").read_text(encoding="utf-8")
        assert ADDRESS in accounts
        assert SECRET not in accounts
        assert SECRET not in secrets
        assert "site-packages" not in str(private.resolve()).lower()
    finally:
        shutil.rmtree(work, ignore_errors=True)
