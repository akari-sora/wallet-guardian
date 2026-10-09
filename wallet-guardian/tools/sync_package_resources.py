"""Copy the editable resource trees into the package so a wheel matches them.

Repo-root ``config/``, ``schemas/``, ``migrations/monitor/``, ``examples/``,
``deploy/systemd/``, and the network tier plan stay the files editors change.
The packaged copies under ``src/guardian/_resources/`` are what an installed
monitor loads.
``runtime-pin.json`` is not part of that copy.
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

_TREES = (
    ("config", "config"),
    ("schemas", "schemas"),
    ("migrations/monitor", "migrations/monitor"),
    ("examples", "examples"),
    ("deploy/systemd", "deploy/systemd"),
)
_FILES = (
    ("docs/mcm-01/network-tiers.plan.json", "docs/mcm-01/network-tiers.plan.json"),
)


def repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def sync_package_resources(root: Path | None = None) -> Path:
    """Replace packaged copies with the bytes of the editable trees."""
    root = repo_root() if root is None else Path(root)
    destination = root / "src" / "guardian" / "_resources"
    destination.mkdir(parents=True, exist_ok=True)
    for source_rel, dest_rel in _TREES:
        _sync_tree(root / source_rel, destination / dest_rel)
    for source_rel, dest_rel in _FILES:
        _sync_file(root / source_rel, destination / dest_rel)
    return destination


def _sync_tree(source: Path, destination: Path) -> None:
    if not source.is_dir():
        raise SystemExit(f"missing resource tree: {source}")
    if destination.exists():
        shutil.rmtree(destination)
    destination.mkdir(parents=True)
    for path in sorted(item for item in source.rglob("*") if item.is_file()):
        if "__pycache__" in path.parts or path.suffix in {".pyc", ".pyo"}:
            continue
        target = destination / path.relative_to(source)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(path.read_bytes())


def _sync_file(source: Path, destination: Path) -> None:
    if not source.is_file():
        raise SystemExit(f"missing resource file: {source}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(source.read_bytes())


def main() -> int:
    sync_package_resources()
    return 0


if __name__ == "__main__":
    sys.exit(main())
