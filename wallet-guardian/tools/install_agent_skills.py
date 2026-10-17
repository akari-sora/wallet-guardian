"""Install the wallet-guardian skill into one agent home.

The default install is a dry-run. ``--apply`` writes. Existing destinations are
never overwritten. A SHA-256 marker is written next to the installed copy.
``doctor`` lists copies under ``HOME``. This tool reads ``HOME`` from the
environment and does not call the password database.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from pathlib import Path

MARKER = ".wallet-guardian-skill-sha256.json"
_CATEGORY = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,30}[a-z0-9])?$")


def repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def skill_source() -> Path:
    return repo_root() / "skills" / "wallet-guardian"


def _emit(payload: dict) -> None:
    sys.stdout.write(json.dumps(payload, indent=2, sort_keys=True) + "\n")


def resolve_home(explicit: str | None) -> Path:
    raw = explicit if explicit is not None else os.environ.get("HOME")
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError("HOME is not set")
    if raw == "~" or raw.startswith("~/") or raw.startswith("~\\"):
        raise ValueError("pass an absolute HOME")
    home = Path(raw).resolve()
    if not home.is_dir():
        raise ValueError("HOME is not a directory")
    return home


def _source_files(source: Path) -> dict[str, bytes]:
    if not source.is_dir():
        raise ValueError("skill source is missing")
    found: dict[str, bytes] = {}
    for path in sorted(source.rglob("*")):
        if not path.is_file():
            continue
        if "__pycache__" in path.parts or path.suffix == ".pyc":
            continue
        rel = path.relative_to(source).as_posix()
        if rel == MARKER or ".." in rel.split("/"):
            continue
        found[rel] = path.read_bytes()
    if "SKILL.md" not in found:
        raise ValueError("skill source is missing SKILL.md")
    return found


def _hashes(files: dict[str, bytes]) -> dict[str, str]:
    return {rel: hashlib.sha256(data).hexdigest() for rel, data in sorted(files.items())}


def _tree_hash(hashes: dict[str, str]) -> str:
    blob = "".join(f"{rel}\0{hashes[rel]}\n" for rel in sorted(hashes))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _marker_bytes(hashes: dict[str, str]) -> bytes:
    payload = {
        "algorithm": "sha256",
        "files": hashes,
        "name": "wallet-guardian",
        "tree_sha256": _tree_hash(hashes),
    }
    return (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode("utf-8")


def _category(agent: str, category: str | None) -> str | None:
    if agent != "hermes":
        if category is not None:
            raise ValueError("category is only for hermes")
        return None
    name = "crypto" if category is None else category
    if _CATEGORY.fullmatch(name) is None:
        raise ValueError("category must be one path segment")
    return name


def destination(home: Path, agent: str, category: str | None) -> Path:
    chosen = _category(agent, category)
    if agent == "claude":
        path = home / ".claude" / "skills" / "wallet-guardian"
    elif agent == "agents":
        path = home / ".agents" / "skills" / "wallet-guardian"
    elif agent == "hermes":
        path = home / ".hermes" / "skills" / str(chosen) / "wallet-guardian"
    else:
        raise ValueError("unknown agent")
    return _under_home(home, path)


def _under_home(home: Path, path: Path) -> Path:
    """Resolve ``path`` and refuse a destination that leaves ``home``."""
    home_r = home.resolve()
    cursor = path
    suffix: list[str] = []
    while not cursor.exists():
        suffix.append(cursor.name)
        parent = cursor.parent
        if parent == cursor:
            raise ValueError("destination escapes HOME")
        cursor = parent
    resolved = cursor.resolve()
    for name in reversed(suffix):
        resolved = resolved / name
    try:
        resolved.relative_to(home_r)
    except ValueError as exc:
        raise ValueError("destination escapes HOME") from exc
    return resolved


def _agent_of(home: Path, path: Path) -> str:
    try:
        rel = path.resolve().relative_to(home.resolve())
    except ValueError:
        return "unknown"
    parts = rel.parts
    if parts[:2] == (".claude", "skills"):
        return "claude"
    if parts[:2] == (".agents", "skills"):
        return "agents"
    if len(parts) >= 3 and parts[0] == ".hermes" and parts[1] == "skills":
        return "hermes"
    return "unknown"


def _escapes_home(home: Path, path: Path) -> bool:
    """True when ``path`` resolves outside ``home``, including through a parent symlink."""
    try:
        path.resolve().relative_to(home.resolve())
    except (OSError, RuntimeError, ValueError):
        return True
    return False


def _installed_files(home: Path, path: Path) -> tuple[dict[str, bytes], bool]:
    """Read files that stay under ``home``. An escaping symlink is not read."""
    found: dict[str, bytes] = {}
    escaped = False
    if not path.is_dir() or _escapes_home(home, path):
        return found, True
    for child in sorted(path.rglob("*")):
        rel = child.relative_to(path).as_posix()
        if child.is_symlink() and _escapes_home(home, child):
            escaped = True
            continue
        if _escapes_home(home, child):
            escaped = True
            continue
        if rel == MARKER or not child.is_file():
            continue
        found[rel] = child.read_bytes()
    return found, escaped


def _matches_marker(home: Path, path: Path, hashes: dict[str, str]) -> bool:
    marker = path / MARKER
    if not marker.is_file() or _escapes_home(home, marker):
        return False
    try:
        payload = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    if not isinstance(payload, dict):
        return False
    files = payload.get("files")
    return files == hashes and payload.get("tree_sha256") == _tree_hash(hashes)


def install_skill(home: Path, agent: str, category: str | None, apply: bool) -> dict:
    files = _source_files(skill_source())
    hashes = _hashes(files)
    dest = destination(home, agent, category)
    exists = dest.exists()
    report = {
        "agent": agent,
        "apply": apply,
        "conflicts": [dest.as_posix()] if exists else [],
        "destination": dest.as_posix(),
        "dry_run": not apply,
        "tree_sha256": _tree_hash(hashes),
        "would_write": [] if exists else sorted(files),
        "wrote": False,
    }
    if exists or not apply:
        return report
    dest.mkdir(parents=True)
    for rel, data in files.items():
        target = dest / rel
        if target.exists():
            raise ValueError("refusing to overwrite")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    (dest / MARKER).write_bytes(_marker_bytes(hashes))
    report["wrote"] = True
    report["would_write"] = sorted(files)
    return report


def _known_destinations(home: Path) -> list[Path]:
    found = [
        home / ".claude" / "skills" / "wallet-guardian",
        home / ".agents" / "skills" / "wallet-guardian",
    ]
    hermes = home / ".hermes" / "skills"
    if hermes.is_dir() and not hermes.is_symlink() and not _escapes_home(home, hermes):
        for child in sorted(hermes.iterdir()):
            if child.is_symlink() or not child.is_dir() or _escapes_home(home, child):
                continue
            if _CATEGORY.fullmatch(child.name):
                found.append(child / "wallet-guardian")
    return found


def doctor(home: Path) -> dict:
    source_hashes = _hashes(_source_files(skill_source()))
    installs = []
    ok = True
    for path in _known_destinations(home):
        try:
            present = os.path.lexists(path)
        except OSError:
            present = False
        if not present:
            continue
        if path.is_symlink() or not path.is_dir() or _escapes_home(home, path):
            installs.append({
                "agent": _agent_of(home, path),
                "matches_marker": False,
                "matches_source": False,
                "path": path.as_posix(),
            })
            ok = False
            continue
        installed, escaped = _installed_files(home, path)
        hashes = _hashes(installed)
        matches_source = (not escaped) and hashes == source_hashes
        matches_marker = (not escaped) and _matches_marker(home, path, hashes)
        if not matches_source or not matches_marker:
            ok = False
        row = {
            "agent": _agent_of(home, path),
            "matches_marker": matches_marker,
            "matches_source": matches_source,
            "path": path.as_posix(),
        }
        if not escaped:
            row["tree_sha256"] = _tree_hash(hashes)
        installs.append(row)
    return {"installs": installs, "ok": ok}


class _ExactFlagParser(argparse.ArgumentParser):
    """Long options must be spelled in full. A prefix is not a flag."""

    def __init__(self, *args: object, **kwargs: object) -> None:
        kwargs["allow_abbrev"] = False
        super().__init__(*args, **kwargs)

    def add_subparsers(self, **kwargs: object):  # type: ignore[no-untyped-def]
        kwargs.setdefault("parser_class", type(self))
        return super().add_subparsers(**kwargs)


def main(argv: list[str] | None = None) -> int:
    parser = _ExactFlagParser(description="Install the wallet-guardian skill. Default is dry-run.")
    parser.add_argument("--home", default=None)
    sub = parser.add_subparsers(dest="cmd", required=True)
    install = sub.add_parser("install", help="copy the skill; writes only with --apply")
    install.add_argument("--agent", required=True, choices=("claude", "agents", "hermes"))
    install.add_argument("--category", default=None)
    install.add_argument("--apply", action="store_true")
    sub.add_parser("doctor", help="list installed copies under HOME")
    args = parser.parse_args(argv)
    try:
        home = resolve_home(args.home)
        if args.cmd == "doctor":
            report = doctor(home)
            _emit(report)
            return 0 if report["ok"] else 1
        report = install_skill(home, args.agent, args.category, bool(args.apply))
    except ValueError as exc:
        _emit({"error": str(exc), "ok": False})
        return 2
    except OSError as exc:
        _emit({"error": str(exc), "ok": False})
        return 1
    if report["conflicts"] and args.apply:
        report["ok"] = False
        _emit(report)
        return 2
    report["ok"] = True
    _emit(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
