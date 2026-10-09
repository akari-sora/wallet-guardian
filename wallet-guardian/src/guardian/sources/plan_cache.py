"""Remember a plan refusal so the next round does not repeat it.

The file stores source id, network, and the reason. It does not store keys,
addresses, or response bodies.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


class PlanFailureCache:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._rows: dict[str, str] = {}
        if self.path.is_file():
            try:
                loaded = json.loads(self.path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                loaded = {}
            if isinstance(loaded, dict):
                for key, value in loaded.items():
                    if isinstance(key, str) and isinstance(value, str) and value:
                        self._rows[key] = value

    def reason(self, source_id: str, network: str) -> str | None:
        found = self._rows.get(f"{source_id}|{network}")
        return found or None

    def remember(self, source_id: str, network: str, reason: str) -> None:
        if not reason:
            return
        self._rows[f"{source_id}|{network}"] = reason
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(self._rows, indent=2, sort_keys=True) + "\n"
        self.path.write_text(payload, encoding="utf-8")


def cache_beside(database_file: Path) -> PlanFailureCache:
    return PlanFailureCache(Path(database_file).parent / "capability-failures.json")


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
