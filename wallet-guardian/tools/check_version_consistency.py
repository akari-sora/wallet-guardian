"""Check that the preview version strings agree.

Sources: ``pyproject.toml``, ``guardian.__version__`` when that assignment is
present, the ``guardian`` entry in ``uv.lock``, the first versioned changelog heading,
and ``release/manifest.draft.json``.

PEP 440 ``0.4.0rc2`` maps to ``v0.4.0-rc.2``.
This tool does not create a tag.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

EXPECTED = "0.4.0rc2"
_RC = re.compile(r"^(\d+\.\d+\.\d+)rc(\d+)$")
_PYPROJECT = re.compile(r'(?m)^version = "([^"]+)"')
_INIT = re.compile(r'(?m)^__version__ = "([^"]+)"')
_CHANGELOG = re.compile(r"(?m)^## \[([^\]]+)\]")


def repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def tag_name_for_pep440(version: str) -> str | None:
    """Map ``0.4.0rc2`` to ``v0.4.0-rc.2``. Other versions return None."""
    match = _RC.fullmatch(version)
    if match is None:
        return None
    return f"v{match.group(1)}-rc.{int(match.group(2))}"


def collect_versions(root: Path) -> dict[str, str | None]:
    """Read the five version sources. A missing ``__version__`` stays None."""
    pyproject = (root / "pyproject.toml").read_text(encoding="utf-8")
    py_match = _PYPROJECT.search(pyproject)
    init_path = root / "src" / "guardian" / "__init__.py"
    guardian: str | None = None
    if init_path.is_file():
        init_match = _INIT.search(init_path.read_text(encoding="utf-8"))
        if init_match is None:
            raise ValueError("guardian version missing")
        guardian = init_match.group(1)
    lock = _lock_version((root / "uv.lock").read_text(encoding="utf-8"))
    changelog = (root / "CHANGELOG.md").read_text(encoding="utf-8")
    # [Unreleased] sits above the version heading and is not a release version.
    headings = [item for item in _CHANGELOG.findall(changelog) if item.lower() != "unreleased"]
    change_match = headings[0] if headings else None
    manifest = json.loads((root / "release" / "manifest.draft.json").read_text(encoding="utf-8"))
    manifest_version = manifest.get("version") if isinstance(manifest, dict) else None
    return {
        "pyproject": None if py_match is None else py_match.group(1),
        "guardian": guardian,
        "uv_lock": lock,
        "changelog": change_match,
        "manifest": manifest_version if isinstance(manifest_version, str) else None,
    }


def consistency_errors(root: Path) -> list[str]:
    found = collect_versions(root)
    errors: list[str] = []
    present = {name: value for name, value in found.items() if value is not None}
    if not present:
        return ["no version sources"]
    distinct = set(present.values())
    if len(distinct) != 1:
        rendered = ", ".join(f"{name}={value}" for name, value in present.items())
        errors.append("mismatch: " + rendered)
    if found.get("pyproject") != EXPECTED:
        errors.append(f"pyproject={found.get('pyproject')} expected {EXPECTED}")
    for name, value in present.items():
        if value != EXPECTED:
            errors.append(f"{name}={value} expected {EXPECTED}")
    if found.get("guardian") is None and (root / "src" / "guardian" / "__init__.py").is_file():
        errors.append("guardian version missing")
    return errors


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Check preview version strings")
    parser.add_argument("--root")
    args = parser.parse_args(argv)
    root = Path(args.root) if args.root else repo_root()
    try:
        found = collect_versions(root)
        errors = consistency_errors(root)
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        sys.stderr.write(str(exc) + "\n")
        return 1
    version = found.get("pyproject") or ""
    report: dict[str, Any] = {
        "versions": found,
        "expected": EXPECTED,
        "tag_name": tag_name_for_pep440(version) if version else None,
        "tag_created": False,
        "ok": not errors,
        "errors": errors,
    }
    sys.stdout.write(json.dumps(report, indent=2) + "\n")
    return 0 if report["ok"] else 1


def _lock_version(text: str) -> str | None:
    for block in text.split("[[package]]"):
        if re.search(r'(?m)^name = "wallet-guardian-monitor"$', block) is None:
            continue
        match = re.search(r'(?m)^version = "([^"]+)"', block)
        if match is not None:
            return match.group(1)
    return None


if __name__ == "__main__":
    raise SystemExit(main())
