"""Report-only capture of one legacy monitor run.

The live legacy routine is not wired to this module. A caller copies state in,
records the query windows it actually scanned, and publishes an immutable
directory. This module never writes the legacy state file, never opens the MCM
database, and never imports a sender.

Publish order: write ``<run_id>.tmp-<token>/``, fsync files and the directory,
rename onto ``<run_id>/``, then write ``COMPLETE`` with the manifest sha256.
A directory without ``COMPLETE``, or whose bytes do not match the manifest, is
not a run. The same ``run_id`` cannot be published twice.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import stat
import threading
from collections.abc import Mapping, Sequence
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from guardian.configuration import AccountsDocument, file_permission_bits, path_inside_work_tree
from guardian.errors import ConfigError
from guardian.resources import read_resource_json
from guardian.jsonschema_lite import ValidationError, validate

SCHEMA_VERSION = "guardian.legacy-run.v1"
EVENT_VERSION = "guardian.legacy-event.v1"
PAYLOAD_FILES = ("coverage.json", "events.jsonl", "state-after.json", "state-before.json")
WINDOW_STATUSES = frozenset({"complete", "error", "not_scanned", "partial"})
DIR_MODE = 0o700
FILE_MODE = 0o600
_RUN_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_LABEL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
_SHA = re.compile(r"^[0-9a-f]{64}$")
_HASH = re.compile(r"^0x[0-9a-fA-F]{64}$")
_ISO = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$")
_HOST = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
_LOCKS: dict[str, threading.Lock] = {}
_LOCK_GUARD = threading.Lock()
_SCHEMA: dict[str, Any] | None = None
_EVENT_SCHEMA: dict[str, Any] | None = None


def address_hash(identity_key: str) -> str:
    """Hash a normalized identity. The raw address is not the stored value."""
    if not isinstance(identity_key, str) or not identity_key:
        raise ConfigError("identity key is required")
    return hashlib.sha256(identity_key.encode("utf-8")).hexdigest()


def scope_identity_hash(accounts: Sequence[Mapping[str, str]]) -> str:
    """Scope identity over labels and address hashes, in a stable order."""
    rows = []
    for account in accounts:
        label = account.get("label")
        digest = account.get("address_hash")
        if not isinstance(label, str) or _LABEL.fullmatch(label) is None:
            raise ConfigError("account label is not usable")
        if not isinstance(digest, str) or _SHA.fullmatch(digest) is None:
            raise ConfigError("address hash is not usable")
        rows.append({"address_hash": digest, "label": label})
    rows.sort(key=lambda row: (row["label"], row["address_hash"]))
    payload = json.dumps({"accounts": rows}, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def scope_hash_for_accounts(accounts: AccountsDocument) -> str:
    """Scope hash for the accounts MCM loaded. Raw addresses stay in the hash input only."""
    rows = [
        {"address_hash": address_hash(account.normalized.identity_key), "label": account.label}
        for account in accounts.accounts
    ]
    return scope_identity_hash(rows)


def begin_capture(
    runs_dir: str | Path,
    *,
    run_id: str,
    legacy_state: str | Path,
    tool_name: str,
    tool_version: str,
    tool_hash: str,
    host_id: str,
    scope_hash: str,
    accounts: Sequence[Mapping[str, str]],
    started_at: str,
) -> dict[str, Any]:
    """Open one tmp run and copy state-before. The source file is only read."""
    _check_identity(run_id, tool_name, tool_version, tool_hash, host_id, scope_hash, accounts, started_at)
    if scope_identity_hash(accounts) != scope_hash:
        raise ConfigError("scope hash does not match accounts")
    root = _runs_root(runs_dir)
    with capture_writer_lock(root):
        _refuse_existing(root, run_id)
        token = secrets.token_hex(4)
        tmp = root / f"{run_id}.tmp-{token}"
        _mkdir(tmp)
        try:
            _copy_readonly(Path(legacy_state), tmp / "state-before.json")
            _write_bytes(tmp / "events.jsonl", b"")
            _write_json(tmp / "coverage.json", {"schema_version": "guardian.legacy-coverage.v1", "windows": []})
            _write_json(tmp / "session.json", {
                "accounts": [_public_account(account) for account in accounts],
                "host_id": host_id,
                "run_id": run_id,
                "scope_hash": scope_hash,
                "started_at": started_at,
                "tool_hash": tool_hash,
                "tool_name": tool_name,
                "tool_version": tool_version,
                "windows": [],
            })
            _fsync_tree(tmp)
        except Exception:
            _remove_tree(tmp)
            raise
    return {"phase": "begun", "run_id": run_id, "status": "ok"}


def record_capture(
    runs_dir: str | Path,
    *,
    run_id: str,
    windows: Sequence[Mapping[str, Any]] = (),
    events: Sequence[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    """Append query windows and sidecar events. Status values are stored as given."""
    _check_run_id(run_id)
    checked = [_check_window(window) for window in windows]
    encoded = [_encode_event(event) for event in events]
    root = _runs_root(runs_dir)
    with capture_writer_lock(root):
        _refuse_published(root, run_id)
        tmp = _open_tmp(root, run_id)
        session = _read_json(tmp / "session.json")
        stored = session.get("windows")
        if not isinstance(stored, list):
            stored = []
        stored.extend(checked)
        session["windows"] = stored
        _write_json(tmp / "session.json", session)
        _write_json(tmp / "coverage.json", {"schema_version": "guardian.legacy-coverage.v1", "windows": stored})
        if encoded:
            with (tmp / "events.jsonl").open("ab") as handle:
                handle.write("".join(encoded).encode("utf-8"))
                handle.flush()
                os.fsync(handle.fileno())
        _fsync_tree(tmp)
    return {"events": len(encoded), "phase": "recorded", "run_id": run_id, "status": "ok", "windows": len(checked)}


def finish_capture(
    runs_dir: str | Path,
    *,
    run_id: str,
    legacy_state: str | Path,
    exit_code: int,
    finished_at: str,
) -> dict[str, Any]:
    """Copy state-after, publish the directory, then write COMPLETE."""
    _check_run_id(run_id)
    if isinstance(exit_code, bool) or not isinstance(exit_code, int):
        raise ConfigError("exit code is not usable")
    finished = _parse_time(finished_at)
    if finished is None:
        raise ConfigError("finished_at is not usable")
    root = _runs_root(runs_dir)
    with capture_writer_lock(root):
        _refuse_published(root, run_id)
        tmp = _open_tmp(root, run_id)
        session = _read_json(tmp / "session.json")
        started = _parse_time(session.get("started_at"))
        if started is None or finished < started:
            raise ConfigError("finished_at is not usable")
        _copy_readonly(Path(legacy_state), tmp / "state-after.json")
        windows = session.get("windows") if isinstance(session.get("windows"), list) else []
        _write_json(tmp / "coverage.json", {"schema_version": "guardian.legacy-coverage.v1", "windows": windows})
        session_path = tmp / "session.json"
        session_path.unlink()
        manifest = {
            "exit_code": exit_code,
            "files": _file_list(tmp),
            "finished_at": _fmt(finished),
            "host_id": session.get("host_id"),
            "legacy_tool": {
                "hash": session.get("tool_hash"),
                "name": session.get("tool_name"),
                "version": session.get("tool_version"),
            },
            "run_id": run_id,
            "schema_version": SCHEMA_VERSION,
            "scope": {
                "accounts": session.get("accounts"),
                "scope_hash": session.get("scope_hash"),
            },
            "started_at": _fmt(started),
            "windows": windows,
        }
        encoded = _canonical(manifest)
        try:
            validate(json.loads(encoded.decode("utf-8")), _manifest_schema())
        except ValidationError as exc:
            raise ConfigError("legacy manifest is not valid") from exc
        _write_bytes(tmp / "manifest.json", encoded)
        _fsync_tree(tmp)
        final = root / run_id
        os.replace(tmp, final)
        _fsync_dir(root)
        if os.name == "posix":
            os.chmod(final, DIR_MODE)
        digest = hashlib.sha256((final / "manifest.json").read_bytes()).hexdigest()
        _atomic_file(final, "COMPLETE", (digest + "\n").encode("utf-8"))
        _fsync_dir(final)
        _fsync_dir(root)
    return {"manifest_sha256": digest, "phase": "finished", "run_id": run_id, "status": "ok"}


def inspect_runs(
    runs_dir: str | Path,
    *,
    expected_scope_hash: str,
    slot_start: str | None,
    slot_end: str | None,
    max_age_seconds: int,
) -> dict[str, Any]:
    """Read the newest usable run. This function does not create or modify files."""
    if isinstance(max_age_seconds, bool) or not isinstance(max_age_seconds, int) or max_age_seconds < 0:
        raise ConfigError("legacy max age is not usable")
    root = Path(runs_dir)
    empty = {"events": [], "finished_at": "", "reason": "legacy_evidence_missing", "run_id": "", "windows": []}
    if not root.is_dir():
        return empty
    candidates = _candidates(root)
    if not candidates:
        return empty
    classified = [_classify(path, expected_scope_hash, slot_start, slot_end, max_age_seconds) for path in candidates]
    usable = [item for item in classified if item["reason"] is None]
    if usable:
        return max(usable, key=lambda item: item["finished_at"])
    return max(classified, key=lambda item: item["sort_key"])


@contextmanager
def capture_writer_lock(runs_dir: Path) -> Iterator[None]:
    """One writer per runs directory. A second writer gets ``capture_locked``."""
    key = str(runs_dir.resolve())
    thread_lock = _thread_lock(key)
    if not thread_lock.acquire(blocking=False):
        raise ConfigError("capture_locked")
    fd: int | None = None
    locked = False
    try:
        lock_path = runs_dir / ".capture.lock"
        fd = os.open(lock_path, os.O_CREAT | os.O_RDWR | getattr(os, "O_BINARY", 0), FILE_MODE)
        if os.name == "posix":
            os.chmod(lock_path, FILE_MODE)
        _lock_fd(fd)
        locked = True
        yield
    finally:
        if fd is not None:
            if locked:
                _unlock_fd(fd)
            os.close(fd)
        thread_lock.release()


def manifest_schema() -> dict[str, Any]:
    return _manifest_schema()


def event_schema() -> dict[str, Any]:
    return _event_schema()


def _classify(
    path: Path,
    expected_scope_hash: str,
    slot_start: str | None,
    slot_end: str | None,
    max_age_seconds: int,
) -> dict[str, Any]:
    sort_key = _mtime_key(path)
    base = {"events": [], "finished_at": "", "run_id": "", "sort_key": sort_key, "windows": []}
    if ".tmp-" in path.name:
        return {**base, "reason": "legacy_incomplete_marker"}
    verified = _verify_published(path)
    if verified["reason"] is not None:
        return {**base, "reason": verified["reason"], "run_id": verified.get("run_id") or ""}
    manifest = verified["manifest"]
    finished_at = str(manifest.get("finished_at") or "")
    windows = manifest.get("windows") if isinstance(manifest.get("windows"), list) else []
    body = {
        **base,
        "finished_at": finished_at,
        "run_id": str(manifest.get("run_id") or ""),
        "sort_key": finished_at or sort_key,
        "windows": windows,
    }
    scope = manifest.get("scope") if isinstance(manifest.get("scope"), dict) else {}
    if scope.get("scope_hash") != expected_scope_hash:
        return {**body, "reason": "legacy_identity_mismatch"}
    slot_end_at = _parse_time(slot_end)
    finished = _parse_time(finished_at)
    if slot_end_at is None or finished is None:
        return {**body, "reason": "legacy_window_gap"}
    if (slot_end_at - finished).total_seconds() > max_age_seconds:
        return {**body, "reason": "legacy_stale"}
    reason = _window_reason(manifest, windows, slot_start, slot_end)
    if reason is not None:
        return {**body, "reason": reason}
    events, event_reason = _load_events(path / "events.jsonl")
    if event_reason is not None:
        return {**body, "reason": event_reason}
    return {**body, "events": events, "reason": None}


def _verify_published(path: Path) -> dict[str, Any]:
    complete = path / "COMPLETE"
    manifest_path = path / "manifest.json"
    if not complete.is_file() or not manifest_path.is_file():
        return {"manifest": None, "reason": "legacy_incomplete_marker", "run_id": path.name}
    try:
        manifest_bytes = _read_only(manifest_path)
        marker = _read_only(complete).decode("utf-8").strip()
    except (ConfigError, UnicodeError):
        return {"manifest": None, "reason": "legacy_hash_mismatch", "run_id": path.name}
    if marker != hashlib.sha256(manifest_bytes).hexdigest():
        return {"manifest": None, "reason": "legacy_hash_mismatch", "run_id": path.name}
    try:
        manifest = json.loads(manifest_bytes.decode("utf-8"))
        validate(manifest, _manifest_schema())
    except (UnicodeError, json.JSONDecodeError, ValidationError):
        return {"manifest": None, "reason": "legacy_hash_mismatch", "run_id": path.name}
    if not isinstance(manifest, dict):
        return {"manifest": None, "reason": "legacy_hash_mismatch", "run_id": path.name}
    names = []
    for item in manifest.get("files") or []:
        if not isinstance(item, dict):
            return {"manifest": None, "reason": "legacy_hash_mismatch", "run_id": path.name}
        name = item.get("name")
        if not isinstance(name, str) or name not in PAYLOAD_FILES:
            return {"manifest": None, "reason": "legacy_hash_mismatch", "run_id": path.name}
        file_path = path / name
        if not file_path.is_file():
            return {"manifest": None, "reason": "legacy_hash_mismatch", "run_id": path.name}
        try:
            data = _read_only(file_path)
        except ConfigError:
            return {"manifest": None, "reason": "legacy_hash_mismatch", "run_id": path.name}
        if len(data) != item.get("size") or hashlib.sha256(data).hexdigest() != item.get("sha256"):
            return {"manifest": None, "reason": "legacy_hash_mismatch", "run_id": path.name}
        names.append(name)
    if set(names) != set(PAYLOAD_FILES):
        return {"manifest": None, "reason": "legacy_hash_mismatch", "run_id": path.name}
    try:
        coverage = json.loads(_read_only(path / "coverage.json").decode("utf-8"))
    except (ConfigError, UnicodeError, json.JSONDecodeError):
        return {"manifest": None, "reason": "legacy_hash_mismatch", "run_id": path.name}
    covered = coverage.get("windows") if isinstance(coverage, dict) else None
    if covered != manifest.get("windows"):
        return {"manifest": None, "reason": "legacy_hash_mismatch", "run_id": path.name}
    return {"manifest": manifest, "reason": None, "run_id": manifest.get("run_id")}


def _window_reason(
    manifest: Mapping[str, Any],
    windows: Sequence[Any],
    slot_start: str | None,
    slot_end: str | None,
) -> str | None:
    start = _parse_time(slot_start)
    end = _parse_time(slot_end)
    run_start = _parse_time(manifest.get("started_at"))
    run_end = _parse_time(manifest.get("finished_at"))
    if start is None or end is None or run_start is None or run_end is None:
        return "legacy_window_gap"
    if run_start > end or run_end < start:
        return "legacy_window_gap"
    if not windows:
        return "legacy_window_gap"
    saw_partial = False
    for window in windows:
        if not isinstance(window, dict):
            return "legacy_window_gap"
        status = window.get("status")
        if status in {"error", "partial"}:
            saw_partial = True
            continue
        if status == "not_scanned":
            return "legacy_window_gap"
        if status != "complete":
            return "legacy_window_gap"
        if not _timestamp_in(window.get("target_timestamp"), start, end):
            return "legacy_window_gap"
        left = window.get("from_block")
        right = window.get("to_block")
        if isinstance(left, bool) or isinstance(right, bool) or not isinstance(left, int) or not isinstance(right, int):
            return "legacy_window_gap"
        if left > right:
            return "legacy_window_gap"
    if saw_partial:
        return "legacy_partial"
    return None


def _load_events(path: Path) -> tuple[list[dict[str, Any]], str | None]:
    try:
        text = _read_only(path).decode("utf-8")
    except (ConfigError, UnicodeError):
        return [], "legacy_incomplete_marker"
    events: list[dict[str, Any]] = []
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            item = json.loads(line)
            validate(item, _event_schema())
        except (json.JSONDecodeError, ValidationError):
            return [], "legacy_incomplete_marker"
        if isinstance(item, dict):
            events.append(item)
    return events, None


def _candidates(root: Path) -> list[Path]:
    found: list[Path] = []
    try:
        children = list(root.iterdir())
    except OSError:
        return []
    for path in children:
        if not path.is_dir():
            continue
        if path.name.startswith("."):
            continue
        found.append(path)
    return found


def _runs_root(runs_dir: str | Path) -> Path:
    root = Path(runs_dir).expanduser()
    resolved = root.resolve()
    if path_inside_work_tree(resolved):
        raise ConfigError("runs dir is inside the repo")
    if root.exists() and not root.is_dir():
        raise ConfigError("runs dir is not a directory")
    _mkdir(root)
    return root


def _refuse_existing(root: Path, run_id: str) -> None:
    _refuse_published(root, run_id)
    if _tmp_matches(root, run_id):
        raise ConfigError("run_id is immutable")


def _refuse_published(root: Path, run_id: str) -> None:
    if (root / run_id).exists():
        raise ConfigError("run_id is immutable")


def _open_tmp(root: Path, run_id: str) -> Path:
    matches = _tmp_matches(root, run_id)
    if len(matches) != 1:
        raise ConfigError("capture session was not found")
    return matches[0]


def _tmp_matches(root: Path, run_id: str) -> list[Path]:
    prefix = run_id + ".tmp-"
    return [path for path in root.iterdir() if path.is_dir() and path.name.startswith(prefix)]


def _check_identity(
    run_id: str,
    tool_name: str,
    tool_version: str,
    tool_hash: str,
    host_id: str,
    scope_hash: str,
    accounts: Sequence[Mapping[str, str]],
    started_at: str,
) -> None:
    _check_run_id(run_id)
    if ".tmp-" in run_id:
        raise ConfigError("run_id is not usable")
    if not isinstance(tool_name, str) or _NAME.fullmatch(tool_name) is None:
        raise ConfigError("tool name is not usable")
    if not isinstance(tool_version, str) or _NAME.fullmatch(tool_version) is None:
        raise ConfigError("tool version is not usable")
    if not isinstance(tool_hash, str) or _SHA.fullmatch(tool_hash) is None:
        raise ConfigError("tool hash is not usable")
    if not isinstance(host_id, str) or _HOST.fullmatch(host_id) is None:
        raise ConfigError("host id is not usable")
    if not isinstance(scope_hash, str) or _SHA.fullmatch(scope_hash) is None:
        raise ConfigError("scope hash is not usable")
    if not isinstance(accounts, Sequence) or isinstance(accounts, (str, bytes)):
        raise ConfigError("accounts are not usable")
    if _parse_time(started_at) is None:
        raise ConfigError("started_at is not usable")
    for account in accounts:
        _public_account(account)


def _check_run_id(run_id: str) -> None:
    if not isinstance(run_id, str) or _RUN_ID.fullmatch(run_id) is None:
        raise ConfigError("run_id is not usable")


def _public_account(account: Mapping[str, str]) -> dict[str, str]:
    label = account.get("label")
    digest = account.get("address_hash")
    if not isinstance(label, str) or _LABEL.fullmatch(label) is None:
        raise ConfigError("account label is not usable")
    if not isinstance(digest, str) or _SHA.fullmatch(digest) is None:
        raise ConfigError("address hash is not usable")
    return {"address_hash": digest, "label": label}


def _check_window(window: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(window, Mapping):
        raise ConfigError("query window is not usable")
    status = window.get("status")
    if status not in WINDOW_STATUSES:
        raise ConfigError("query window status is not usable")
    network = window.get("network")
    capability = window.get("capability")
    source = window.get("source")
    if not isinstance(network, str) or not network or "://" in network or len(network) > 64:
        raise ConfigError("query window network is not usable")
    if not isinstance(capability, str) or not capability or len(capability) > 96:
        raise ConfigError("query window capability is not usable")
    if not isinstance(source, str) or not source or "://" in source or len(source) > 96:
        raise ConfigError("query window source is not usable")
    left = window.get("from_block")
    right = window.get("to_block")
    if isinstance(left, bool) or isinstance(right, bool) or not isinstance(left, int) or not isinstance(right, int):
        raise ConfigError("query window blocks are not usable")
    if left < 0 or right < left:
        raise ConfigError("query window blocks are not usable")
    target_hash = window.get("target_hash")
    if not isinstance(target_hash, str) or _HASH.fullmatch(target_hash) is None:
        raise ConfigError("query window target hash is not usable")
    if _parse_time(window.get("target_timestamp")) is None:
        raise ConfigError("query window target timestamp is not usable")
    checked = {
        "capability": capability,
        "from_block": left,
        "network": network,
        "source": source,
        "status": status,
        "target_hash": target_hash,
        "target_timestamp": _fmt(_parse_time(window.get("target_timestamp"))),
        "to_block": right,
    }
    return checked


def _encode_event(event: Mapping[str, Any]) -> str:
    if not isinstance(event, Mapping):
        raise ConfigError("sidecar event is not usable")
    try:
        validate(dict(event), _event_schema())
    except ValidationError as exc:
        raise ConfigError("sidecar event is not usable") from exc
    return json.dumps(dict(event), ensure_ascii=False, separators=(",", ":"), sort_keys=True) + "\n"


def _file_list(directory: Path) -> list[dict[str, Any]]:
    rows = []
    for name in PAYLOAD_FILES:
        data = _read_only(directory / name)
        rows.append({"name": name, "sha256": hashlib.sha256(data).hexdigest(), "size": len(data)})
    return rows


def _copy_readonly(source: Path, target: Path) -> None:
    first = _read_only(source)
    _write_bytes(target, first)
    second = _read_only(source)
    if second != first:
        target.unlink(missing_ok=True)
        raise ConfigError("legacy state changed during copy")


def _read_only(path: Path) -> bytes:
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_BINARY", 0))
    except OSError as exc:
        raise ConfigError("legacy state was not found") from exc
    try:
        chunks: list[bytes] = []
        while True:
            block = os.read(fd, 1024 * 1024)
            if not block:
                break
            chunks.append(block)
        return b"".join(chunks)
    finally:
        os.close(fd)


def _write_json(path: Path, payload: Any) -> None:
    _write_bytes(path, _canonical(payload))


def _canonical(payload: Any) -> bytes:
    return (json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")


def _write_bytes(path: Path, data: bytes) -> None:
    fd = os.open(path, os.O_CREAT | os.O_TRUNC | os.O_WRONLY | getattr(os, "O_BINARY", 0), FILE_MODE)
    try:
        os.write(fd, data)
        os.fsync(fd)
    finally:
        os.close(fd)
    if os.name == "posix":
        os.chmod(path, FILE_MODE)
        mode = stat.S_IMODE(path.stat().st_mode)
        if mode & 0o077:
            path.unlink(missing_ok=True)
            raise ConfigError("capture file is group or world readable")


def _atomic_file(directory: Path, name: str, data: bytes) -> None:
    temporary = directory / f".{name}.{secrets.token_hex(4)}.tmp"
    _write_bytes(temporary, data)
    _fsync_dir(directory)
    os.replace(temporary, directory / name)
    _fsync_dir(directory)


def _mkdir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True, mode=DIR_MODE)
    if os.name == "posix":
        os.chmod(path, DIR_MODE)
        mode = file_permission_bits(path)
        if mode is not None and mode & 0o077:
            raise ConfigError("capture dir perms looser than 0700")


def _fsync_tree(directory: Path) -> None:
    flags = os.O_RDWR if os.name == "nt" else os.O_RDONLY
    for child in sorted(directory.iterdir()):
        if child.is_file():
            fd = os.open(child, flags | getattr(os, "O_BINARY", 0))
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
    _fsync_dir(directory)


def _fsync_dir(path: Path) -> None:
    if os.name != "posix":
        return
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _read_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(_read_only(path).decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ConfigError("capture session is not valid json") from exc
    if not isinstance(payload, dict):
        raise ConfigError("capture session is not valid json")
    return payload


def _remove_tree(path: Path) -> None:
    if not path.exists():
        return
    for child in sorted(path.rglob("*"), reverse=True):
        if child.is_file() or child.is_symlink():
            child.unlink(missing_ok=True)
        elif child.is_dir():
            child.rmdir()
    path.rmdir()


def _thread_lock(key: str) -> threading.Lock:
    with _LOCK_GUARD:
        lock = _LOCKS.get(key)
        if lock is None:
            lock = threading.Lock()
            _LOCKS[key] = lock
        return lock


def _lock_fd(fd: int) -> None:
    try:
        if os.name == "nt":
            import msvcrt

            if os.lseek(fd, 0, os.SEEK_END) < 1:
                os.write(fd, b"\0")
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            return
        import fcntl

        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        raise ConfigError("capture_locked") from exc


def _unlock_fd(fd: int) -> None:
    if os.name == "nt":
        import msvcrt

        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        return
    import fcntl

    fcntl.flock(fd, fcntl.LOCK_UN)


def _parse_time(value: Any) -> datetime | None:
    if not isinstance(value, str) or _ISO.fullmatch(value) is None:
        return None
    text = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def _fmt(value: datetime | None) -> str:
    if value is None:
        return ""
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _timestamp_in(value: Any, start: datetime, end: datetime) -> bool:
    moment = _parse_time(value)
    return moment is not None and start <= moment <= end


def _mtime_key(path: Path) -> str:
    try:
        return f"{path.stat().st_mtime_ns:020d}"
    except OSError:
        return ""


def _manifest_schema() -> dict[str, Any]:
    global _SCHEMA
    if _SCHEMA is None:
        _SCHEMA = read_resource_json("schemas", "legacy-run.schema.json")
    return _SCHEMA


def _event_schema() -> dict[str, Any]:
    global _EVENT_SCHEMA
    if _EVENT_SCHEMA is None:
        _EVENT_SCHEMA = read_resource_json("schemas", "legacy-event.schema.json")
    return _EVENT_SCHEMA
