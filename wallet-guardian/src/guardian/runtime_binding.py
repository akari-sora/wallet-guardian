"""Bind the running interpreter to this checkout.

An installed copy whose files do not sit in this repo is refused. The check
compares real paths. It does not trust a caller-supplied label.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from guardian.errors import GuardianError


def worktree_root() -> Path:
    """Directory that contains this checkout's pyproject and src/guardian."""
    import guardian

    return Path(guardian.__file__).resolve().parents[2]


def runtime_report() -> dict[str, str]:
    """Paths and the Kinza class the process actually loaded."""
    import guardian
    import guardian.protocols.kinza as kinza_module
    from guardian.protocols.kinza import Kinza
    from guardian.protocols.registry import PLUGIN_REGISTRY

    root = worktree_root()
    loaded = PLUGIN_REGISTRY.get("kinza")
    return {
        "binding": "worktree",
        "executable": sys.executable,
        "guardian_file": str(Path(guardian.__file__).resolve()),
        "kinza_class": f"{Kinza.__module__}.{Kinza.__qualname__}",
        "kinza_file": str(Path(kinza_module.__file__).resolve()),
        "kinza_registry": f"{loaded.__module__}.{loaded.__qualname__}" if isinstance(loaded, type) else "",
        "worktree": str(root),
    }


def assert_worktree(repo: Path | None = None) -> dict[str, str]:
    """Refuse a guardian package that is not this checkout.

    ``repo`` is only for a negative check. Production uses the loaded files.
    """
    import guardian
    import guardian.protocols.kinza as kinza_module
    from guardian.protocols.kinza import Kinza
    from guardian.protocols.registry import PLUGIN_REGISTRY

    root = worktree_root() if repo is None else Path(repo).resolve()
    guardian_file = Path(guardian.__file__).resolve()
    kinza_file = Path(kinza_module.__file__).resolve()
    expected_init = (root / "src" / "guardian" / "__init__.py").resolve()
    expected_kinza = (root / "src" / "guardian" / "protocols" / "kinza.py").resolve()
    same_init = os.path.normcase(str(guardian_file)) == os.path.normcase(str(expected_init))
    same_kinza = os.path.normcase(str(kinza_file)) == os.path.normcase(str(expected_kinza))
    registered = PLUGIN_REGISTRY.get("kinza")
    if not (root / "pyproject.toml").is_file() or not same_init or not same_kinza or registered is not Kinza:
        raise GuardianError("runtime binding does not match this worktree")
    return runtime_report()
