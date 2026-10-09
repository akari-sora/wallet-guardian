"""Draft an unpublished preview manifest for a local dist directory.

The dist directory is the output of ``scripts/build-release.sh``: one wheel,
one sdist, ``constraints.txt``, and optional ``SHA256SUMS``. This tool hashes
those files, records the runtime content hash, and writes ``published: false``
and ``tag: null``. It does not upload, tag, or open a socket.

PEP 440 ``0.4.0rc1`` maps to ``v0.4.0-rc.1``. This tool does not create that
tag. A request to publish, a URL argument, or a set credential variable
refuses the run.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from pathlib import Path
from typing import Any

SCHEMA_VERSION = "guardian.release-manifest.v1"
CHANNEL = "preview"
LICENSE_SPDX = "AGPL-3.0-only"

_VERSION = re.compile(r'(?m)^version = "([^"]+)"')
_PYTHON = re.compile(r'(?m)^requires-python = "([^"]+)"')
_LICENSE = re.compile(r'(?m)^license = "([^"]+)"')
_RC = re.compile(r"^(\d+\.\d+\.\d+)rc(\d+)$")
_SHA40 = re.compile(r"^[0-9a-f]{40}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_CREDENTIALS = (
    "ALCHEMY_API_KEY",
    "ALCHEMY_TRANSFERS_URL",
    "BLOCKSCOUT_API_KEY",
    "BLOCKSCOUT_BASE_API_KEY",
    "BLOCKSCOUT_OPTIMISM_API_KEY",
    "COINGECKO_API_KEY",
    "ETHERSCAN_API_KEY",
    "GOPLUS_API_KEY",
    "HELIUS_API_KEY",
    "NODEREAL_API_KEY",
    "NODEREAL_BSC_URL",
    "TELEGRAM_BOT_TOKEN",
    "WG_PUBLISH_TOKEN",
)
_PRIVATE = frozenset({"private", "data", "evidence"})


class DraftError(Exception):
    def __init__(self, message: str, code: int = 2) -> None:
        super().__init__(message)
        self.code = code


def repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def tag_name_for_pep440(version: str) -> str | None:
    """Map ``0.4.0rc1`` to ``v0.4.0-rc.1``. Other versions return None."""
    match = _RC.fullmatch(version)
    if match is None:
        return None
    return f"v{match.group(1)}-rc.{int(match.group(2))}"


def runtime_content_hash(root: Path) -> str:
    """Same bytes as ``content_hash`` in ``tools/mcm_shadow_round.py``."""
    digest = hashlib.sha256()
    packaged_config = root / "src" / "guardian" / "_resources" / "config"
    tops = ["src/guardian"]
    if not packaged_config.is_dir():
        tops.append("config")
    for top in tops:
        base = root / top
        if not base.is_dir():
            continue
        for path in sorted(item for item in base.rglob("*") if item.is_file()):
            if "__pycache__" in path.parts or path.suffix in {".pyc", ".pyo"}:
                continue
            digest.update(f"{top}/{path.relative_to(base).as_posix()}".encode("utf-8"))
            digest.update(b"\0")
            digest.update(_lf_bytes(path.read_bytes()))
            digest.update(b"\0")
    return digest.hexdigest()


def draft_manifest(
    dist: Path,
    *,
    root: Path,
    source_sha: str,
    evidence: Path | None = None,
) -> dict[str, Any]:
    """Build the preview manifest. Does not write and does not touch the network."""
    _reject_private(dist)
    if evidence is not None:
        _reject_private(evidence)
    if not _SHA40.fullmatch(source_sha):
        raise DraftError("source sha must be 40 lowercase hex characters")
    version, requires_python, license_spdx = _project(root)
    if license_spdx != LICENSE_SPDX:
        raise DraftError("license SPDX is not AGPL-3.0-only")
    wheel, sdist, constraints = _dist_files(dist)
    _check_sums(dist, wheel, sdist, constraints)
    payload: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "version": version,
        "pep440": version,
        "git_tag_name": tag_name_for_pep440(version),
        "tag_created": False,
        "source_sha": source_sha,
        "content_hash": runtime_content_hash(root),
        "wheel": {"name": wheel.name, "sha256": _sha256(wheel)},
        "sdist": {"name": sdist.name, "sha256": _sha256(sdist)},
        "constraints_sha256": _sha256(constraints),
        "license_spdx": license_spdx,
        "requires_python": requires_python,
        "gate_evidence": _evidence(evidence),
        "channel": CHANNEL,
        "published": False,
        "tag": None,
    }
    errors = validate_manifest(payload)
    if errors:
        raise DraftError("manifest refused: " + ",".join(errors))
    return payload


def validate_manifest(payload: dict[str, Any]) -> list[str]:
    """Subset of schemas/release-manifest.schema.json. published true is an error."""
    errors: list[str] = []
    if payload.get("schema_version") != SCHEMA_VERSION:
        errors.append("schema_version")
    if not isinstance(payload.get("version"), str) or not payload["version"]:
        errors.append("version")
    if payload.get("pep440") != payload.get("version"):
        errors.append("pep440")
    if payload.get("tag_created") is not False:
        errors.append("tag_created")
    source = payload.get("source_sha")
    if source is not None and not (isinstance(source, str) and _SHA40.fullmatch(source)):
        errors.append("source_sha")
    content = payload.get("content_hash")
    if not (isinstance(content, str) and _SHA256.fullmatch(content)):
        errors.append("content_hash")
    for key in ("wheel", "sdist"):
        item = payload.get(key)
        if item is None:
            continue
        if not isinstance(item, dict):
            errors.append(key)
            continue
        name = item.get("name")
        digest = item.get("sha256")
        if not isinstance(name, str) or not name or set(item) != {"name", "sha256"}:
            errors.append(key)
        elif not (isinstance(digest, str) and _SHA256.fullmatch(digest)):
            errors.append(key)
    constraints = payload.get("constraints_sha256")
    if constraints is not None and not (isinstance(constraints, str) and _SHA256.fullmatch(constraints)):
        errors.append("constraints_sha256")
    if payload.get("license_spdx") != LICENSE_SPDX:
        errors.append("license_spdx")
    if not isinstance(payload.get("requires_python"), str) or not payload["requires_python"]:
        errors.append("requires_python")
    gate = payload.get("gate_evidence")
    if gate is not None:
        if not isinstance(gate, dict) or set(gate) != {"name", "sha256", "status"}:
            errors.append("gate_evidence")
        elif gate.get("name") != "release-evidence.json":
            errors.append("gate_evidence")
        elif not (isinstance(gate.get("sha256"), str) and _SHA256.fullmatch(gate["sha256"])):
            errors.append("gate_evidence")
        elif gate.get("status") is not None and not isinstance(gate.get("status"), str):
            errors.append("gate_evidence")
    if payload.get("channel") != CHANNEL:
        errors.append("channel")
    if payload.get("published") is not False:
        errors.append("published")
    if payload.get("tag") is not None:
        errors.append("tag")
    mapped = tag_name_for_pep440(str(payload.get("version") or ""))
    if mapped is not None and payload.get("git_tag_name") != mapped:
        errors.append("git_tag_name")
    return errors


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    reason = _refusal_reason(argv)
    if reason:
        sys.stderr.write(reason + "\n")
        return 2
    set_names = [name for name in _CREDENTIALS if os.environ.get(name)]
    if set_names:
        sys.stderr.write("refusing to draft while credentials are set: " + ",".join(set_names) + "\n")
        return 2
    parser = argparse.ArgumentParser(description="Draft an unpublished preview manifest")
    parser.add_argument("--dist", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--source-sha", required=True)
    parser.add_argument("--evidence")
    parser.add_argument("--root")
    args = parser.parse_args(argv)
    try:
        root = Path(args.root) if args.root else repo_root()
        dist = Path(args.dist)
        output = Path(args.output)
        evidence = Path(args.evidence) if args.evidence else None
        _reject_private(output)
        if _inside(output, dist):
            raise DraftError("refusing to write the manifest inside the dist directory")
        payload = draft_manifest(dist, root=root, source_sha=args.source_sha, evidence=evidence)
        if payload["published"] is not False or payload["tag"] is not None:
            raise DraftError("refusing published true")
        _write_json(output, payload)
    except DraftError as exc:
        sys.stderr.write(str(exc) + "\n")
        return exc.code
    return 0


def _refusal_reason(argv: list[str]) -> str | None:
    for arg in argv:
        lower = arg.lower()
        if lower in {"--published", "--publish", "--published=true", "--publish=true", "--tag"}:
            return "refusing published true"
        if lower.startswith("--tag="):
            return "refusing published true"
        if "published" in lower and "true" in lower:
            return "refusing published true"
        if "://" in arg:
            return "refusing network argument"
    return None


def _project(root: Path) -> tuple[str, str, str]:
    text = (root / "pyproject.toml").read_text(encoding="utf-8")
    version = _VERSION.search(text)
    requires = _PYTHON.search(text)
    license_match = _LICENSE.search(text)
    if version is None or requires is None or license_match is None:
        raise DraftError("pyproject.toml is missing version, requires-python, or license")
    return version.group(1), requires.group(1), license_match.group(1)


def _dist_files(dist: Path) -> tuple[Path, Path, Path]:
    if not dist.is_dir():
        raise DraftError("dist directory is missing")
    wheels = sorted(path for path in dist.iterdir() if path.is_file() and path.suffix == ".whl")
    sdists = sorted(path for path in dist.iterdir() if path.is_file() and path.name.endswith(".tar.gz"))
    constraints = dist / "constraints.txt"
    if len(wheels) != 1 or len(sdists) != 1 or not constraints.is_file():
        raise DraftError("dist must contain one wheel, one sdist, and constraints.txt")
    return wheels[0], sdists[0], constraints


def _check_sums(dist: Path, wheel: Path, sdist: Path, constraints: Path) -> None:
    sums = dist / "SHA256SUMS"
    if not sums.is_file():
        return
    rows: dict[str, str] = {}
    for line in sums.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        parts = line.split()
        if len(parts) != 2 or not _SHA256.fullmatch(parts[0]):
            raise DraftError("SHA256SUMS line is invalid", code=1)
        rows[parts[1]] = parts[0]
    required = (wheel.name, sdist.name, constraints.name)
    for name in required:
        if name not in rows:
            raise DraftError(f"SHA256SUMS missing {name}", code=1)
    for name, digest in rows.items():
        target = dist / name
        if not target.is_file() or _sha256(target) != digest:
            raise DraftError(f"SHA256SUMS mismatch for {name}", code=1)


def _evidence(path: Path | None) -> dict[str, Any] | None:
    if path is None:
        return None
    if path.name != "release-evidence.json":
        raise DraftError("evidence file must be named release-evidence.json")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise DraftError("evidence file is not json") from exc
    if not isinstance(payload, dict):
        raise DraftError("evidence file is not an object")
    if payload.get("published") is True:
        raise DraftError("refusing published true")
    status = payload.get("status")
    if status is not None and not isinstance(status, str):
        raise DraftError("evidence status is not a string")
    return {"name": "release-evidence.json", "sha256": _sha256(path), "status": status}


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _lf_bytes(data: bytes) -> bytes:
    return data.replace(b"\r\n", b"\n").replace(b"\r", b"\n")


def _reject_private(path: Path) -> None:
    if any(part in _PRIVATE for part in path.parts):
        raise DraftError("refusing a private, data, or evidence path")


def _inside(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
    except ValueError:
        return False
    return True


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(payload, indent=2, ensure_ascii=True) + "\n"
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(text, encoding="utf-8", newline="\n")
    temporary.replace(path)


if __name__ == "__main__":
    raise SystemExit(main())
