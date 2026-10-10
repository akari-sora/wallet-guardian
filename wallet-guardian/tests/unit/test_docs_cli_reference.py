"""Docs versus CLI: every flag in docs/cli-reference.md is a real guardian flag.

Offline. ``--help`` exits before a command runs. Sockets are blocked in this
process; the help child is only the argparse text.
"""

from __future__ import annotations

import os
import re
import socket
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
REFERENCE = ROOT / "docs" / "cli-reference.md"
_SECTION = re.compile(
    r"^## `guardian(?: ([^`]+))?`\n\n```text\n(.*?)\n```",
    re.M | re.S,
)
_FLAG = re.compile(r"(?<![\w-])(?:--[A-Za-z0-9][A-Za-z0-9-]*|-h)(?![\w-])")


@pytest.fixture(autouse=True)
def _offline(monkeypatch: pytest.MonkeyPatch) -> None:
    def blocked(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("network is disabled")

    monkeypatch.setattr(socket, "socket", blocked)
    monkeypatch.setattr(socket, "create_connection", blocked)
    monkeypatch.setattr(socket, "getaddrinfo", blocked)


def _help(args: list[str]) -> str:
    env = os.environ.copy()
    env["COLUMNS"] = "200"
    completed = subprocess.run(
        [sys.executable, "-m", "guardian.cli", *args, "--help"],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )
    assert completed.returncode == 0, completed.stderr
    text = completed.stdout.replace("\r\n", "\n")
    assert "usage:" in text.lower()
    return text


def _children(text: str) -> list[str]:
    joined = text.replace("\n", " ")
    start = joined.find("positional arguments:")
    if start < 0:
        return []
    end = joined.find("options:", start)
    segment = joined[start:end if end >= 0 else None]
    brace = segment.find("{")
    close = segment.find("}", brace + 1)
    if brace < 0 or close < 0:
        return []
    parts = [part.strip() for part in segment[brace + 1:close].split(",")]
    if not parts or any(not part.replace("-", "").isalnum() or not part[:1].islower() for part in parts):
        return []
    return parts


def _walk(prefix: list[str]) -> dict[tuple[str, ...], str]:
    text = _help(prefix)
    found = {tuple(prefix): text}
    for name in _children(text):
        found.update(_walk(prefix + [name]))
    return found


def _flags(text: str) -> set[str]:
    return set(_FLAG.findall(text))


def test_docs_cli_reference_flags_match_help() -> None:
    """Every documented subcommand's flags are exactly the flags ``--help`` prints."""
    body = REFERENCE.read_text(encoding="utf-8").replace("\r\n", "\n")
    documented: dict[tuple[str, ...], str] = {}
    for match in _SECTION.finditer(body):
        key = tuple(match.group(1).split()) if match.group(1) else ()
        assert key not in documented
        documented[key] = match.group(2)
    live = _walk([])
    assert documented
    assert set(documented) == set(live)
    assert ("run-once",) in documented
    assert ("install-service",) in documented
    for key, doc_text in documented.items():
        doc_flags = _flags(doc_text)
        live_flags = _flags(live[key])
        assert doc_flags, key
        assert doc_flags == live_flags, key
    run_once = _flags(documented[("run-once",)])
    assert "--no-send" in run_once
    assert "--mcm-config" in run_once
    assert "--config" in run_once
    service = _flags(documented[("install-service",)])
    assert "--dry-run" in service
    assert "--apply" in service
    assert "--from" in _flags(documented[("init",)])
    assert "--mcm-config" in _flags(documented[("coverage",)])
    # START-03. Doctor and coverage flags in this file are the flags --help prints.
    for key in (("doctor",), ("coverage",)):
        assert key in documented
        assert _flags(documented[key]) == _flags(live[key]), key
    assert "--live" in _flags(documented[("doctor",)])
    assert "--mcm-config" in _flags(documented[("doctor",)])
    assert "--mcm-config" in _flags(documented[("coverage",)])


def test_start03_docs_name_aliases_doh_and_secret_precedence() -> None:
    """START-03. The operator docs name the alias table, DoH variables, and profile-then-env."""
    root = ROOT
    faq = (root / "docs" / "faq-troubleshooting.md").read_text(encoding="utf-8")
    readme = (root / "README.md").read_text(encoding="utf-8")
    keys = (root / "docs" / "api-keys.md").read_text(encoding="utf-8")
    host = (root / "docs" / "host-network.md").read_text(encoding="utf-8")
    reference = REFERENCE.read_text(encoding="utf-8")
    for text in (faq, readme, keys, host):
        assert "GUARDIAN_RESOLVER_MODE" in text
        assert "GUARDIAN_DOH_ENDPOINTS" in text
        assert "GUARDIAN_DOH_BOOTSTRAP" in text
    for typed, canonical in (
        ("arbitrum-mainnet", "arbitrum-one"),
        ("polygon-mainnet", "polygon-pos"),
        ("arbitrum-one", "arbitrum-one"),
        ("polygon-pos", "polygon-pos"),
    ):
        assert typed in faq
        assert canonical in faq
    assert "nonempty" in keys.lower() or "nonempty" in keys
    assert "secret profile" in keys.lower()
    assert "process environment" in keys.lower()
    assert "401" in keys
    assert "selected_from" in reference
    assert "DoH enabled is not a qualified path" in faq
    assert "DoH enabled is not a qualified path" in host
