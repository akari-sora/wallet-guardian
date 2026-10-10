"""Tests are hermetic with respect to ambient credentials and local config.

The shared credential resolver reads the process environment after the private
profile, and the CLI loads ``.env.local`` when a variable is unset. A
developer's keys, secret-profile path, and checkout ``.env.local`` must not
leak into fixture runs. Tests that need a value set it themselves.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

_SUFFIXES = ("_API_KEY", "_ACCESS_KEY", "_BOT_TOKEN", "_AUTH_TOKEN")
_EXPLICIT = (
    "MONITOR_SECRET_PROFILE",
    "MONITOR_ACCOUNTS_FILE",
    "MONITOR_DELIVERY_PROFILE",
    "MONITOR_STATE_DIR",
    "GUARDIAN_CONFIG_DIR",
    "GUARDIAN_STATE_DIR",
    "GUARDIAN_MONITOR_CONFIG",
    "GUARDIAN_RESOLVER_MODE",
    "GUARDIAN_DOH_ENDPOINTS",
    "GUARDIAN_DOH_BOOTSTRAP",
    "GUARDIAN_EGRESS_AMBIENT_PROXY",
)
# Path, endpoint, and resolver settings inherited from a developer shell.
_MARKERS = (
    "PATH",
    "DIR",
    "FILE",
    "URL",
    "ENDPOINT",
    "HOST",
    "RESOLVER",
    "DOH",
    "BOOTSTRAP",
    "PROFILE",
)


def _inherited_monitor_setting(name: str) -> bool:
    if name in _EXPLICIT:
        return True
    if not (name.startswith("MONITOR_") or name.startswith("GUARDIAN_")):
        return False
    return any(marker in name for marker in _MARKERS)


@pytest.fixture(autouse=True)
def _no_ambient_credentials(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Drop inherited secrets and point config/state/home at this test's tmp dir.

    ``XDG_CONFIG_HOME`` is the switch ``default_env_file`` already honors, so
    ``load_local_env`` cannot reopen a checkout ``.env.local`` after those
    keys are removed.
    """
    for name in list(os.environ):
        if name.endswith(_SUFFIXES) or _inherited_monitor_setting(name):
            monkeypatch.delenv(name, raising=False)
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg-config"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg-state"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg-data"))
