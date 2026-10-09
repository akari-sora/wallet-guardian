"""Packaged read-only files for an install that is not a git checkout.

Runtime config, schemas, migrations, and examples live under
``guardian/_resources`` and are opened with ``importlib.resources``.
Private config and state never live in this tree.
"""

from __future__ import annotations

import json
from contextlib import ExitStack
from importlib.resources import as_file, files
from pathlib import Path
from typing import Any

_STACK: ExitStack | None = None
_ROOT: Path | None = None


def packaged_root() -> Path:
    """Directory of ``guardian/_resources`` inside the loaded package."""
    global _STACK, _ROOT
    if _ROOT is None:
        stack = ExitStack()
        traversable = files("guardian").joinpath("_resources")
        root = Path(stack.enter_context(as_file(traversable))).resolve()
        package = _package_dir()
        try:
            root.relative_to(package)
        except ValueError as exc:
            stack.close()
            raise RuntimeError("packaged resources resolved outside the guardian package") from exc
        _STACK = stack
        _ROOT = root
    return _ROOT


def resource_path(*parts: str) -> Path:
    """Path to one packaged file or directory. Parts cannot escape the package."""
    for part in parts:
        if not isinstance(part, str) or part in {"", ".", ".."} or "/" in part or "\\" in part:
            raise ValueError("resource path must stay inside the package")
    return packaged_root().joinpath(*parts)


def read_resource_text(*parts: str) -> str:
    return resource_path(*parts).read_text(encoding="utf-8")


def read_resource_json(*parts: str) -> Any:
    return json.loads(read_resource_text(*parts))


def _package_dir() -> Path:
    import guardian

    return Path(guardian.__file__).resolve().parent
