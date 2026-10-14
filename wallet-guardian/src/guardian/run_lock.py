"""One run-once at a time for a state file.

A second overlapping run is refused. It does not move a cursor and it does not
write alerts. The lock is a file beside the sqlite database, not a network port.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from guardian.errors import ConfigError
from guardian.sources.plan_cache import database_file


@contextmanager
def run_once_guard(database: Any) -> Iterator[None]:
    """Hold an exclusive non-blocking lock for the database file, if it has one."""
    path = database_file(database)
    if path is None:
        yield
        return
    lock_path = path.with_name(path.name + ".run-once.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        _lock(descriptor)
    except OSError as exc:
        os.close(descriptor)
        raise ConfigError("run_once_locked") from exc
    try:
        yield
    finally:
        _unlock(descriptor)
        os.close(descriptor)


def _lock(descriptor: int) -> None:
    if os.name == "nt":
        import msvcrt

        if os.lseek(descriptor, 0, os.SEEK_END) < 1:
            os.write(descriptor, b"\0")
        os.lseek(descriptor, 0, os.SEEK_SET)
        msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
        return
    import fcntl

    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)


def _unlock(descriptor: int) -> None:
    if os.name == "nt":
        import msvcrt

        os.lseek(descriptor, 0, os.SEEK_SET)
        msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
        return
    import fcntl

    fcntl.flock(descriptor, fcntl.LOCK_UN)
