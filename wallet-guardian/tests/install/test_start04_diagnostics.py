"""START-04. A fresh installed wheel's doctor, coverage, and export stay free of secrets."""

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
PROFILE_SECRET = "synthetic-profile-canary-START04-aa11"
ENV_SECRET = "synthetic-env-canary-START04-bb22"

_PROBE = r"""
import json
import os
import socket
import sys
from pathlib import Path

import httpx

from guardian.cli import main


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
os.environ["GUARDIAN_STATE_DIR"] = str(private / "state")
for name in (
    "MONITOR_ACCOUNTS_FILE",
    "MONITOR_DELIVERY_PROFILE",
    "MONITOR_SECRET_PROFILE",
    "MONITOR_STATE_DIR",
    "ETHERSCAN_API_KEY",
    "GOPLUS_API_KEY",
    "BLOCKSCOUT_API_KEY",
    "HELIUS_API_KEY",
    "NODEREAL_API_KEY",
    "ALCHEMY_API_KEY",
):
    os.environ.pop(name, None)


def run(argv):
    import io
    from contextlib import redirect_stdout

    buf = io.StringIO()
    with redirect_stdout(buf):
        code = main(argv)
    text = buf.getvalue()
    payload = json.loads(text) if text.strip() else {}
    return code, text, payload


answers = Path(spec["answers"])
code, _text, first = run(["init", "--dir", str(private), "--from", str(answers), "--yes"])
assert code == 0, first
for key, value in first["env"].items():
    os.environ[key] = value
secret_path = private / "secret-profile.json"
profile = json.loads(secret_path.read_text(encoding="utf-8"))
profile["ETHERSCAN_API_KEY"] = spec["profile_secret"]
secret_path.write_text(json.dumps(profile), encoding="utf-8")
os.environ["ETHERSCAN_API_KEY"] = spec["env_secret"]
os.environ["GUARDIAN_RESOLVER_MODE"] = "doh"
os.environ["GUARDIAN_DOH_ENDPOINTS"] = "https://cloudflare-dns.com/dns-query,https://dns.google/resolve"
os.environ["GUARDIAN_DOH_BOOTSTRAP"] = "cloudflare-dns.com=1.1.1.1,1.0.0.1;dns.google=8.8.8.8,8.8.4.4"
config = str(private / "monitor.json")
chunks = []
code, doctor_text, doctor = run(["doctor", "--mcm-config", config])
assert code == 0, doctor
chunks.append(doctor_text)
assert doctor["offline"] is True
assert doctor["connectivity"]["probed"] is False
assert doctor["egress_qualification"]["qualified"] is False
assert doctor["egress_qualification"]["note"] == "DoH enabled is not a qualified path"
assert doctor["resolver"]["requested_mode"] == "doh"
assert doctor["resolver"]["effective_mode"] == "doh"
assert doctor["resolver"]["endpoints"] == ["cloudflare-dns.com", "dns.google"]
row = next(item for item in doctor["credentials"] if item["variable"] == "ETHERSCAN_API_KEY")
assert row["selected_from"] == "profile"
assert row["present"] is True
assert row["shadowed"] is True
code, coverage_text, coverage = run(["coverage", "--mcm-config", config])
assert code == 0, coverage
chunks.append(coverage_text)
assert coverage["status"] == "ok"
assert coverage["live_verified"] is False
assert coverage["connectivity"]["probed"] is False
assert coverage["egress_qualification"]["qualified"] is False
coverage_row = next(item for item in coverage["credentials"] if item["variable"] == "ETHERSCAN_API_KEY")
assert coverage_row == row
code, export_text, exported = run(["export", "--public", "--mcm-config", config])
assert code == 0, exported
chunks.append(export_text)
assert exported["wallet_counts"]["by_family"]["evm"] == 1
public = "\n".join(chunks)
for needle in (spec["profile_secret"], spec["env_secret"], spec["address"], spec["address"].lower()):
    assert needle not in public
assert "apikey=" not in public.lower()
print(json.dumps({"status": "ok", "qualified": False, "offline": True}))
"""


def _outside_checkout() -> Path:
    from guardian.configuration import path_inside_work_tree

    path = Path(tempfile.mkdtemp(prefix="wg-start04-"))
    if path_inside_work_tree(path):
        shutil.rmtree(path, ignore_errors=True)
        raise RuntimeError("temporary directory is inside the git work tree")
    return path


def _child_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    # TEMP/TMP stay so uv can unpack a wheel. On Windows an env without them
    # writes under C:\WINDOWS and the install is denied. The values are
    # directory paths, not credentials.
    env = {
        key: os.environ[key]
        for key in (
            "PATH",
            "SYSTEMROOT",
            "LANG",
            "LC_ALL",
            "UV_CACHE_DIR",
            "UV_PYTHON_INSTALL_DIR",
            "TEMP",
            "TMP",
            "TMPDIR",
        )
        if key in os.environ
    }
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
    return subprocess.run(
        command,
        cwd=cwd,
        env=env if env is not None else _child_env(),
        text=True,
        capture_output=True,
        timeout=timeout,
        check=False,
    )


def _venv_python(venv: Path) -> Path:
    if os.name == "nt":
        return venv / "Scripts" / "python.exe"
    return venv / "bin" / "python"


def _wheel(dist: Path) -> Path:
    wheels = sorted(dist.glob("*.whl"))
    assert len(wheels) == 1
    return wheels[0]


def test_start04_installed_wheel_diagnostics_omit_secrets() -> None:
    """START-04. Doctor, coverage, and public export from an installed wheel leak nothing."""
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
        assert installed.returncode == 0, "START-04 gate unmet (pre-stage dependencies in the uv cache): " + installed.stderr
        home = work / "home"
        private = work / "private"
        home.mkdir()
        probe = work / "probe.py"
        probe.write_text(_PROBE, encoding="utf-8")
        answers = work / "answers.json"
        answers.write_text(json.dumps({
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
        }), encoding="utf-8")
        spec_path = work / "spec.json"
        spec_path.write_text(json.dumps({
            "address": ADDRESS,
            "answers": str(answers),
            "env_secret": ENV_SECRET,
            "home": str(home),
            "private": str(private),
            "profile_secret": PROFILE_SECRET,
        }), encoding="utf-8")
        env = _child_env({"HOME": str(home), "USERPROFILE": str(home)})
        completed = _run([str(python), str(probe), str(spec_path)], cwd=work, env=env, timeout=180)
        combined = completed.stdout + completed.stderr
        assert completed.returncode == 0, combined
        assert PROFILE_SECRET not in combined
        assert ENV_SECRET not in combined
        assert ADDRESS.lower() not in combined.lower()
        assert "apikey=" not in combined.lower()
    finally:
        shutil.rmtree(work, ignore_errors=True)
