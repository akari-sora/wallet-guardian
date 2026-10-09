"""Read-only egress diagnosis.

Prints one JSON report. It does not read wallet files, sign, or broadcast.
Formal qualification is BLOCKED_EGRESS until a probe shows the network honored
the pinned destination address. Preview can still be OK.

    uv run python tools/egress_doctor.py --qualification formal --egress-config PATH
    uv run guardian egress-doctor --qualification preview --egress-config PATH
"""

from __future__ import annotations

import sys
from typing import Any


def main(argv: list[str] | None = None, *, hooks: Any = None) -> int:
    from guardian.cli import main as guardian_main

    extra = list(sys.argv[1:] if argv is None else argv)
    if extra[:1] == ["egress-doctor"]:
        extra = extra[1:]
    return guardian_main(["egress-doctor", *extra], egress_hooks=hooks)


if __name__ == "__main__":
    raise SystemExit(main())
