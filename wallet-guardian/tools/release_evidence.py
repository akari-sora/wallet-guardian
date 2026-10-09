"""Collect release evidence from an offline gate-run directory.

The tool reads JUnit files, command logs, tool versions, and artifact bytes.
It does not talk to the network, does not sign, and does not read private/,
data/, or evidence/. PASS is refused when a required step is missing, skipped,
failed, or marked allow_failure. A gitlab_runner label also needs pause and
cleanup readback. local_external is the label for an isolated run that did
not happen on the GitLab runner.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Any
from xml.etree import ElementTree

from guardian.security.redact import redact

SCHEMA_VERSION = "guardian.release-evidence.v1"
REQUIRED_STEPS = (
    "python-tests",
    "client-tests",
    "build-checks",
    "post-install-functional",
    "read-only-negative",
    "public-sample-probe",
)
JUNIT_STEPS = ("python-tests", "client-tests")
REQUIRED_TOOLS = ("python", "uv", "pytest", "node", "npm")
STATUSES = ("PASS", "FAIL", "SKIP", "BLOCKED", "NOT_RUN")
_RANK = {"PASS": 0, "NOT_RUN": 1, "BLOCKED": 2, "SKIP": 3, "FAIL": 4}
_SHA = re.compile(r"^[0-9a-f]{40}$")
_HASH = re.compile(r"^[0-9a-f]{64}$")
_ENV = re.compile(r"^(local_external|gitlab_runner:[A-Za-z0-9][A-Za-z0-9_-]{0,63})$")
_LABEL = re.compile(r"^(gitlab_runner:[A-Za-z0-9][A-Za-z0-9_-]{0,63}|local_external)?$")
_MAX_TEXT = 2000

_OUTPUT_KEYS = (
    "artifact_hashes",
    "commands",
    "content_hash",
    "environment",
    "execution_label",
    "external_execution",
    "junit",
    "public_sample_probe",
    "required_steps",
    "runner_cleanup_readback",
    "runner_pause_readback",
    "schema_version",
    "source_sha",
    "status",
    "status_reasons",
    "tool_versions",
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="release_evidence")
    parser.add_argument("--gate-dir", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args(argv)
    gate = Path(args.gate_dir)
    if not gate.is_dir():
        sys.stderr.write("gate directory is missing\n")
        return 2
    payload = collect_release_evidence(gate)
    _write_json(Path(args.output), payload)
    sys.stdout.write(payload["status"] + "\n")
    return 0 if payload["status"] == "PASS" else 1


def collect_release_evidence(gate: Path) -> dict[str, Any]:
    """Build release-evidence from files under ``gate``. Never raises on a bad bundle."""
    gate = gate.resolve()
    reasons: list[str] = []
    identity, identity_status, identity_reasons = _identity(gate)
    reasons.extend(identity_reasons)
    versions, version_status, version_reasons = _versions(gate)
    reasons.extend(version_reasons)
    probe, probe_status, probe_reasons = _probe(gate)
    artifacts, artifact_problem = _artifacts(gate)

    commands: list[dict[str, Any]] = []
    junit_rows: list[dict[str, Any]] = []
    step_status = "PASS"
    build_status = "NOT_RUN"
    for step_id in REQUIRED_STEPS:
        record, row, status, step_reasons = _step(
            gate,
            step_id,
            probe_status=probe_status if step_id == "public-sample-probe" else None,
            probe_reasons=probe_reasons if step_id == "public-sample-probe" else (),
        )
        commands.append(record)
        if row is not None:
            junit_rows.append(row)
        reasons.extend(step_reasons)
        step_status = _worse(step_status, status)
        if step_id == "build-checks":
            build_status = status

    artifact_status = "PASS"
    if artifact_problem == "mismatch":
        artifact_status = "FAIL"
        reasons.append("artifact_hash_mismatch")
    elif artifact_problem == "invalid":
        artifact_status = "BLOCKED"
        reasons.append("artifact_hashes_invalid")
    elif artifact_problem in {"missing", "empty"} and build_status == "PASS":
        artifact_status = "BLOCKED"
        reasons.append("artifact_hashes_missing")

    environment = identity["environment"]
    pause, pause_status, pause_reasons = _readback(
        gate / "runner" / "pause.json",
        environment=environment,
        flag="paused_after_use",
        kind="pause",
    )
    cleanup, cleanup_status, cleanup_reasons = _readback(
        gate / "runner" / "cleanup.json",
        environment=environment,
        flag="cleanup_confirmed",
        kind="cleanup",
    )
    reasons.extend(pause_reasons)
    reasons.extend(cleanup_reasons)

    label = environment if _ENV.fullmatch(environment) else ""
    status = "PASS"
    for item in (step_status, identity_status, version_status, artifact_status, pause_status, cleanup_status):
        status = _worse(status, item)

    payload: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "status": status,
        "status_reasons": _unique(reasons),
        "source_sha": identity["source_sha"],
        "content_hash": identity["content_hash"],
        "environment": environment if label else "",
        "execution_label": label,
        "external_execution": label == "local_external",
        "tool_versions": versions,
        "commands": commands,
        "junit": junit_rows,
        "artifact_hashes": artifacts,
        "public_sample_probe": probe,
        "runner_pause_readback": pause,
        "runner_cleanup_readback": cleanup,
        "required_steps": list(REQUIRED_STEPS),
    }
    if payload["status"] == "PASS" and payload["status_reasons"]:
        payload["status"] = "BLOCKED"
    structural = _structural_errors(payload)
    if structural:
        payload["status"] = _worse(str(payload["status"]), "BLOCKED")
        payload["status_reasons"] = _unique([*payload["status_reasons"], *(f"schema_invalid:{item}" for item in structural)])
    if payload["status"] == "PASS":
        invariant = _pass_errors(payload)
        if invariant:
            payload["status"] = "BLOCKED"
            payload["status_reasons"] = _unique([*payload["status_reasons"], *(f"pass_invariant:{item}" for item in invariant)])
    return payload


def validate_release_evidence(payload: dict[str, Any]) -> list[str]:
    """Structural schema subset. PASS invariants are included when status is PASS."""
    errors = _structural_errors(payload)
    if payload.get("status") == "PASS":
        errors.extend(_pass_errors(payload))
    return errors


def _step(
    gate: Path,
    step_id: str,
    *,
    probe_status: str | None,
    probe_reasons: tuple[str, ...] | list[str],
) -> tuple[dict[str, Any], dict[str, Any] | None, str, list[str]]:
    parsed, error = _parse_log(gate / "steps" / f"{step_id}.log")
    reasons: list[str] = []
    status = "PASS"
    command = ""
    exit_code: int | None = None
    allow = False
    junit_rel = ""
    if error == "missing":
        status = "NOT_RUN"
        reasons.append(f"step_missing:{step_id}")
    elif error is not None or parsed is None:
        status = "BLOCKED"
        reasons.append(f"step_log_invalid:{step_id}")
    else:
        command = redact(parsed["command"])
        if command != parsed["command"]:
            reasons.append(f"secret_redacted:{step_id}")
            status = _worse(status, "BLOCKED")
        if len(command) > _MAX_TEXT:
            command = command[:_MAX_TEXT]
            reasons.append(f"command_too_long:{step_id}")
            status = _worse(status, "BLOCKED")
        if not command:
            reasons.append(f"command_missing:{step_id}")
            status = _worse(status, "BLOCKED")
        exit_code = parsed["exit_code"]
        allow = parsed["allow_failure"]
        claimed = parsed["claimed"]
        if claimed == "PASS":
            pass
        elif claimed in STATUSES:
            status = _worse(status, claimed)
            reasons.append(f"step_{claimed.lower()}:{step_id}")
        elif claimed is not None:
            status = _worse(status, "BLOCKED")
            reasons.append(f"step_status_invalid:{step_id}")
        if exit_code != 0:
            status = _worse(status, "FAIL")
            reasons.append(f"exit_code:{step_id}:{exit_code}")
        if allow:
            reasons.append(f"allow_failure:{step_id}")
            status = _worse(status, "FAIL" if exit_code != 0 else "BLOCKED")
        junit_rel = parsed["junit"] or ""

    row: dict[str, Any] | None = None
    if step_id in JUNIT_STEPS or junit_rel:
        row, junit_status, junit_reasons = _junit(gate, step_id, junit_rel, required=step_id in JUNIT_STEPS)
        reasons.extend(junit_reasons)
        status = _worse(status, junit_status)
    if probe_status is not None:
        reasons.extend(probe_reasons)
        status = _worse(status, probe_status)

    record = {
        "id": step_id,
        "command": command,
        "exit_code": exit_code,
        "allow_failure": allow,
        "status": status,
    }
    return record, row, status, reasons


def _junit(
    gate: Path,
    step_id: str,
    relative: str,
    *,
    required: bool,
) -> tuple[dict[str, Any], str, list[str]]:
    blank = {
        "step": step_id,
        "path": relative,
        "present": False,
        "tests": None,
        "failures": None,
        "errors": None,
        "skipped": None,
    }
    if not relative:
        if required:
            return blank, "NOT_RUN", [f"junit_missing:{step_id}"]
        return blank, "PASS", []
    path = _safe_child(gate, relative)
    if path is None:
        return blank, "BLOCKED", [f"junit_path_rejected:{step_id}"]
    if not path.is_file():
        return blank, "NOT_RUN", [f"junit_missing:{step_id}"]
    counts, error = _parse_junit(path)
    if error is not None or counts is None:
        blank["path"] = relative
        return blank, "BLOCKED", [f"{error}:{step_id}"]
    row = {"step": step_id, "path": relative, "present": True, **counts}
    reasons: list[str] = []
    status = "PASS"
    if counts["failures"] or counts["errors"]:
        status = "FAIL"
        reasons.append(f"junit_failed:{step_id}")
    if counts["skipped"]:
        status = _worse(status, "SKIP")
        reasons.append(f"junit_skipped:{step_id}")
    if counts["tests"] <= 0:
        status = _worse(status, "NOT_RUN")
        reasons.append(f"junit_empty:{step_id}")
    return row, status, reasons


def _parse_junit(path: Path) -> tuple[dict[str, int] | None, str | None]:
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None, "junit_unreadable"
    upper = text.upper()
    if "<!DOCTYPE" in upper or "<!ENTITY" in upper:
        return None, "junit_doctype"
    try:
        root = ElementTree.fromstring(text)
    except ElementTree.ParseError:
        return None, "junit_invalid"
    suites = [element for element in root.iter() if _local(element.tag) == "testsuite"]
    tests = failures = errors = skipped = 0
    for suite in suites:
        tests += _attr_int(suite, "tests")
        failures += _attr_int(suite, "failures")
        errors += _attr_int(suite, "errors")
        skipped += _attr_int(suite, "skipped")
    cases = failure_nodes = error_nodes = skipped_nodes = 0
    for element in root.iter():
        name = _local(element.tag)
        if name == "testcase":
            cases += 1
        elif name == "failure":
            failure_nodes += 1
        elif name == "error":
            error_nodes += 1
        elif name == "skipped":
            skipped_nodes += 1
    return {
        "tests": max(tests, cases),
        "failures": max(failures, failure_nodes),
        "errors": max(errors, error_nodes),
        "skipped": max(skipped, skipped_nodes),
    }, None


def _parse_log(path: Path) -> tuple[dict[str, Any] | None, str | None]:
    if not path.is_file():
        return None, "missing"
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None, "unreadable"
    header: dict[str, str] = {}
    for line in text.splitlines():
        if line.strip() == "---":
            break
        if not line.strip():
            continue
        key, sep, value = line.partition(":")
        if not sep or not key.strip():
            return None, "invalid"
        header[key.strip()] = value.strip()
    command = header.get("command", "")
    exit_text = header.get("exit_code")
    allow_text = header.get("allow_failure")
    if exit_text is None or allow_text is None or "command" not in header:
        return None, "invalid"
    if not re.fullmatch(r"-?\d+", exit_text):
        return None, "invalid"
    if allow_text not in {"true", "false"}:
        return None, "invalid"
    claimed = header.get("status")
    junit = header.get("junit") or None
    return {
        "command": command,
        "exit_code": int(exit_text),
        "allow_failure": allow_text == "true",
        "claimed": claimed,
        "junit": junit,
    }, None


def _identity(gate: Path) -> tuple[dict[str, str], str, list[str]]:
    empty = {"source_sha": "", "content_hash": "", "environment": ""}
    path = gate / "identity.json"
    if not path.is_file():
        return empty, "NOT_RUN", ["identity_missing"]
    data, error = _read_json(path)
    if error or not isinstance(data, dict):
        return empty, "BLOCKED", ["identity_invalid"]
    reasons: list[str] = []
    status = "PASS"
    sha = data.get("source_sha", "")
    digest = data.get("content_hash", "")
    environment = data.get("environment", "")
    if not isinstance(sha, str) or not _SHA.fullmatch(sha):
        reasons.append("source_sha_invalid")
        status = "BLOCKED"
        sha = ""
    if not isinstance(digest, str) or not _HASH.fullmatch(digest):
        reasons.append("content_hash_invalid")
        status = "BLOCKED"
        digest = ""
    if not isinstance(environment, str) or not _ENV.fullmatch(environment):
        reasons.append("environment_invalid")
        status = "BLOCKED"
        environment = ""
    return {"source_sha": sha, "content_hash": digest, "environment": environment}, status, reasons


def _versions(gate: Path) -> tuple[dict[str, str], str, list[str]]:
    blank = {name: "" for name in REQUIRED_TOOLS}
    path = gate / "versions.json"
    if not path.is_file():
        return blank, "NOT_RUN", ["tool_versions_missing"]
    data, error = _read_json(path)
    if error or not isinstance(data, dict):
        return blank, "BLOCKED", ["tool_versions_invalid"]
    reasons: list[str] = []
    status = "PASS"
    versions: dict[str, str] = {}
    for name in REQUIRED_TOOLS:
        value = data.get(name, "")
        if not isinstance(value, str) or not value.strip() or len(value) > 64 or any(char in value for char in "\r\n"):
            versions[name] = ""
            reasons.append(f"tool_versions_missing:{name}")
            status = "BLOCKED"
        else:
            versions[name] = value.strip()
    return versions, status, reasons


def _probe(gate: Path) -> tuple[dict[str, str], str, list[str]]:
    blank = {"status": "NOT_RUN", "provenance": "", "sample_id": ""}
    path = gate / "probe" / "provenance.json"
    if not path.is_file():
        return blank, "NOT_RUN", ["public_sample_probe_missing"]
    data, error = _read_json(path)
    if error or not isinstance(data, dict):
        return blank, "BLOCKED", ["public_sample_probe_invalid"]
    status_text = data.get("status", "")
    provenance = data.get("provenance", "")
    sample_id = data.get("sample_id", "")
    if status_text not in STATUSES or not isinstance(provenance, str) or not isinstance(sample_id, str):
        return blank, "BLOCKED", ["public_sample_probe_invalid"]
    provenance = redact(provenance)[:_MAX_TEXT]
    sample_id = redact(sample_id)[:128]
    payload = {"status": status_text, "provenance": provenance, "sample_id": sample_id}
    if status_text != "PASS":
        return payload, status_text, [f"public_sample_probe_{status_text.lower()}"]
    if not provenance.strip() or not sample_id.strip():
        payload["status"] = "BLOCKED"
        return payload, "BLOCKED", ["public_sample_probe_incomplete"]
    return payload, "PASS", []


def _artifacts(gate: Path) -> tuple[list[dict[str, str]], str | None]:
    root = gate / "artifacts"
    if not root.is_dir():
        return [], "missing"
    rows: list[dict[str, str]] = []
    for path in sorted(item for item in root.rglob("*") if item.is_file() and item.name != "hashes.json"):
        if path.is_symlink() or not _contained(gate, path):
            return [], "invalid"
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        rows.append({"path": path.relative_to(gate).as_posix(), "sha256": digest})
    manifest = root / "hashes.json"
    if manifest.is_file():
        data, error = _read_json(manifest)
        if error or not isinstance(data, list):
            return rows, "invalid"
        expected: dict[str, str] = {}
        for item in data:
            if not isinstance(item, dict) or not isinstance(item.get("path"), str) or not isinstance(item.get("sha256"), str):
                return rows, "invalid"
            expected[item["path"]] = item["sha256"]
        actual = {row["path"]: row["sha256"] for row in rows}
        if expected != actual:
            return rows, "mismatch"
    if not rows:
        return [], "empty"
    return rows, None


def _readback(
    path: Path,
    *,
    environment: str,
    flag: str,
    kind: str,
) -> tuple[dict[str, Any], str, list[str]]:
    missing_reason = f"runner_{kind}_readback_missing"
    required = environment.startswith("gitlab_runner:")
    if not path.is_file():
        if required:
            return _readback_row(flag, "BLOCKED", None, "", "", missing_reason), "BLOCKED", [missing_reason]
        return (
            _readback_row(flag, "NOT_RUN", None, "", "", "not_required_for_local_external"),
            "PASS",
            [],
        )
    data, error = _read_json(path)
    if error or not isinstance(data, dict):
        reason = f"runner_{kind}_readback_invalid"
        return _readback_row(flag, "BLOCKED", None, "", "", reason), "BLOCKED", [reason]
    raw_flag = data.get(flag)
    readback = data.get("readback", "")
    recorded_at = data.get("recorded_at", "")
    if not isinstance(readback, str) or not isinstance(recorded_at, str) or not isinstance(raw_flag, bool):
        reason = f"runner_{kind}_readback_invalid"
        return _readback_row(flag, "BLOCKED", None, "", "", reason), "BLOCKED", [reason]
    masked = redact(readback)
    reasons: list[str] = []
    status = "PASS"
    if masked != readback:
        reasons.append(f"secret_redacted:runner_{kind}")
        status = "BLOCKED"
    readback = masked[:_MAX_TEXT]
    recorded_at = recorded_at[:64]
    if raw_flag is not True:
        status = "FAIL"
        reasons.append(f"runner_{kind}_readback_rejected")
    elif not readback.strip() or not recorded_at.strip():
        status = _worse(status, "BLOCKED")
        reasons.append(f"runner_{kind}_readback_empty")
    if not required and status == "PASS":
        # Present local readback is recorded. It does not change the local label.
        return _readback_row(flag, "PASS", raw_flag, readback, recorded_at, ""), "PASS", reasons
    if status != "PASS":
        return _readback_row(flag, status, raw_flag, readback, recorded_at, reasons[0] if reasons else ""), status, reasons
    return _readback_row(flag, "PASS", True, readback, recorded_at, ""), "PASS", []


def _readback_row(
    flag: str,
    status: str,
    flag_value: bool | None,
    readback: str,
    recorded_at: str,
    reason: str,
) -> dict[str, Any]:
    return {
        "status": status,
        flag: flag_value,
        "readback": readback,
        "recorded_at": recorded_at,
        "reason": reason,
    }


def _pass_errors(payload: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    label = payload.get("execution_label")
    if not isinstance(label, str) or not _ENV.fullmatch(label):
        errors.append("execution_label")
    if payload.get("environment") != label:
        errors.append("environment")
    if payload.get("external_execution") is not (label == "local_external"):
        errors.append("external_execution")
    if not _SHA.fullmatch(str(payload.get("source_sha") or "")):
        errors.append("source_sha")
    if not _HASH.fullmatch(str(payload.get("content_hash") or "")):
        errors.append("content_hash")
    versions = payload.get("tool_versions")
    if not isinstance(versions, dict) or any(not versions.get(name) for name in REQUIRED_TOOLS):
        errors.append("tool_versions")
    commands = {row.get("id"): row for row in payload.get("commands", []) if isinstance(row, dict)}
    if list(commands) != list(REQUIRED_STEPS):
        errors.append("commands")
    for step_id in REQUIRED_STEPS:
        row = commands.get(step_id)
        if not isinstance(row, dict) or row.get("status") != "PASS" or row.get("exit_code") != 0 or row.get("allow_failure") is not False:
            errors.append(f"command:{step_id}")
        elif not str(row.get("command") or "").strip():
            errors.append(f"command:{step_id}")
    junit = {row.get("step"): row for row in payload.get("junit", []) if isinstance(row, dict)}
    for step_id in JUNIT_STEPS:
        row = junit.get(step_id)
        if not isinstance(row, dict) or row.get("present") is not True:
            errors.append(f"junit:{step_id}")
            continue
        if not all(row.get(name) == 0 for name in ("failures", "errors", "skipped")) or not isinstance(row.get("tests"), int) or row["tests"] < 1:
            errors.append(f"junit:{step_id}")
    hashes = payload.get("artifact_hashes")
    if not isinstance(hashes, list) or not hashes:
        errors.append("artifact_hashes")
    else:
        for item in hashes:
            if not isinstance(item, dict) or not _HASH.fullmatch(str(item.get("sha256") or "")):
                errors.append("artifact_hashes")
                break
    probe = payload.get("public_sample_probe")
    if not isinstance(probe, dict) or probe.get("status") != "PASS" or not str(probe.get("provenance") or "").strip() or not str(probe.get("sample_id") or "").strip():
        errors.append("public_sample_probe")
    if isinstance(label, str) and label.startswith("gitlab_runner:"):
        for key, flag in (("runner_pause_readback", "paused_after_use"), ("runner_cleanup_readback", "cleanup_confirmed")):
            row = payload.get(key)
            if not isinstance(row, dict) or row.get("status") != "PASS" or row.get(flag) is not True or not str(row.get("readback") or "").strip():
                errors.append(key)
    if payload.get("status_reasons"):
        errors.append("status_reasons")
    if payload.get("required_steps") != list(REQUIRED_STEPS):
        errors.append("required_steps")
    return errors


def _structural_errors(payload: dict[str, Any]) -> list[str]:
    if not isinstance(payload, dict):
        return ["type"]
    errors: list[str] = []
    if set(payload) != set(_OUTPUT_KEYS):
        errors.append("keys")
    if payload.get("schema_version") != SCHEMA_VERSION:
        errors.append("schema_version")
    if payload.get("status") not in STATUSES:
        errors.append("status")
    reasons = payload.get("status_reasons")
    if not isinstance(reasons, list) or any(not isinstance(item, str) for item in reasons):
        errors.append("status_reasons")
    source_sha = payload.get("source_sha")
    if not isinstance(source_sha, str) or (source_sha != "" and _SHA.fullmatch(source_sha) is None):
        errors.append("source_sha")
    if not isinstance(payload.get("content_hash"), str) or (
        payload.get("content_hash") != "" and not _HASH.fullmatch(str(payload.get("content_hash")))
    ):
        errors.append("content_hash")
    if not isinstance(payload.get("environment"), str):
        errors.append("environment")
    if not isinstance(payload.get("execution_label"), str) or not _LABEL.fullmatch(str(payload.get("execution_label"))):
        errors.append("execution_label")
    if not isinstance(payload.get("external_execution"), bool):
        errors.append("external_execution")
    versions = payload.get("tool_versions")
    if not isinstance(versions, dict) or set(versions) != set(REQUIRED_TOOLS) or any(not isinstance(value, str) for value in versions.values()):
        errors.append("tool_versions")
    commands = payload.get("commands")
    if not isinstance(commands, list):
        errors.append("commands")
    else:
        for row in commands:
            if not isinstance(row, dict) or set(row) != {"id", "command", "exit_code", "allow_failure", "status"}:
                errors.append("commands")
                break
            if row["status"] not in STATUSES or not isinstance(row["allow_failure"], bool) or not isinstance(row["command"], str):
                errors.append("commands")
                break
            if row["exit_code"] is not None and not isinstance(row["exit_code"], int):
                errors.append("commands")
                break
    junit = payload.get("junit")
    if not isinstance(junit, list):
        errors.append("junit")
    else:
        for row in junit:
            if not isinstance(row, dict) or set(row) != {"step", "path", "present", "tests", "failures", "errors", "skipped"}:
                errors.append("junit")
                break
            if not isinstance(row["present"], bool) or not isinstance(row["path"], str):
                errors.append("junit")
                break
            for name in ("tests", "failures", "errors", "skipped"):
                if row[name] is not None and (not isinstance(row[name], int) or row[name] < 0):
                    errors.append("junit")
                    break
    hashes = payload.get("artifact_hashes")
    if not isinstance(hashes, list):
        errors.append("artifact_hashes")
    else:
        for item in hashes:
            if not isinstance(item, dict) or set(item) != {"path", "sha256"}:
                errors.append("artifact_hashes")
                break
            if not isinstance(item["path"], str) or not _HASH.fullmatch(item["sha256"]):
                errors.append("artifact_hashes")
                break
    probe = payload.get("public_sample_probe")
    if not isinstance(probe, dict) or set(probe) != {"status", "provenance", "sample_id"}:
        errors.append("public_sample_probe")
    elif probe.get("status") not in STATUSES or not isinstance(probe.get("provenance"), str) or not isinstance(probe.get("sample_id"), str):
        errors.append("public_sample_probe")
    for key, flag in (("runner_pause_readback", "paused_after_use"), ("runner_cleanup_readback", "cleanup_confirmed")):
        row = payload.get(key)
        if not isinstance(row, dict) or set(row) != {"status", flag, "readback", "recorded_at", "reason"}:
            errors.append(key)
            continue
        if row["status"] not in STATUSES or not isinstance(row["readback"], str) or not isinstance(row["recorded_at"], str) or not isinstance(row["reason"], str):
            errors.append(key)
        elif row[flag] is not None and not isinstance(row[flag], bool):
            errors.append(key)
    if payload.get("required_steps") != list(REQUIRED_STEPS):
        errors.append("required_steps")
    return errors


def _safe_child(gate: Path, relative: str) -> Path | None:
    if not relative or relative.startswith(("/", "\\")) or ":" in relative[:3]:
        return None
    parts = Path(relative).parts
    if ".." in parts:
        return None
    candidate = gate / relative
    if not _contained(gate, candidate):
        return None
    return candidate


def _contained(gate: Path, path: Path) -> bool:
    try:
        path.resolve().relative_to(gate.resolve())
    except ValueError:
        return False
    return True


def _read_json(path: Path) -> tuple[Any, str | None]:
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None, "unreadable"
    try:
        return json.loads(text), None
    except json.JSONDecodeError:
        return None, "invalid_json"


def _attr_int(element: ElementTree.Element, name: str) -> int:
    raw = element.attrib.get(name)
    if raw is None or not re.fullmatch(r"\d+", raw):
        return 0
    return int(raw)


def _local(tag: str) -> str:
    if "}" in tag:
        return tag.split("}", 1)[1]
    return tag


def _worse(left: str, right: str) -> str:
    return left if _RANK[left] >= _RANK[right] else right


def _unique(items: list[str]) -> list[str]:
    return sorted(set(items))


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(main())
