"""Remember a plan refusal so the next round does not repeat it.

The file stores source id, network, and the reason. It does not store keys,
addresses, or response bodies. A side map records when each refusal expires.
Old files that only have the reason stay valid.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

_EXPIRES = "_expires"
_DEFAULT_TTL = 86400.0


class PlanFailureCache:
    def __init__(
        self,
        path: Path,
        *,
        clock: Callable[[], float] | None = None,
        ttl: float = _DEFAULT_TTL,
    ) -> None:
        self.path = Path(path)
        self._clock = clock or time.time
        self._ttl = ttl
        self._rows: dict[str, str] = {}
        self._expires: dict[str, float] = {}
        if self.path.is_file():
            try:
                loaded = json.loads(self.path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                loaded = {}
            if isinstance(loaded, dict):
                expires = loaded.get(_EXPIRES)
                if isinstance(expires, dict):
                    for key, value in expires.items():
                        if isinstance(key, str) and isinstance(value, (int, float)) and not isinstance(value, bool):
                            self._expires[key] = float(value)
                for key, value in loaded.items():
                    if key == _EXPIRES:
                        continue
                    if isinstance(key, str) and isinstance(value, str) and value:
                        self._rows[key] = value

    def reason(self, source_id: str, network: str) -> str | None:
        key = f"{source_id}|{network}"
        found = self._rows.get(key)
        if not found:
            return None
        expires = self._expires.get(key)
        if expires is not None and self._clock() >= expires:
            return None
        return found

    def remember(self, source_id: str, network: str, reason: str) -> None:
        if not reason:
            return
        key = f"{source_id}|{network}"
        self._rows[key] = reason
        self._expires[key] = self._clock() + self._ttl
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {**self._rows, _EXPIRES: self._expires}
        text = json.dumps(payload, indent=2, sort_keys=True) + "\n"
        self.path.write_text(text, encoding="utf-8")


def cache_beside(database_file: Path, *, clock: Callable[[], float] | None = None) -> PlanFailureCache:
    return PlanFailureCache(Path(database_file).parent / "capability-failures.json", clock=clock)


def database_file(database: Any) -> Path | None:
    try:
        row = database.conn.execute("PRAGMA database_list").fetchone()
    except Exception:
        return None
    if row is None:
        return None
    file = row["file"] if hasattr(row, "keys") else row[2]
    if not isinstance(file, str) or not file:
        return None
    return Path(file)
