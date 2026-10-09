"""MCM-SHARE-01 S3. Offline installer draft and honest slot accounting."""

from __future__ import annotations

import json
import socket
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest

from guardian import __version__
from guardian.cli import main
from guardian.errors import ConfigError
from guardian.resources import read_resource_json, resource_path
from guardian.scheduler import evaluate_scheduled_slot, record_scheduled_slot, shadow_writer_lock
from guardian.service_install import CommandResult, execute_install_service, render_units

ROOT = Path(__file__).resolve().parents[2]
_FETCH = ("curl ", "wget ", "git clone", "git pull", "uv sync", "uv pip", "pip install", "https://", "http://")
_HARDENING = (
    "NoNewPrivileges=yes",
    "ProtectSystem=strict",
    "ProtectHome=read-only",
    "PrivateTmp=yes",
    "MemoryMax=512M",
    "CPUQuota=50%",
    "TimeoutStartSec=3600",
)
_SLOT = datetime(2026, 10, 9, 12, 0, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def _block_network(monkeypatch: pytest.MonkeyPatch) -> None:
    def blocked(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("network is disabled")

    monkeypatch.setattr(socket, "socket", blocked)
    monkeypatch.setattr(socket, "create_connection", blocked)
    monkeypatch.setattr(socket, "getaddrinfo", blocked)
    monkeypatch.setattr(httpx, "Client", blocked)


def test_mcm_share_s3_01_render_units_absolute_executable_fixed_version_no_fetch(tmp_path: Path) -> None:
    """MCM-SHARE-S3-01. Rendered units pin an absolute executable and the installed version."""
    executable = tmp_path / "bin" / "guardian"
    legacy = tmp_path / "bin" / "legacy-collector"
    config = tmp_path / "config"
    state = tmp_path / "state"
    monitor = config / "monitor.json"
    rendered = render_units(
        executable=str(executable),
        version=__version__,
        config_directory=str(config),
        state_directory=str(state),
        mcm_config=str(monitor),
        monitor_user="guardian-monitor",
        offset_minutes=17,
        legacy_executable=str(legacy),
        install_mode="systemd_user_timer",
    )
    packaged = resource_path("deploy", "systemd")
    disk = ROOT / "deploy" / "systemd"
    names = sorted(path.name for path in disk.iterdir() if path.is_file())
    assert names
    for name in names:
        assert (packaged / name).read_bytes() == (disk / name).read_bytes()
        assert not any(marker in (disk / name).read_text(encoding="utf-8").lower() for marker in _FETCH)
    pin = read_resource_json("runtime-pin.json")
    assert pin["version"] == __version__
    assert __version__ not in {"main", "latest", "master"}
    resolved_executable = str(executable.resolve())
    resolved_legacy = str(legacy.resolve())
    resolved_state = str(state.resolve())
    mcm = rendered["guardian-mcm-shadow.service"]
    legacy_unit = rendered["guardian-legacy-collector.service"]
    watchdog = rendered["guardian-watchdog.service"]
    timer = rendered["guardian-mcm-shadow.timer"]
    assert mcm.split("ExecStart=", 1)[1].splitlines()[0].startswith(resolved_executable + " ")
    assert "--no-send" in mcm
    assert f"GUARDIAN_INSTALLED_VERSION={__version__}" in mcm
    assert f"ReadWritePaths={resolved_state}" in mcm
    for marker in _HARDENING:
        assert marker in mcm
        assert marker in legacy_unit
        assert marker in watchdog
    assert "RandomizedDelaySec=0" in timer
    assert "Persistent=false" in timer
    assert "*-*-* 0/3:17:00" in timer
    assert legacy_unit.split("ExecStart=", 1)[1].splitlines()[0] == resolved_legacy
    assert "does not wait for an LLM chat to be idle" in mcm
    assert "does not wait for an LLM chat to be idle" in legacy_unit
    joined = "\n".join(rendered.values()).lower()
    assert not any(marker in joined for marker in _FETCH)
    for text in (mcm, legacy_unit, watchdog):
        assert "After=guardian-" not in text
        assert "Requires=guardian-" not in text
    system = render_units(
        executable=str(executable),
        version=__version__,
        config_directory=str(config),
        state_directory=str(state),
        mcm_config=str(monitor),
        monitor_user="guardian-monitor",
        offset_minutes=17,
        legacy_executable=str(legacy),
        install_mode="systemd_system_service",
    )
    system_unit = system["guardian-mcm-shadow.service"]
    assert "User=guardian-monitor" in system_unit
    assert "Group=guardian-monitor" in system_unit
    assert "User=root" not in system_unit
    assert system_unit.split("ExecStart=", 1)[1].startswith(resolved_executable + " ")


def test_mcm_share_s3_02_linger_check_refuses_without_linger_and_cron_only_if_detected(tmp_path: Path) -> None:
    """MCM-SHARE-S3-02. User timers need linger and a user manager. Cron is only a detected fallback."""
    paths = _paths(tmp_path)

    ready, ready_calls = _runner("Linger=yes\nState=active\n")
    ok = _plan(paths, ready, systemd_present=True, cron_present=True)
    assert ok["status"] == "ok"
    assert ok["linger"]["present"] is True
    assert ok["linger"]["user_manager"] is True
    assert ok["linger"]["user_timer"] == "ok"
    assert ok["schedule_mode"] == "systemd_user_timer"
    assert ok["cron_fallback"] is False
    assert ok["cron_detected"] is True
    assert "guardian-unattended.cron" not in ok["selected_files"]
    assert "Persistent=false" in ok["rendered"]["guardian-mcm-shadow.timer"]
    assert ready_calls and ready_calls[0][0] == "loginctl"
    assert not any(command[0] == "systemctl" for command in ready_calls)

    missing, _missing_calls = _runner("Linger=no\nState=closing\n")
    warned = _plan(paths, missing, systemd_present=True, cron_present=True)
    assert warned["linger"]["user_timer"] == "refused"
    assert "linger_missing" in warned["warnings"]
    assert warned["status"] == "warn"
    assert warned["schedule_mode"] == "systemd_system_service"
    assert warned["cron_fallback"] is False
    assert "User=guardian-monitor" in warned["rendered"]["guardian-mcm-shadow.service"]

    manager, _manager_calls = _runner("Linger=yes\nState=offline\n")
    no_manager = _plan(paths, manager, systemd_present=True, cron_present=False)
    assert no_manager["linger"]["user_timer"] == "refused"
    assert "user_manager_missing" in no_manager["warnings"]
    assert no_manager["schedule_mode"] == "systemd_system_service"

    cron_runner, _cron_calls = _runner("Linger=no\nState=offline\n")
    cron = _plan(paths, cron_runner, systemd_present=False, cron_present=True)
    assert cron["cron_fallback"] is True
    assert cron["schedule_mode"] == "cron"
    assert cron["selected_files"] == ["guardian-unattended.cron"]
    assert "run-once" in cron["rendered"]["guardian-unattended.cron"]
    assert "--no-send" in cron["rendered"]["guardian-unattended.cron"]

    absent, _absent_calls = _runner("Linger=no\nState=offline\n")
    no_cron = _plan(paths, absent, systemd_present=False, cron_present=False)
    assert no_cron["cron_detected"] is False
    assert no_cron["cron_fallback"] is False
    assert no_cron["schedule_mode"] == "none"
    assert "cron_not_detected" in no_cron["warnings"]
    assert "guardian-unattended.cron" not in no_cron["selected_files"]


def test_mcm_share_s3_03_sole_writer_second_round_refuses(tmp_path: Path) -> None:
    """MCM-SHARE-S3-03. One shadow writer. The second concurrent round is refused."""
    directory = tmp_path / "state"
    verdict = _on_time(_SLOT)
    release = threading.Event()
    started = threading.Event()
    holder: list[BaseException] = []

    def hold() -> None:
        try:
            with shadow_writer_lock(directory):
                started.set()
                assert release.wait(5)
        except BaseException as exc:  # noqa: BLE001 — the holder must report a failure
            holder.append(exc)
            started.set()
            release.set()

    thread = threading.Thread(target=hold)
    thread.start()
    assert started.wait(5)
    with pytest.raises(ConfigError, match="shadow_round_locked"):
        record_scheduled_slot(directory, verdict)
    release.set()
    thread.join(5)
    assert not thread.is_alive()
    assert holder == []
    saved = record_scheduled_slot(directory, verdict)
    assert saved["on_time"] is True
    assert saved["counted"] is True
    assert saved["on_time_count"] == 1


def test_mcm_share_s3_04_missed_slot_honest_late_and_catchup_not_double_counted(tmp_path: Path) -> None:
    """MCM-SHARE-S3-04. Late, missing, and catch-up rounds do not count, and a catch-up is not a second on-time round."""
    on_time = _on_time(_SLOT, start=_SLOT + timedelta(minutes=30), seal=_SLOT + timedelta(minutes=60))
    assert on_time["on_time"] is True
    assert on_time["counted"] is True
    assert on_time["status"] == "on_time"
    inside = _on_time(_SLOT, start=_SLOT + timedelta(minutes=10), seal=_SLOT + timedelta(minutes=40))
    assert inside["counted"] is True

    late = _on_time(_SLOT, start=_SLOT + timedelta(minutes=30, seconds=1), seal=_SLOT + timedelta(minutes=50))
    assert late["reason"] == "late_start"
    assert late["counted"] is False
    assert late["on_time"] is False
    early = _on_time(_SLOT, start=_SLOT - timedelta(minutes=30, seconds=1), seal=_SLOT + timedelta(minutes=10))
    assert early["reason"] == "early_start"
    assert early["counted"] is False
    late_seal = _on_time(_SLOT, start=_SLOT + timedelta(minutes=5), seal=_SLOT + timedelta(minutes=60, seconds=1))
    assert late_seal["reason"] == "late_seal"
    assert late_seal["counted"] is False
    missing = _on_time(_SLOT, evidence_present=False)
    assert missing["reason"] == "evidence_missing"
    assert missing["counted"] is False
    assert missing["status"] == "evidence_missing"
    encoded = json.dumps(missing)
    assert "zero" not in encoded
    assert missing["status"] not in {"complete", "safe", "zero"}
    timed_out = _on_time(_SLOT, timed_out=True)
    assert timed_out["reason"] == "timeout"
    assert timed_out["counted"] is False
    missed = evaluate_scheduled_slot(
        slot_at=_SLOT + timedelta(hours=3),
        started_at=None,
        sealed_at=None,
        required_scans_ok=False,
        comparison_sealed=False,
        evidence_present=False,
        missed=True,
    )
    assert missed["reason"] == "missed"
    assert missed["counted"] is False
    scans = _on_time(_SLOT, required_scans_ok=False)
    assert scans["reason"] == "required_scans_incomplete"
    assert scans["counted"] is False
    comparison = _on_time(_SLOT, comparison_sealed=False)
    assert comparison["reason"] == "comparison_not_sealed"
    assert comparison["counted"] is False

    directory = tmp_path / "slots"
    first = record_scheduled_slot(directory, on_time)
    assert first["on_time_count"] == 1
    catch_up = _on_time(_SLOT, catch_up=True)
    second = record_scheduled_slot(directory, catch_up)
    assert second["counted"] is False
    assert second["on_time"] is False
    assert second["data_filled"] is True
    assert second["on_time_count"] == 1
    ledger = json.loads((directory / "shadow-slots.json").read_text(encoding="utf-8"))
    assert ledger["slots"][on_time["slot_id"]]["on_time_count"] == 1

    other = _SLOT + timedelta(hours=3)
    missed_record = record_scheduled_slot(directory, missed)
    assert missed_record["on_time_count"] == 0
    filled = _on_time(
        other,
        start=other + timedelta(hours=2),
        seal=other + timedelta(hours=3),
        catch_up=True,
    )
    assert filled["counted"] is False
    assert filled["data_filled"] is True
    after = record_scheduled_slot(directory, filled)
    assert after["on_time_count"] == 0
    assert after["data_filled"] is True
    assert after["on_time_count"] != 2


def test_mcm_share_s3_05_dry_run_only_no_systemctl_without_apply_and_docs(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """MCM-SHARE-S3-05. Dry-run writes only the requested preview and does not call systemctl."""
    docs = (ROOT / "docs" / "box-unattended.md").read_text(encoding="utf-8")
    lowered = docs.lower()
    assert "license: pending" in lowered
    assert "loginctl show-user" in lowered
    assert "user manager" in lowered
    assert "windows retirement" in lowered
    assert "host smoke" in lowered
    assert "state watermark" in lowered
    assert "backup" in lowered
    assert "only after" in lowered
    assert "common-cause" in lowered
    assert "whole host is down" in lowered
    assert "does not mean every missed slot ran on time" in lowered
    assert "this package does not disable it" in lowered

    outside = tmp_path / "outside"
    outside.mkdir()
    sentinel = outside / "sentinel.txt"
    sentinel.write_text("keep", encoding="utf-8")
    home_units = Path.home() / ".config" / "systemd" / "user"
    before_home = sorted(path.name for path in home_units.glob("guardian-*")) if home_units.is_dir() else []
    output = tmp_path / "preview"
    calls: list[list[str]] = []

    def runner(argv: list[str]) -> CommandResult:
        calls.append(list(argv))
        if argv[0] == "loginctl":
            return CommandResult(0, "Linger=yes\nState=lingering\n", "")
        if argv[0] == "systemctl":
            return CommandResult(0, "enabled\n", "")
        raise AssertionError(argv)

    executable = tmp_path / "bin" / "guardian"
    code = main(
        [
            "install-service",
            "--dry-run",
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
            "--user",
            "guardian-monitor",
            "--offset-minutes",
            "17",
        ],
        service_runner=runner,
    )
    payload = json.loads(capsys.readouterr().out)
    assert code == 1
    assert payload["dry_run"] is True
    assert payload["installed"] is False
    assert payload["sender_started"] is False
    assert payload["systemctl_called"] is False
    assert payload["linger"]["present"] is True
    assert payload["linger"]["user_manager"] is True
    assert payload["fetches_code"] is False
    assert not any(command[0] == "systemctl" for command in calls)
    preview = output / "guardian-mcm-shadow.service"
    assert preview.is_file()
    assert str(executable.resolve()) in preview.read_text(encoding="utf-8")
    for written in payload["wrote"]:
        assert Path(written).resolve().is_relative_to(output.resolve())
    assert sentinel.read_text(encoding="utf-8") == "keep"
    assert not any(outside.rglob("*.service"))

    calls.clear()
    both = main(
        [
            "install-service",
            "--dry-run",
            "--apply",
            "--output",
            str(tmp_path / "ignored-apply"),
            "--executable",
            str(executable),
            "--config-dir",
            str(tmp_path / "config"),
            "--state-dir",
            str(tmp_path / "state"),
            "--user",
            "guardian-monitor",
        ],
        service_runner=runner,
    )
    mixed = json.loads(capsys.readouterr().out)
    assert both == 1
    assert mixed["installed"] is False
    assert mixed["systemctl_called"] is False
    assert not any(command[0] == "systemctl" for command in calls)

    calls.clear()
    applied_dir = tmp_path / "applied"
    applied = execute_install_service(
        dry_run=False,
        apply=True,
        mcm_config=str(tmp_path / "config" / "monitor.json"),
        output_dir=str(applied_dir),
        executable=str(executable),
        config_directory=str(tmp_path / "config"),
        state_directory=str(tmp_path / "state"),
        user="guardian-monitor",
        runner=runner,
        systemd_present=True,
        cron_present=False,
    )
    assert applied["installed"] is True
    assert applied["sender_started"] is False
    assert applied["systemctl_called"] is True
    assert applied["schedule_mode"] == "systemd_user_timer"
    assert any(command[0] == "systemctl" and "enable" in command for command in calls)
    assert all("start" not in command and "--now" not in command for command in calls)
    assert (applied_dir / "guardian-mcm-shadow.timer").is_file()
    for written in applied["wrote"]:
        assert Path(written).resolve().is_relative_to(applied_dir.resolve())
    refused = execute_install_service(
        dry_run=True,
        apply=False,
        mcm_config=str(tmp_path / "config" / "monitor.json"),
        output_dir=str(tmp_path / "refused"),
        executable=str(executable),
        config_directory=str(tmp_path / "config"),
        state_directory=str(tmp_path / "state"),
        user="guardian-monitor",
        runner=runner,
        systemd_present=True,
        cron_present=False,
    )
    assert refused["installed"] is False
    assert refused["systemctl_called"] is False
    assert sentinel.read_text(encoding="utf-8") == "keep"
    after_home = sorted(path.name for path in home_units.glob("guardian-*")) if home_units.is_dir() else []
    assert after_home == before_home


def _paths(root: Path) -> dict[str, str]:
    return {
        "config_directory": str(root / "config"),
        "executable": str(root / "bin" / "guardian"),
        "legacy_executable": str(root / "bin" / "legacy-collector"),
        "mcm_config": str(root / "config" / "monitor.json"),
        "state_directory": str(root / "state"),
    }


def _plan(paths: dict[str, str], runner, *, systemd_present: bool, cron_present: bool) -> dict:
    return execute_install_service(
        dry_run=True,
        apply=False,
        output_dir=None,
        runner=runner,
        systemd_present=systemd_present,
        cron_present=cron_present,
        user="guardian-monitor",
        offset_minutes=5,
        **paths,
    )


def _runner(stdout: str):
    calls: list[list[str]] = []

    def run(argv: list[str]) -> CommandResult:
        calls.append(list(argv))
        if argv and argv[0] == "systemctl":
            raise AssertionError("systemctl during dry-run")
        return CommandResult(0, stdout, "")

    return run, calls


def _on_time(
    slot: datetime,
    *,
    start: datetime | None = None,
    seal: datetime | None = None,
    required_scans_ok: bool = True,
    comparison_sealed: bool = True,
    evidence_present: bool = True,
    timed_out: bool = False,
    missed: bool = False,
    catch_up: bool = False,
) -> dict:
    return evaluate_scheduled_slot(
        slot_at=slot,
        started_at=slot + timedelta(minutes=5) if start is None else start,
        sealed_at=slot + timedelta(minutes=20) if seal is None else seal,
        required_scans_ok=required_scans_ok,
        comparison_sealed=comparison_sealed,
        evidence_present=evidence_present,
        timed_out=timed_out,
        missed=missed,
        catch_up=catch_up,
    )
