"""Bind the running interpreter to the checkout or the pinned install.

A source checkout must load ``src/guardian`` from that checkout. An installed
wheel must match the pin shipped in the package. A different copy is refused.
The check compares real paths and the pin. It does not trust a caller-supplied label.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from guardian.errors import GuardianError


def _ancestors(start: Path):
    current = start.resolve()
    if current.is_file():
        current = current.parent
    while True:
        yield current
        parent = current.parent
        if parent == current:
            return
        current = parent


def source_checkout_root() -> Path | None:
    """Checkout root when this process loaded ``src/guardian`` from that checkout."""
    import guardian

    loaded = Path(guardian.__file__).resolve()
    for directory in _ancestors(loaded):
        expected = directory / "src" / "guardian" / "__init__.py"
        if not (directory / "pyproject.toml").is_file() or not expected.is_file():
            continue
        if os.path.normcase(str(expected.resolve())) == os.path.normcase(str(loaded)):
            return directory
        return None
    return None


def worktree_root() -> Path:
    """Directory that contains this checkout's pyproject and src/guardian."""
    root = source_checkout_root()
    if root is None:
        raise GuardianError("runtime binding does not match this worktree")
    return root


def _identity() -> tuple[Path, Path, type, type | None]:
    import guardian
    import guardian.protocols.kinza as kinza_module
    from guardian.protocols.kinza import Kinza
    from guardian.protocols.registry import PLUGIN_REGISTRY

    registered = PLUGIN_REGISTRY.get("kinza")
    loaded = registered if isinstance(registered, type) else None
    return (
        Path(guardian.__file__).resolve(),
        Path(kinza_module.__file__).resolve(),
        Kinza,
        loaded,
    )


def _report(binding: str, root: Path, guardian_file: Path, kinza_file: Path, kinza: type, registered: type | None) -> dict[str, str]:
    return {
        "binding": binding,
        "executable": sys.executable,
        "guardian_file": str(guardian_file),
        "kinza_class": f"{kinza.__module__}.{kinza.__qualname__}",
        "kinza_file": str(kinza_file),
        "kinza_registry": f"{registered.__module__}.{registered.__qualname__}" if registered is not None else "",
        "worktree": str(root),
    }


def runtime_report() -> dict[str, str]:
    """Paths and the Kinza class the process actually loaded."""
    checkout = source_checkout_root()
    guardian_file, kinza_file, kinza, registered = _identity()
    if checkout is not None:
        return _report("worktree", checkout, guardian_file, kinza_file, kinza, registered)
    if _installed_pin_ok():
        return _report("installed", guardian_file.parent, guardian_file, kinza_file, kinza, registered)
    raise GuardianError("runtime binding does not match the pinned manifest")


def assert_worktree(repo: Path | None = None) -> dict[str, str]:
    """Refuse a guardian package that is not this checkout or the pinned install.

    ``repo`` is only for a negative check against one checkout. Production uses
    the loaded files: a source tree must match that tree, and an install must
    match the pin inside the package.
    """
    if repo is not None:
        return _assert_checkout(Path(repo).resolve())
    checkout = source_checkout_root()
    if checkout is not None:
        return _assert_checkout(checkout)
    if _installed_pin_ok():
        return runtime_report()
    raise GuardianError("runtime binding does not match the pinned manifest")


def _assert_checkout(root: Path) -> dict[str, str]:
    guardian_file, kinza_file, kinza, registered = _identity()
    expected_init = (root / "src" / "guardian" / "__init__.py").resolve()
    expected_kinza = (root / "src" / "guardian" / "protocols" / "kinza.py").resolve()
    same_init = os.path.normcase(str(guardian_file)) == os.path.normcase(str(expected_init))
    same_kinza = os.path.normcase(str(kinza_file)) == os.path.normcase(str(expected_kinza))
    if not (root / "pyproject.toml").is_file() or not same_init or not same_kinza or registered is not kinza:
        raise GuardianError("runtime binding does not match this worktree")
    return _report("worktree", root, guardian_file, kinza_file, kinza, registered)


def _installed_pin_ok() -> bool:
    """True when the loaded files are the installed distribution named by the pin."""
    import guardian
    from importlib.metadata import PackageNotFoundError, distribution

    from guardian.resources import packaged_root, read_resource_json

    try:
        pin = read_resource_json("runtime-pin.json")
    except (OSError, RuntimeError, ValueError):
        return False
    if not isinstance(pin, dict):
        return False
    if pin.get("schema_version") != "guardian.runtime-pin.v1":
        return False
    if pin.get("distribution") != "guardian":
        return False
    if pin.get("version") != guardian.__version__:
        return False
    try:
        dist = distribution("guardian")
    except PackageNotFoundError:
        return False
    guardian_file, kinza_file, kinza, registered = _identity()
    installed_init = Path(str(dist.locate_file("guardian/__init__.py"))).resolve()
    if os.path.normcase(str(installed_init)) != os.path.normcase(str(guardian_file)):
        return False
    package = guardian_file.parent
    try:
        kinza_file.relative_to(package)
        packaged_root().resolve().relative_to(package)
    except ValueError:
        return False
    return registered is kinza
