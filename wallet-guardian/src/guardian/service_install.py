"""Dry-run installer for the unattended box schedule.

The command prints absolute unit text and refuses to enable anything unless
``apply`` is set. Probes use an injectable runner. A dry-run never calls
``systemctl``. This module does not sign, broadcast, or fetch code.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from guardian import __version__
from guardian.errors import ConfigError
from guardian.locations import config_dir as default_config_dir
from guardian.locations import state_dir as default_state_dir
from guardian.resources import read_resource_json, read_resource_text

_UNIT_NAMES = (
    "guardian-mcm-shadow.service",
    "guardian-mcm-shadow.timer",
    "guardian-legacy-collector.service",
    "guardian-legacy-collector.timer",
    "guardian-watchdog.service",
    "guardian-watchdog.timer",
)
_CRON_NAME = "guardian-unattended.cron"
_TEMPLATE_NAMES = _UNIT_NAMES + (_CRON_NAME,)
_SERVICE_NAMES = (
    "guardian-mcm-shadow.service",
    "guardian-legacy-collector.service",
    "guardian-watchdog.service",
)
_TIMER_NAMES = (
    "guardian-mcm-shadow.timer",
    "guardian-legacy-collector.timer",
    "guardian-watchdog.timer",
)
_HARDENING = (
    "NoNewPrivileges=yes",
    "ProtectSystem=strict",
    "ProtectHome=read-only",
    "PrivateTmp=yes",
    "MemoryMax=512M",
    "CPUQuota=50%",
    "TimeoutStartSec=3600",
)
_FETCH_MARKERS = (
    "curl ",
    "wget ",
    "git clone",
    "git pull",
    "uv sync",
    "uv pip",
    "pip install",
    "https://",
    "http://",
)
_MOVING = frozenset({"head", "latest", "main", "master"})
_USER = re.compile(r"^[a-z_][a-z0-9_-]{0,31}$")
_TOKEN = re.compile(r"@[A-Z0-9_]+@")
_DEFAULT_LEGACY = "/usr/local/sbin/guardian-legacy-collector"
_WATCHDOG_CALENDAR = "*:0/15"
Runner = Callable[[Sequence[str]], Any]


class CommandResult:
    """One probed command. stdout is not a secret store."""

    def __init__(self, returncode: int, stdout: str, stderr: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def installed_version() -> str:
    """Version pinned in the package. A branch name is refused."""
    pin = read_resource_json("runtime-pin.json")
    pinned = pin.get("version") if isinstance(pin, dict) else None
    if not isinstance(pinned, str) or pinned != __version__:
        raise ConfigError("runtime pin does not match the installed version")
    if pinned.strip().lower() in _MOVING:
        raise ConfigError("installed version must be fixed")
    return pinned


def load_templates() -> dict[str, str]:
    """Unit templates shipped inside the package."""
    loaded: dict[str, str] = {}
    for name in _TEMPLATE_NAMES:
        loaded[name] = read_resource_text("deploy", "systemd", name)
    return loaded


def render_units(
    *,
    executable: str,
    version: str,
    config_directory: str,
    state_directory: str,
    mcm_config: str,
    monitor_user: str,
    offset_minutes: int,
    legacy_executable: str,
    install_mode: str,
) -> dict[str, str]:
    """Substitute absolute paths. No network and no moving version."""
    if version.strip().lower() in _MOVING:
        raise ConfigError("installed version must be fixed")
    if install_mode not in {"cron", "none", "systemd_system_service", "systemd_user_timer"}:
        raise ConfigError("install mode")
    _check_user(monitor_user)
    offset = _offset(offset_minutes)
    executable = _unit_path(executable, "executable")
    legacy_executable = _unit_path(legacy_executable, "legacy executable")
    config_directory = _unit_path(config_directory, "config directory")
    state_directory = _unit_path(state_directory, "state directory")
    mcm_config = _unit_path(mcm_config, "mcm config")
    if install_mode == "systemd_user_timer":
        user_directive = ""
        wanted_by = "default.target"
    else:
        user_directive = f"User={monitor_user}\nGroup={monitor_user}"
        wanted_by = "multi-user.target"
    values = {
        "CONFIG_DIR": config_directory,
        "EXECUTABLE": executable,
        "LEGACY_EXECUTABLE": legacy_executable,
        "MCM_CONFIG": mcm_config,
        "MONITOR_USER": monitor_user,
        "OFFSET_MINUTE": str(offset),
        "ON_CALENDAR": f"*-*-* 0/3:{offset:02d}:00",
        "STATE_DIR": state_directory,
        "USER_DIRECTIVE": user_directive,
        "VERSION": version,
        "WANTED_BY": wanted_by,
        "WATCHDOG_CALENDAR": _WATCHDOG_CALENDAR,
    }
    rendered: dict[str, str] = {}
    for name, body in load_templates().items():
        text = body
        for key, value in values.items():
            text = text.replace(f"@{key}@", value)
        if _TOKEN.search(text):
            raise ConfigError("unit template has an unsubstituted token")
        lowered = text.lower()
        if any(marker in lowered for marker in _FETCH_MARKERS):
            raise ConfigError("unit would fetch code")
        rendered[name] = text if text.endswith("\n") else text + "\n"
    _require_hardening(rendered)
    return rendered


def classify_schedule(
    *,
    linger: bool,
    user_manager: bool,
    systemd_present: bool,
    cron_present: bool,
) -> dict[str, Any]:
    """User timer with linger, else a non-root system service, else tested cron."""
    warnings: list[str] = []
    user_timer = "refused"
    if linger and user_manager and systemd_present:
        return {
            "cron_fallback": False,
            "schedule_mode": "systemd_user_timer",
            "status": "ok",
            "user_timer": "ok",
            "warnings": warnings,
        }
    if not linger:
        warnings.append("linger_missing")
    if not user_manager:
        warnings.append("user_manager_missing")
    warnings.append("systemd_user_timer_refused")
    if systemd_present:
        warnings.append("systemd_system_service_selected")
        return {
            "cron_fallback": False,
            "schedule_mode": "systemd_system_service",
            "status": "warn",
            "user_timer": user_timer,
            "warnings": warnings,
        }
    warnings.append("systemd_not_detected")
    if cron_present:
        warnings.append("cron_fallback_selected")
        return {
            "cron_fallback": True,
            "schedule_mode": "cron",
            "status": "warn",
            "user_timer": user_timer,
            "warnings": warnings,
        }
    warnings.append("cron_not_detected")
    return {
        "cron_fallback": False,
        "schedule_mode": "none",
        "status": "warn",
        "user_timer": user_timer,
        "warnings": warnings,
    }


def execute_install_service(
    *,
    dry_run: bool,
    apply: bool,
    mcm_config: str | None,
    output_dir: str | None = None,
    executable: str | None = None,
    config_directory: str | None = None,
    state_directory: str | None = None,
    user: str = "guardian-monitor",
    offset_minutes: int = 5,
    legacy_executable: str | None = None,
    runner: Runner | None = None,
    systemd_present: bool | None = None,
    cron_present: bool | None = None,
    version: str | None = None,
) -> dict[str, Any]:
    """Render the plan. Write only under ``output_dir`` unless ``apply`` is set."""
    applying = bool(apply) and not bool(dry_run)
    recorded: list[list[str]] = []
    facts = _probe(
        user,
        _recording(runner, recorded, allow_systemctl=False),
        systemd_present=systemd_present,
        cron_present=cron_present,
    )
    choice = classify_schedule(
        linger=facts["linger"],
        user_manager=facts["user_manager"],
        systemd_present=facts["systemd_present"],
        cron_present=facts["cron_present"],
    )
    pinned = installed_version() if version is None else version
    paths = _paths(
        executable=executable,
        config_directory=config_directory,
        state_directory=state_directory,
        mcm_config=mcm_config,
        legacy_executable=legacy_executable,
    )
    rendered = render_units(
        executable=paths["executable"],
        version=pinned,
        config_directory=paths["config_directory"],
        state_directory=paths["state_directory"],
        mcm_config=paths["mcm_config"],
        monitor_user=user,
        offset_minutes=offset_minutes,
        legacy_executable=paths["legacy_executable"],
        install_mode=str(choice["schedule_mode"]),
    )
    mode = str(choice["schedule_mode"])
    selected = _selected(mode)
    payload: dict[str, Any] = {
        "applied": False,
        "commands": recorded,
        "common_cause": "same-host watchdog cannot alert when the host is down",
        "cron_detected": facts["cron_present"] is True,
        "cron_fallback": choice["cron_fallback"] is True,
        "dry_run": not applying,
        "executable": paths["executable"],
        "fetches_code": False,
        "installed": False,
        "linger": {
            "present": facts["linger"] is True,
            "user_manager": facts["user_manager"] is True,
            "user_timer": choice["user_timer"],
        },
        "offset_minutes": _offset(offset_minutes),
        "on_calendar": f"*-*-* 0/3:{_offset(offset_minutes):02d}:00",
        "persistent_counts_as_on_time": False,
        "planned_units": [
            {
                "action": "print_only",
                "exec": _exec_start(rendered[name]),
                "name": name,
            }
            for name in _UNIT_NAMES
        ],
        "rendered": rendered,
        "schedule_mode": mode,
        "selected_files": selected,
        "sender_started": False,
        "status": choice["status"],
        "systemctl_called": False,
        "version": pinned,
        "warnings": list(choice["warnings"]),
        "wrote": [],
    }
    if output_dir:
        payload["wrote"] = _write_units(Path(output_dir), rendered)
    if not applying:
        payload["systemctl_called"] = _systemctl(recorded)
        payload["commands"] = recorded
        return payload
    if mode == "none":
        payload["status"] = "refused"
        payload["warnings"] = [*payload["warnings"], "no_scheduler_to_install"]
        payload["systemctl_called"] = _systemctl(recorded)
        payload["commands"] = recorded
        return payload
    destination = _destination(mode, output_dir, user)
    if output_dir is None:
        payload["wrote"] = _write_units(destination, {name: rendered[name] for name in selected})
    install = _recording(runner, recorded, allow_systemctl=True)
    failed = _run_install(mode, user, destination, selected, install)
    payload["commands"] = recorded
    payload["systemctl_called"] = _systemctl(recorded)
    if failed:
        payload["status"] = "error"
        payload["warnings"] = [*payload["warnings"], "install_command_failed"]
        return payload
    for unit in payload["planned_units"]:
        if unit["name"] in selected and unit["name"].endswith(".timer"):
            unit["action"] = "enable"
    payload["applied"] = True
    payload["dry_run"] = False
    payload["installed"] = True
    payload["status"] = "installed"
    return payload


def _probe(
    user: str,
    runner: Runner,
    *,
    systemd_present: bool | None,
    cron_present: bool | None,
) -> dict[str, bool]:
    _check_user(user)
    result = _coerce(runner(["loginctl", "show-user", user, "--property=Linger", "--property=State"]))
    linger, manager = _parse_linger(result.stdout if result.returncode == 0 else "")
    if systemd_present is None:
        systemd_present = shutil.which("systemctl") is not None
    if cron_present is None:
        cron_present = shutil.which("cron") is not None or shutil.which("crond") is not None
    return {
        "cron_present": bool(cron_present),
        "linger": linger,
        "systemd_present": bool(systemd_present),
        "user_manager": manager,
    }


def _parse_linger(stdout: str) -> tuple[bool, bool]:
    linger = False
    manager = False
    for line in stdout.splitlines():
        if line.startswith("Linger="):
            linger = line.split("=", 1)[1].strip().lower() == "yes"
        elif line.startswith("State="):
            manager = line.split("=", 1)[1].strip().lower() in {"active", "lingering"}
    return linger, manager


def _paths(
    *,
    executable: str | None,
    config_directory: str | None,
    state_directory: str | None,
    mcm_config: str | None,
    legacy_executable: str | None,
) -> dict[str, str]:
    config = _directory(config_directory, default_config_dir())
    state = _directory(state_directory, default_state_dir())
    config_file = _file(mcm_config, Path(config) / "monitor.json")
    return {
        "config_directory": config,
        "executable": _resolve_executable(executable),
        "legacy_executable": _unit_path(legacy_executable or _DEFAULT_LEGACY, "legacy executable"),
        "mcm_config": config_file,
        "state_directory": state,
    }


def _resolve_executable(explicit: str | None) -> str:
    if explicit:
        return _unit_path(explicit, "executable")
    found = shutil.which("guardian")
    if found:
        return _unit_path(found, "executable")
    name = "guardian.exe" if os.name == "nt" else "guardian"
    sibling = Path(sys.executable).resolve().with_name(name)
    if sibling.is_file():
        return _unit_path(os.fspath(sibling), "executable")
    return _unit_path(sys.executable, "executable")


def _directory(value: str | None, default: Path) -> str:
    path = Path(value).expanduser() if value else default
    return _unit_path(os.fspath(path), "directory")


def _file(value: str | None, default: Path) -> str:
    path = Path(value).expanduser() if value else default
    return _unit_path(os.fspath(path), "file")


def _unit_path(value: str, label: str) -> str:
    resolved = Path(value).expanduser().resolve()
    if not resolved.is_absolute():
        raise ConfigError(f"{label} must be absolute")
    text = os.fspath(resolved)
    if any(char in text for char in "\n\r\"'%"):
        raise ConfigError(f"{label} contains a forbidden character")
    if any(char.isspace() for char in text):
        raise ConfigError(f"{label} must not contain whitespace")
    if "://" in text:
        raise ConfigError(f"{label} must be a local path")
    return text


def _offset(value: int) -> int:
    if type(value) is not int or not 0 <= value <= 59:
        raise ConfigError("offset minutes must be 0..59")
    return value


def _check_user(user: str) -> None:
    if not isinstance(user, str) or _USER.fullmatch(user) is None or user == "root":
        raise ConfigError("monitor account must be a dedicated non-root user")


def _require_hardening(rendered: Mapping[str, str]) -> None:
    for name in _SERVICE_NAMES:
        text = rendered[name]
        for marker in _HARDENING:
            if marker not in text:
                raise ConfigError("unit is missing a resource limit")
        if "ReadWritePaths=" not in text:
            raise ConfigError("unit is missing a state write path")


def _selected(mode: str) -> list[str]:
    if mode == "cron":
        return [_CRON_NAME]
    if mode in {"systemd_system_service", "systemd_user_timer"}:
        return list(_UNIT_NAMES)
    return []


def _exec_start(text: str) -> str:
    for line in text.splitlines():
        if line.startswith("ExecStart="):
            return line.split("=", 1)[1]
    return ""


def _write_units(directory: Path, rendered: Mapping[str, str]) -> list[str]:
    root = directory.expanduser().resolve()
    if not root.is_absolute():
        raise ConfigError("output directory must be absolute")
    root.mkdir(parents=True, exist_ok=True)
    wrote: list[str] = []
    for name, text in sorted(rendered.items()):
        if name != Path(name).name or name in {".", ".."}:
            raise ConfigError("unit name")
        path = root / name
        parent = path.resolve().parent
        if os.path.normcase(os.fspath(parent)) != os.path.normcase(os.fspath(root)):
            raise ConfigError("unit path escaped the output directory")
        path.write_text(text if text.endswith("\n") else text + "\n", encoding="utf-8", newline="\n")
        wrote.append(os.fspath(path.resolve()))
    return wrote


def _destination(mode: str, output_dir: str | None, user: str) -> Path:
    if output_dir:
        return Path(output_dir).expanduser().resolve()
    if mode == "systemd_user_timer":
        return Path("/home") / user / ".config" / "systemd" / "user"
    if mode == "systemd_system_service":
        return Path("/etc/systemd/system")
    if mode == "cron":
        return Path("/etc/cron.d")
    raise ConfigError("no install destination")


def _run_install(mode: str, user: str, destination: Path, selected: Sequence[str], runner: Runner) -> bool:
    if mode == "cron":
        cron_path = destination / _CRON_NAME
        result = _coerce(runner(["crontab", "-u", user, os.fspath(cron_path)]))
        return result.returncode != 0
    prefix = ["systemctl", "--user"] if mode == "systemd_user_timer" else ["systemctl"]
    commands = [prefix + ["daemon-reload"]]
    for name in selected:
        if name in _TIMER_NAMES:
            commands.append(prefix + ["enable", name])
    for argv in commands:
        result = _coerce(runner(argv))
        if result.returncode != 0:
            return True
    return False


def _recording(runner: Runner | None, recorded: list[list[str]], *, allow_systemctl: bool) -> Runner:
    def wrapped(argv: Sequence[str]) -> CommandResult:
        command = [str(part) for part in argv]
        recorded.append(command)
        if runner is not None:
            return _coerce(runner(command))
        return _default_runner(command, allow_systemctl=allow_systemctl)

    return wrapped


def _default_runner(argv: Sequence[str], *, allow_systemctl: bool) -> CommandResult:
    if not argv:
        return CommandResult(127, "", "empty command")
    if argv[0] == "systemctl" and not allow_systemctl:
        return CommandResult(127, "", "systemctl refused without --apply")
    if argv[0] not in {"crontab", "loginctl", "systemctl"}:
        return CommandResult(127, "", "command refused")
    return _spawn(argv)


def _spawn(argv: Sequence[str]) -> CommandResult:
    try:
        completed = subprocess.run(
            list(argv),
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return CommandResult(127, "", type(exc).__name__)
    return CommandResult(completed.returncode, completed.stdout or "", completed.stderr or "")


def _coerce(value: Any) -> CommandResult:
    if isinstance(value, CommandResult):
        return value
    returncode = getattr(value, "returncode", None)
    stdout = getattr(value, "stdout", None)
    stderr = getattr(value, "stderr", "")
    if not isinstance(returncode, int) or isinstance(returncode, bool) or not isinstance(stdout, str):
        raise ConfigError("command runner returned an unreadable result")
    return CommandResult(returncode, stdout, stderr if isinstance(stderr, str) else "")


def _systemctl(commands: Sequence[Sequence[str]]) -> bool:
    return any(bool(command) and command[0] == "systemctl" for command in commands)
