"""Private config and state directories. Nothing here is shipped in the wheel.

Defaults follow the XDG base directory spec. ``GUARDIAN_CONFIG_DIR`` and
``GUARDIAN_STATE_DIR`` override those locations. Explicit CLI paths are
separate and are not rewritten to these defaults.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path


class DynamicStateRoot:
    """State root read at use time so a test can replace ``ROOT`` with a path."""

    def _path(self) -> Path:
        return state_dir()

    def __truediv__(self, other: object) -> Path:
        return self._path() / other

    def __fspath__(self) -> str:
        return os.fspath(self._path())

    def __str__(self) -> str:
        return os.fspath(self._path())


def config_dir() -> Path:
    """Private config directory. Never a path inside the installed package."""
    override = os.environ.get("GUARDIAN_CONFIG_DIR", "").strip()
    if override:
        return Path(override).expanduser()
    xdg = os.environ.get("XDG_CONFIG_HOME", "").strip()
    base = Path(xdg).expanduser() if xdg else Path.home() / ".config"
    return base / "guardian"


def state_dir() -> Path:
    """Private state directory. Never a path inside the installed package."""
    override = os.environ.get("GUARDIAN_STATE_DIR", "").strip()
    if override:
        return Path(override).expanduser()
    xdg = os.environ.get("XDG_STATE_HOME", "").strip()
    base = Path(xdg).expanduser() if xdg else Path.home() / ".local" / "state"
    return base / "guardian"


def default_env_file() -> Path:
    """``.env.local`` for this process.

    An explicit XDG or ``GUARDIAN_CONFIG_DIR`` wins. A source checkout with
    no such override still reads the gitignored file next to ``pyproject.toml``
    when that file exists.
    """
    packaged = config_dir() / ".env.local"
    if os.environ.get("GUARDIAN_CONFIG_DIR", "").strip() or os.environ.get("XDG_CONFIG_HOME", "").strip():
        return packaged
    from guardian.runtime_binding import source_checkout_root

    checkout = source_checkout_root()
    if checkout is not None:
        legacy = checkout / ".env.local"
        if legacy.is_file():
            return legacy
    return packaged


def ensure_private_dir(path: Path) -> None:
    """Create a directory and, on POSIX, restrict it to the owner (0700)."""
    existed = path.exists()
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    if os.name != "posix":
        return
    if not existed:
        os.chmod(path, 0o700)
    mode = stat.S_IMODE(path.stat().st_mode)
    if mode & 0o077:
        os.chmod(path, 0o700)
        mode = stat.S_IMODE(path.stat().st_mode)
    if mode & 0o077:
        raise OSError(f"private directory is group or world accessible: {path}")


def secure_write_text(path: Path, text: str) -> None:
    """Write a file as the owner only (0600 on POSIX)."""
    ensure_private_dir(path.parent)
    data = text.encode("utf-8")
    flags = os.O_CREAT | os.O_TRUNC | os.O_WRONLY | getattr(os, "O_BINARY", 0)
    descriptor = os.open(path, flags, 0o600)
    try:
        os.write(descriptor, data)
    finally:
        os.close(descriptor)
    if os.name == "posix":
        os.chmod(path, 0o600)


def prepare_private_dirs() -> tuple[Path, Path]:
    """Create the default private config and state directories."""
    config = config_dir()
    state = state_dir()
    ensure_private_dir(config)
    ensure_private_dir(state)
    return config, state
