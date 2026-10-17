"""Print unapproved execution candidates. Does not write config/execution."""

from __future__ import annotations

import json
import sys
from pathlib import Path

from guardian.execution.candidates import propose_execution_candidates


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    document = propose_execution_candidates(root)
    sys.stdout.write(json.dumps(document, ensure_ascii=False, indent=2) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
