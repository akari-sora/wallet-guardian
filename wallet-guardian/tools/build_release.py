"""Build a pinned guardian release: wheel, sdist, constraints, and SHA256SUMS.

``uv tool install`` does not apply ``uv.lock``. The constraints file is the pin
operators pass to ``uv pip`` or ``pip``. This script does not publish anywhere.
"""

from __future__ import annotations

import argparse
import hashlib
import re
import subprocess
import sys
from pathlib import Path

_VERSION = re.compile(r'(?m)^version = "([^"]+)"')
_INIT_VERSION = re.compile(r'(?m)^__version__ = "([^"]+)"')


def repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def project_version(root: Path) -> str:
    text = (root / "pyproject.toml").read_text(encoding="utf-8")
    match = _VERSION.search(text)
    if match is None:
        raise SystemExit("pyproject.toml has no version")
    return match.group(1)


def _require_same_version(root: Path, version: str) -> None:
    init = (root / "src" / "guardian" / "__init__.py").read_text(encoding="utf-8")
    found = _INIT_VERSION.search(init)
    if found is None or found.group(1) != version:
        raise SystemExit("src/guardian/__init__.py version does not match pyproject.toml")
    pin_path = root / "src" / "guardian" / "_resources" / "runtime-pin.json"
    pin = pin_path.read_text(encoding="utf-8")
    if f'"version": "{version}"' not in pin:
        raise SystemExit("runtime-pin.json version does not match pyproject.toml")


def _run(command: list[str], root: Path) -> None:
    completed = subprocess.run(command, cwd=root, check=False)
    if completed.returncode != 0:
        raise SystemExit(completed.returncode)


def build_release(root: Path, out_dir: Path) -> None:
    tools_dir = Path(__file__).resolve().parent
    if str(tools_dir) not in sys.path:
        sys.path.insert(0, str(tools_dir))
    from sync_package_resources import sync_package_resources

    version = project_version(root)
    sync_package_resources(root)
    _require_same_version(root, version)
    out_dir.mkdir(parents=True, exist_ok=True)
    _run(
        [
            "uv", "build", "--offline", "--clear", "--no-create-gitignore",
            "--out-dir", str(out_dir), str(root),
        ],
        root,
    )
    constraints = out_dir / "constraints.txt"
    _run(
        [
            "uv", "export", "--frozen", "--offline", "--no-dev", "--no-hashes",
            "--no-emit-project", "--no-header", "--no-annotate",
            "-o", str(constraints),
        ],
        root,
    )
    artifacts = sorted(
        path for path in out_dir.iterdir()
        if path.is_file() and path.name != "SHA256SUMS"
    )
    if not any(path.suffix == ".whl" for path in artifacts):
        raise SystemExit("uv build did not write a wheel")
    if not any(path.name.endswith(".tar.gz") for path in artifacts):
        raise SystemExit("uv build did not write an sdist")
    lines = []
    for path in artifacts:
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        lines.append(f"{digest}  {path.name}\n")
    (out_dir / "SHA256SUMS").write_text("".join(lines), encoding="utf-8", newline="\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build the guardian release artifacts")
    parser.add_argument("--out-dir", default="dist")
    args = parser.parse_args(argv)
    root = repo_root()
    out_dir = Path(args.out_dir)
    if not out_dir.is_absolute():
        out_dir = root / out_dir
    build_release(root, out_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
