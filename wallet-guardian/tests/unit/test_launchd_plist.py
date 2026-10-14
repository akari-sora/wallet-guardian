"""LaunchAgent plists are printed and never loaded.

``balance-probe`` is named in the probe plist and is not a subcommand here.
"""

from __future__ import annotations

import json
import plistlib
import shutil
import subprocess
from pathlib import Path

import pytest

from guardian import __version__
from guardian.cli import main
from guardian.service_install import (
    START_INTERVAL_SLEEP_WARNING,
    CommandResult,
    execute_install_service,
    inspect_launchd_schedule,
    render_launchd_agents,
    render_units,
)

_PROBE = "com.walletguardian.balance-probe.plist"
_RUN = "com.walletguardian.run-once.plist"
_WATCH = "com.walletguardian.watchdog.plist"
_PROBE_MINUTES = list(range(0, 60, 5))
_WATCH_MINUTES = [0, 15, 30, 45]
_RUN_HOURS = list(range(0, 24, 3))


def _agents(root: Path, *, offset: int = 5) -> dict[str, str]:
    config = root / "config"
    return render_launchd_agents(
        executable=str(root / "bin" / "guardian"),
        version=__version__,
        config_directory=str(config),
        state_directory=str(root / "state"),
        mcm_config=str(config / "monitor.json"),
        offset_minutes=offset,
    )


def _loaded(text: str) -> dict:
    loaded = plistlib.loads(text.encode("utf-8"))
    assert isinstance(loaded, dict)
    return loaded


def _minutes(loaded: dict) -> list[int]:
    slots = loaded["StartCalendarInterval"]
    assert isinstance(slots, list) and slots
    assert all(isinstance(slot, dict) for slot in slots)
    return [int(slot["Minute"]) for slot in slots]


