"""Tests are hermetic with respect to ambient credentials and local config.

The shared credential resolver reads the process environment after the private
profile, and the CLI loads ``.env.local`` when a variable is unset. A
developer's keys, secret-profile path, and checkout ``.env.local`` must not
leak into fixture runs. Tests that need a value set it themselves.

Live DNS and non-loopback connects are refused in this process and in child
Python processes. A hostname lookup raises ``gaierror`` immediately, which the
readers already map to a failed read (``dns resolution failed`` /
``dns_failed``), not an empty success.
"""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path
from xml.sax.saxutils import escape

import pytest

_NETBLOCK = Path(__file__).resolve().parent / "support" / "no_live_network"
_netblock_path = str(_NETBLOCK)
_pythonpath = os.environ.get("PYTHONPATH", "")
if _netblock_path not in _pythonpath.split(os.pathsep):
    os.environ["PYTHONPATH"] = _netblock_path + (os.pathsep + _pythonpath if _pythonpath else "")

_guard_spec = importlib.util.spec_from_file_location(
    "live_network_guard",
    _NETBLOCK / "live_network_guard.py",
)
if _guard_spec is None or _guard_spec.loader is None:
    raise RuntimeError("live network guard could not be loaded")
_live_network_guard = importlib.util.module_from_spec(_guard_spec)
sys.modules["live_network_guard"] = _live_network_guard
_guard_spec.loader.exec_module(_live_network_guard)

_JUNIT_CASES: dict[str, pytest.TestReport] = {}
_JUNIT_PATH: str | None = None

# Offline installs read the uv cache; keep it pointing at the real one even though HOME moves per test.
if "UV_CACHE_DIR" not in os.environ:
    os.environ["UV_CACHE_DIR"] = str(Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache") / "uv")

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


@pytest.fixture(autouse=True)
def _no_live_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Refuse live DNS and non-loopback sockets before any timeout can start.

    Hostname resolution raises ``socket.gaierror`` (an ``OSError``) at once,
    including the C ``_socket.getaddrinfo`` used by ``socket.create_connection``.
    Callers that already catch ``OSError`` keep the failed-read result. A test
    that needs a canned answer passes its own ``resolve`` hook or replaces
    ``socket.getaddrinfo`` after this fixture. Child interpreters load the same
    guard from ``PYTHONPATH`` via ``sitecustomize``.
    """
    _live_network_guard.install(monkeypatch)


def pytest_configure(config: pytest.Config) -> None:
    global _JUNIT_PATH
    _JUNIT_PATH = getattr(config.option, "xmlpath", None)


def pytest_runtest_logreport(report: pytest.TestReport) -> None:
    """Rewrite the JUnit file after each test so a killed job still has results."""
    if _JUNIT_PATH is None or report.when == "teardown":
        return
    if report.when == "setup" and report.passed:
        return
    _JUNIT_CASES[report.nodeid] = report
    _write_junit(_JUNIT_PATH, _JUNIT_CASES)


def _write_junit(path: str, cases: dict[str, pytest.TestReport]) -> None:
    failures = sum(1 for report in cases.values() if report.failed)
    skipped = sum(1 for report in cases.values() if report.skipped)
    errors = sum(1 for report in cases.values() if report.when == "setup" and report.failed)
    body = []
    for nodeid, report in cases.items():
        classname, _, name = nodeid.rpartition("::")
        if not name:
            classname, name = "", nodeid
        duration = f"{float(getattr(report, 'duration', 0.0)):.6f}"
        case = f'<testcase classname="{escape(classname)}" name="{escape(name)}" time="{duration}">'
        if report.failed:
            tag = "error" if report.when == "setup" else "failure"
            detail = escape(str(report.longrepr))
            case += f'<{tag} message="{escape(report.nodeid)}">{detail}</{tag}>'
        elif report.skipped:
            reason = ""
            if isinstance(report.longrepr, tuple) and len(report.longrepr) >= 3:
                reason = escape(str(report.longrepr[2]))
            case += f'<skipped message="{reason}"/>'
        case += "</testcase>"
        body.append(case)
    xml = (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        f'<testsuite name="pytest" tests="{len(cases)}" failures="{failures - errors}" '
        f'errors="{errors}" skipped="{skipped}">\n'
        + "\n".join(body)
        + "\n</testsuite>\n"
    )
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(target.suffix + ".tmp")
    temporary.write_text(xml, encoding="utf-8")
    temporary.replace(target)