def test_launchd_dry_run_prints_three_agents_and_exits_nonzero(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Dry-run prints the three plists and exits non-zero. launchctl is not invoked."""

    def refused(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("subprocess during launchd dry-run")

    monkeypatch.setattr(subprocess, "run", refused)

    def runner(argv: list[str]) -> CommandResult:
        raise AssertionError(argv)

    executable = tmp_path / "bin" / "guardian"
    output = tmp_path / "preview"
    code = main(
        [
            "install-service",
            "--dry-run",
            "--schedule-mode",
            "launchd_agent",
            "--output",
            str(output),
            "--executable",
            str(executable),
            "--config-dir",
            str(tmp_path / "config"),
            "--state-dir",
            str(tmp_path / "state"),
            "--mcm-config",
            str(tmp_path / "config" / "monitor.json"),
            "--offset-minutes",
            "17",
        ],
        service_runner=runner,
    )
    payload = json.loads(capsys.readouterr().out)
    assert code == 1
    assert payload["dry_run"] is True
    assert payload["installed"] is False
    assert payload["applied"] is False
    assert payload["loaded"] is False
    assert payload["launchctl_called"] is False
    assert payload["systemctl_called"] is False
    assert payload["sender_started"] is False
    assert payload["schedule_mode"] == "launchd_agent"
    assert payload["probe_subcommand_implemented"] is False
    assert payload["commands"] == []
    assert payload["selected_files"] == [_PROBE, _RUN, _WATCH]
    assert set(payload["rendered"]) == {_PROBE, _RUN, _WATCH}
    assert "launchd_agent_not_loaded" in payload["warnings"]
    assert START_INTERVAL_SLEEP_WARNING not in payload["warnings"]
    for name, text in payload["rendered"].items():
        assert text.startswith("<?xml")
        assert (output / name).read_text(encoding="utf-8") == text
        assert "<key>StartInterval</key>" not in text
        assert "<key>StartCalendarInterval</key>" in text


def test_launchd_program_arguments_are_absolute_and_omit_uv_sync(tmp_path: Path) -> None:
    """Paths in ProgramArguments are absolute. The arrays do not contain uv sync."""
    rendered = _agents(tmp_path, offset=17)
    executable = str((tmp_path / "bin" / "guardian").resolve())
    monitor = str((tmp_path / "config" / "monitor.json").resolve())
    expected = {
        _PROBE: [executable, "balance-probe", "--mcm-config", monitor, "--no-send"],
        _RUN: [executable, "run-once", "--mcm-config", monitor, "--no-send"],
        _WATCH: [executable, "watchdog", "--no-send", "--mcm-config", monitor],
    }
    for name, arguments in expected.items():
        loaded = _loaded(rendered[name])
        assert loaded["ProgramArguments"] == arguments
        assert Path(arguments[0]).is_absolute()
        assert Path(arguments[arguments.index("--mcm-config") + 1]).is_absolute()
        assert "--no-send" in arguments
        assert "uv sync" not in "\n".join(arguments)
        assert "uv sync" not in rendered[name]
        blob = "\n".join(arguments).lower()
        assert "uv sync" not in blob
        assert not any(part in {"uv", "sync"} for part in arguments)


def test_launchd_calendars_use_start_calendar_interval(tmp_path: Path) -> None:
    """Probe every 5 minutes, run-once every 3 hours, watchdog at 0, 15, 30, 45."""
    rendered = _agents(tmp_path, offset=17)
    probe = _loaded(rendered[_PROBE])
    watch = _loaded(rendered[_WATCH])
    run = _loaded(rendered[_RUN])
    assert _minutes(probe) == _PROBE_MINUTES
    assert all(set(slot) == {"Minute"} for slot in probe["StartCalendarInterval"])
    assert _minutes(watch) == _WATCH_MINUTES
    assert all(set(slot) == {"Minute"} for slot in watch["StartCalendarInterval"])
    assert [(slot["Hour"], slot["Minute"]) for slot in run["StartCalendarInterval"]] == [
        (hour, 17) for hour in _RUN_HOURS
    ]
    for loaded in (probe, watch, run):
        assert "StartInterval" not in loaded
    assert inspect_launchd_schedule(rendered[_PROBE]) == []


def test_launchd_plists_parse_and_plutil_lints_when_present(tmp_path: Path) -> None:
    """plistlib always checks structure. plutil -lint runs only when the binary exists."""
    rendered = _agents(tmp_path)
    for name, text in rendered.items():
        loaded = _loaded(text)
        assert isinstance(loaded["ProgramArguments"], list)
        assert loaded["ProgramArguments"]
        assert isinstance(loaded["StartCalendarInterval"], list)
        assert loaded["Label"] == name[: -len(".plist")]
        assert "StartInterval" not in loaded
    plutil = shutil.which("plutil")
    if plutil is None:
        return
    for name, text in rendered.items():
        path = tmp_path / name
        path.write_text(text, encoding="utf-8")
        completed = subprocess.run([plutil, "-lint", str(path)], check=False, capture_output=True, text=True)
        assert completed.returncode == 0, completed.stdout + completed.stderr


def test_launchd_spaces_and_brackets_are_xml_escaped(tmp_path: Path) -> None:
    """A path with spaces, brackets, and XML metacharacters survives as one string."""
    root = tmp_path / "My Wallet [a&b<c]"
    executable = root / "bin" / "guardian"
    monitor = root / "config" / "monitor.json"
    rendered = render_launchd_agents(
        executable=str(executable),
        version=__version__,
        config_directory=str(root / "config"),
        state_directory=str(root / "state"),
        mcm_config=str(monitor),
        offset_minutes=5,
    )
    text = rendered[_PROBE]
    assert "&#91;" in text and "&#93;" in text
    assert "&amp;" in text and "&lt;" in text
    assert "My Wallet" in text
    arguments = _loaded(text)["ProgramArguments"]
    assert arguments[0] == str(executable.resolve())
    assert arguments[3] == str(monitor.resolve())
    assert " " in arguments[0] and "[" in arguments[0] and "]" in arguments[0]
    assert "&" in arguments[0] and "<" in arguments[0]


def test_start_interval_warns_that_sleep_will_skip_runs(tmp_path: Path) -> None:
    """A generated or supplied StartInterval plist warns. StartCalendarInterval does not."""
    assert inspect_launchd_schedule("<key>StartCalendarInterval</key>") == []
    fragment = inspect_launchd_schedule("<key>StartInterval</key>")
    assert fragment == [START_INTERVAL_SLEEP_WARNING]
    assert "sleep will skip runs" in START_INTERVAL_SLEEP_WARNING
    supplied = plistlib.dumps(
        {"Label": "example", "StartInterval": 300},
        fmt=plistlib.FMT_XML,
    ).decode("utf-8")
    assert inspect_launchd_schedule(supplied) == [START_INTERVAL_SLEEP_WARNING]

    def runner(argv: list[str]) -> CommandResult:
        raise AssertionError(argv)

    payload = execute_install_service(
        dry_run=True,
        apply=False,
        schedule_mode="launchd_agent",
        supplied_plist=supplied,
        mcm_config=str(tmp_path / "config" / "monitor.json"),
        executable=str(tmp_path / "bin" / "guardian"),
        config_directory=str(tmp_path / "config"),
        state_directory=str(tmp_path / "state"),
        runner=runner,
        systemd_present=False,
        cron_present=False,
    )
    assert payload["launchctl_called"] is False
    assert payload["commands"] == []
    assert START_INTERVAL_SLEEP_WARNING in payload["warnings"]
    assert all("<key>StartInterval</key>" not in text for text in payload["rendered"].values())


def test_launchd_output_stays_in_directory_and_apply_does_not_load(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """--output writes only under that directory. --apply still does not load."""

    def refused(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("subprocess during launchd apply")

    monkeypatch.setattr(subprocess, "run", refused)
    calls: list[list[str]] = []

    def runner(argv: list[str]) -> CommandResult:
        calls.append(list(argv))
        raise AssertionError(argv)

    output = tmp_path / "out"
    outside = tmp_path / "outside"
    outside.mkdir()
    executable = tmp_path / "bin" / "guardian"
    code = main(
        [
            "install-service",
            "--apply",
            "--schedule-mode",
            "launchd_agent",
            "--output",
            str(output),
            "--executable",
            str(executable),
            "--config-dir",
            str(tmp_path / "config"),
            "--state-dir",
            str(tmp_path / "state"),
            "--mcm-config",
            str(tmp_path / "config" / "monitor.json"),
        ],
        service_runner=runner,
    )
    payload = json.loads(capsys.readouterr().out)
    assert code == 1
    assert payload["installed"] is False
    assert payload["loaded"] is False
    assert payload["launchctl_called"] is False
    assert "launchctl_refused" in payload["warnings"]
    assert calls == []
    written = [Path(item) for item in payload["wrote"]]
    assert written
    assert {path.name for path in written} == {_PROBE, _RUN, _WATCH}
    for path in written:
        assert path.resolve().is_relative_to(output.resolve())
        assert path.suffix == ".plist"
    home_agents = Path.home() / "Library" / "LaunchAgents"
    assert not home_agents.exists()
    assert list(outside.rglob("*")) == []
    assert not any(output.rglob("*.service"))
    assert not any(output.rglob("*.timer"))
    assert not any(output.rglob("*.cron"))


def test_default_schedule_mode_still_renders_systemd(tmp_path: Path) -> None:
    """Omitting launchd_agent keeps the systemd render path, including --no-send."""

    def runner(argv: list[str]) -> CommandResult:
        if argv and argv[0] in {"launchctl", "pmset", "systemctl"}:
            raise AssertionError(argv)
        return CommandResult(0, "Linger=yes\nState=active\n", "")

    payload = execute_install_service(
        dry_run=True,
        apply=False,
        mcm_config=str(tmp_path / "config" / "monitor.json"),
        executable=str(tmp_path / "bin" / "guardian"),
        config_directory=str(tmp_path / "config"),
        state_directory=str(tmp_path / "state"),
        legacy_executable=str(tmp_path / "bin" / "legacy"),
        runner=runner,
        systemd_present=True,
        cron_present=False,
    )
    assert payload["schedule_mode"] == "systemd_user_timer"
    assert all(not name.endswith(".plist") for name in payload["rendered"])
    units = render_units(
        executable=str(tmp_path / "bin" / "guardian"),
        version=__version__,
        config_directory=str(tmp_path / "config"),
        state_directory=str(tmp_path / "state"),
        mcm_config=str(tmp_path / "config" / "monitor.json"),
        monitor_user="guardian-monitor",
        offset_minutes=5,
        legacy_executable=str(tmp_path / "bin" / "legacy"),
        install_mode="systemd_user_timer",
    )
    assert "--no-send" in units["guardian-mcm-shadow.service"]
    assert "--no-send" in units["guardian-watchdog.service"]
    cron = units["guardian-unattended.cron"]
    assert "--no-send" in cron
    assert "uv sync" not in "\n".join(units.values())
