"""MCM-SHARE-01 S8. Preview docs, unpublished manifest draft, version agreement.

Offline: the tests read committed files and a temporary dist directory. Sockets
are blocked. Credential variables are cleared before each test.
"""

from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import re
import socket
import subprocess
import sys
from contextlib import redirect_stdout
from pathlib import Path

import pytest

from guardian.resources import resource_path

WG = Path(__file__).resolve().parents[2]
REPO = WG.parent
MATRIX = WG / "docs" / "coverage-matrix.md"
CHANGELOG = WG / "CHANGELOG.md"
PREVIEW = WG / "docs" / "release-preview.md"
API_KEYS = WG / "docs" / "api-keys.md"
DRAFT = WG / "release" / "manifest.draft.json"
SCHEMA = WG / "schemas" / "release-manifest.schema.json"
ALLOWED = frozenset({
    "ok",
    "partial",
    "blocked",
    "credential_missing",
    "expansion_pending",
    "not_verified",
})
SHA = "0123456789abcdef0123456789abcdef01234567"
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
_CLAIM = re.compile(
    r"production reliability|72h shadow|72-hour shadow|production-ready|"
    r"shadow (is|was) complete|formally qualified",
    re.IGNORECASE,
)
_NEGATION = re.compile(r"\b(not|no|never|unrun|without)\b|NOT_RUN|do not", re.IGNORECASE)


@pytest.fixture(autouse=True)
def _offline(monkeypatch: pytest.MonkeyPatch) -> None:
    def blocked(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("network is disabled")

    monkeypatch.setattr(socket, "socket", blocked)
    monkeypatch.setattr(socket, "create_connection", blocked)
    monkeypatch.setattr(socket, "getaddrinfo", blocked)
    for name in _CREDENTIALS:
        monkeypatch.delenv(name, raising=False)


def _load(name: str, filename: str):
    if name in sys.modules:
        return sys.modules[name]
    path = WG / "tools" / filename
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec is not None and spec.loader is not None
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _text(path: Path) -> str:
    return path.read_text(encoding="utf-8").replace("\r\n", "\n")


def _fenced(text: str) -> list[dict]:
    found = []
    for match in re.finditer(r"```json\n(.*?)\n```", text, re.DOTALL):
        found.append(json.loads(match.group(1)))
    return found


def _table(text: str, header_prefix: str) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    lines = text.splitlines()
    for index, line in enumerate(lines):
        if not line.startswith(header_prefix):
            continue
        headers = [cell.strip() for cell in line.strip().strip("|").split("|")]
        cursor = index + 2
        while cursor < len(lines) and lines[cursor].startswith("|"):
            values = [cell.strip() for cell in lines[cursor].strip().strip("|").split("|")]
            assert len(values) == len(headers)
            rows.append(dict(zip(headers, values)))
            cursor += 1
        return rows
    raise AssertionError(header_prefix)


def test_mcm_share_s8_01_matrix_statuses_honest() -> None:
    """S8-01. Statuses stay in the six, and an ok path has fixture plus live probe."""
    text = _text(MATRIX)
    blocks = {item["kind"]: item for item in _fenced(text)}
    cells_doc = blocks["mcm-share-s8-coverage-cells"]
    evidence = blocks["mcm-share-s8-coverage-evidence"]
    assert cells_doc["allowed_statuses"] == [
        "ok", "partial", "blocked", "credential_missing", "expansion_pending", "not_verified",
    ]
    by_id = {}
    referenced: set[str] = set()
    for cell in cells_doc["cells"]:
        assert cell["id"] not in by_id
        by_id[cell["id"]] = cell
        for key in ("status", "no_key_status", "keyed_status"):
            assert cell[key] in ALLOWED, cell["id"]
        if cell["status"] == "ok":
            assert cell["no_key_status"] == "ok" and cell["keyed_status"] == "ok"
        elif cell["no_key_status"] != cell["keyed_status"]:
            assert cell["status"] == "partial"
        referenced.update(cell["capability_files"])
        for rel in cell["capability_files"]:
            assert (WG / rel).is_file(), rel
    for probe in cells_doc["endpoint_probes"]:
        assert probe["status"] in ALLOWED
        assert probe["status"] != "ok"

    table = _table(text, "| network |")
    seen = set()
    for row in table:
        for key in ("no-key status", "keyed status", "status"):
            assert row[key] in ALLOWED
        match = next(
            cell for cell in cells_doc["cells"]
            if cell["network"] == row["network"]
            and cell["capability"] == row["capability"]
            and cell["no_key_path"] == row["no-key path"]
            and cell["keyed_path"] == row["keyed path"]
        )
        assert row["no-key status"] == match["no_key_status"]
        assert row["keyed status"] == match["keyed_status"]
        assert row["status"] == match["status"]
        seen.add(match["id"])
    assert seen == set(by_id)
    for row in _table(text, "| probe |"):
        assert row["status"] in ALLOWED

    on_disk = {f"config/source-capabilities/{path.name}" for path in (WG / "config" / "source-capabilities").glob("*.json")}
    assert on_disk <= referenced

    ok_paths = []
    for cell in cells_doc["cells"]:
        for side in ("no_key", "keyed"):
            if cell[f"{side}_status"] == "ok":
                ok_paths.append((cell["id"], side))
                ev = evidence["cells"][cell["id"]][side]
                assert ev["offline_fixtures"] is True
                assert ev["live_probe"] is True
                assert ev["live_probe_record"]
                for name in ev["fixture_tests"]:
                    hits = list(WG.joinpath("tests").rglob("*.py"))
                    body = "\n".join(path.read_text(encoding="utf-8") for path in hits)
                    assert f"def {name}" in body
                    start = body.index(f"def {name}")
                    assert "assert " in body[start:start + 4000]
    assert ("bsc-mainnet/history.native_external", "keyed") in ok_paths
    assert ("ethereum-mainnet/history.native_external", "keyed") in ok_paths
    for cid, sides in evidence["cells"].items():
        for side in sides:
            assert (cid, side) in ok_paths

    bsc = by_id["bsc-mainnet/history.native_external"]
    assert bsc["no_key_status"] == "credential_missing"
    assert bsc["keyed_status"] == "ok"
    assert bsc["status"] == "partial"
    assert "nodereal_transfers.json" in bsc["capability_files"][0]

    base = by_id["base-mainnet/history.native_external"]
    assert base["keyed_status"] == "not_verified"
    assert "class AlchemyClient" in (WG / "src" / "guardian" / "sources" / "alchemy.py").read_text(encoding="utf-8")

    avalanche = by_id["avalanche-c/history.native_external"]
    assert avalanche["status"] == "expansion_pending"
    assert by_id["solana-mainnet/balances.native"]["status"] == "not_verified"
    kinza = by_id["bsc-mainnet/positions.kinza"]
    assert kinza["status"] == "not_verified"
    erc = by_id["configured-evm/positions.erc4626"]
    assert erc["status"] == "not_verified"
    assert "vaults_not_configured" in erc["note"]
    assert all(cell["status"] != "ok" for cell in cells_doc["cells"])


def test_mcm_share_s8_02_matrix_key_nokey() -> None:
    """S8-02. Every keyed source names a no-key path and a real api-keys anchor."""
    text = _text(MATRIX)
    sources = next(item for item in _fenced(text) if item["kind"] == "mcm-share-s8-keyed-sources")
    keys = _text(API_KEYS)
    assert len(sources["keyed_sources"]) >= 8
    table = {row["source"]: row for row in _table(text, "| source | credential env |")}
    for item in sources["keyed_sources"]:
        assert item["credential_env"]
        assert item["no_key"].strip()
        assert item["anchor"]
        assert f'id="{item["anchor"]}"' in keys
        assert f'docs/api-keys.md#{item["anchor"]}' == table[item["id"]]["anchor"]
        assert table[item["id"]]["no-key path"] == item["no_key"]
        assert (WG / item["capability_file"]).is_file()
    nodereal = next(item for item in sources["keyed_sources"] if item["id"] == "nodereal-bsc")
    alchemy = next(item for item in sources["keyed_sources"] if item["id"] == "alchemy-base")
    assert "credential_missing" in nodereal["no_key"]
    assert "no keyless BSC external-history source" in nodereal["no_key"]
    assert "credential_missing" in alchemy["no_key"]
    ofac = next(item for item in sources["keyless_sources"] if item["id"] == "ofac")
    assert ofac["credential_env"] is None
    assert 'id="ofac"' in keys
    assert "feed_not_supplied" in ofac["no_key"]


def test_mcm_share_s8_03_manifest_draft_immutable(tmp_path: Path) -> None:
    """S8-03. The draft hashes a temp dist, stays unpublished, and refuses publish or credentials."""
    tool = _load("mcm_share_s8_release_manifest", "release_manifest.py")
    dist = tmp_path / "dist"
    dist.mkdir()
    wheel = dist / "wallet_guardian_monitor-0.4.0rc1-py3-none-any.whl"
    sdist = dist / "wallet_guardian_monitor-0.4.0rc1.tar.gz"
    constraints = dist / "constraints.txt"
    wheel.write_bytes(b"synthetic-wheel-s8")
    sdist.write_bytes(b"synthetic-sdist-s8")
    constraints.write_bytes(b"httpx==0.28.1\n")
    names = (wheel, sdist, constraints)
    sums = "".join(f"{hashlib.sha256(path.read_bytes()).hexdigest()}  {path.name}\n" for path in names)
    (dist / "SHA256SUMS").write_text(sums, encoding="utf-8", newline="\n")
    before = {path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in dist.iterdir() if path.is_file()}

    evidence = tmp_path / "release-evidence.json"
    evidence.write_text(
        json.dumps({
            "schema_version": "guardian.release-evidence.v1",
            "status": "NOT_RUN",
            "note": "synthetic-token-should-not-copy",
        }),
        encoding="utf-8",
        newline="\n",
    )
    output = tmp_path / "manifest.draft.json"
    code = tool.main([
        "--dist", str(dist),
        "--output", str(output),
        "--source-sha", SHA,
        "--evidence", str(evidence),
    ])
    assert code == 0
    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["published"] is False
    assert payload["tag"] is None
    assert payload["channel"] == "preview"
    assert payload["tag_created"] is False
    assert payload["version"] == "0.4.0rc1"
    assert payload["license_spdx"] == "AGPL-3.0-only"
    assert payload["requires_python"] == ">=3.12"
    assert payload["git_tag_name"] == "v0.4.0-rc.1"
    assert payload["source_sha"] == SHA
    assert payload["wheel"]["name"] == wheel.name
    assert payload["wheel"]["sha256"] == hashlib.sha256(wheel.read_bytes()).hexdigest()
    assert payload["sdist"]["sha256"] == hashlib.sha256(sdist.read_bytes()).hexdigest()
    assert payload["constraints_sha256"] == hashlib.sha256(constraints.read_bytes()).hexdigest()
    assert payload["gate_evidence"]["name"] == "release-evidence.json"
    assert payload["gate_evidence"]["status"] == "NOT_RUN"
    assert payload["gate_evidence"]["sha256"] == hashlib.sha256(evidence.read_bytes()).hexdigest()
    assert "synthetic-token-should-not-copy" not in output.read_text(encoding="utf-8")
    assert tool.validate_manifest(payload) == []
    after = {path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in dist.iterdir() if path.is_file()}
    assert after == before
    assert tool.runtime_content_hash(WG) == payload["content_hash"]
    shadow = _load("mcm_share_s8_shadow_round", "mcm_shadow_round.py")
    assert shadow.content_hash(WG) == payload["content_hash"]

    published = tool.main(["--dist", str(dist), "--output", str(tmp_path / "nope.json"), "--source-sha", SHA, "--published"])
    assert published == 2
    assert not (tmp_path / "nope.json").exists()
    networked = tool.main([
        "--dist", str(dist), "--output", str(tmp_path / "net.json"), "--source-sha", SHA, "https://example.invalid",
    ])
    assert networked == 2

    bad = tmp_path / "bad-dist"
    bad.mkdir()
    for path in names:
        (bad / path.name).write_bytes(path.read_bytes())
    (bad / "SHA256SUMS").write_text("0" * 64 + "  " + wheel.name + "\n", encoding="utf-8", newline="\n")
    bad_out = tmp_path / "bad.json"
    assert tool.main(["--dist", str(bad), "--output", str(bad_out), "--source-sha", SHA]) == 1
    assert not bad_out.exists()

    draft = json.loads(_text(DRAFT))
    assert draft["published"] is False
    assert draft["tag"] is None
    assert draft["version"] == "0.4.0rc1"
    assert draft["channel"] == "preview"
    # The committed draft is a template: the real hash is computed when a manifest is cut for a dist.
    # Pinning it here would break on every unrelated src/ change.
    assert re.fullmatch(r"[0-9a-f]{64}", draft["content_hash"])
    assert tool.runtime_content_hash(WG) != "" and re.fullmatch(r"[0-9a-f]{64}", tool.runtime_content_hash(WG))
    assert tool.validate_manifest(draft) == []
    packaged = resource_path("schemas", "release-manifest.schema.json").read_bytes()
    assert packaged == SCHEMA.read_bytes()
    script = _text(WG / "scripts" / "build-release.sh")
    assert "build_release.py" in script


def test_mcm_share_s8_03_manifest_refuses_credentials(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    """S8-03. A set credential name stops the draft and the value is not written."""
    tool = _load("mcm_share_s8_release_manifest", "release_manifest.py")
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "wallet_guardian_monitor-0.4.0rc1-py3-none-any.whl").write_bytes(b"synthetic-wheel-s8")
    (dist / "wallet_guardian_monitor-0.4.0rc1.tar.gz").write_bytes(b"synthetic-sdist-s8")
    (dist / "constraints.txt").write_bytes(b"httpx==0.28.1\n")
    output = tmp_path / "manifest.draft.json"
    monkeypatch.setenv("ETHERSCAN_API_KEY", "synthetic-secret-value")
    code = tool.main(["--dist", str(dist), "--output", str(output), "--source-sha", SHA])
    assert code == 2
    assert not output.exists()
    captured = capsys.readouterr()
    assert "ETHERSCAN_API_KEY" in captured.err
    assert "synthetic-secret-value" not in captured.err


def test_mcm_share_s8_04_version_consistency(tmp_path: Path) -> None:
    """S8-04. Version sources agree on 0.4.0rc1, and a copied mismatch is reported."""
    tool = _load("mcm_share_s8_check_version", "check_version_consistency.py")
    raw = json.loads(_text_from_main(tool))
    assert raw["ok"] is True
    assert raw["tag_created"] is False
    assert raw["tag_name"] == "v0.4.0-rc.1"
    assert tool.tag_name_for_pep440("0.4.0rc1") == "v0.4.0-rc.1"
    for name, value in raw["versions"].items():
        assert value == "0.4.0rc1", name
    assert tool.consistency_errors(WG) == []

    root = tmp_path / "copy"
    (root / "src" / "guardian").mkdir(parents=True)
    (root / "release").mkdir()
    for rel in ("pyproject.toml", "uv.lock", "CHANGELOG.md", "src/guardian/__init__.py", "release/manifest.draft.json"):
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes((WG / rel).read_bytes())
    changelog = root / "CHANGELOG.md"
    changelog.write_text(
        changelog.read_text(encoding="utf-8").replace("## [0.4.0rc1]", "## [0.4.0rc2]", 1),
        encoding="utf-8",
        newline="\n",
    )
    buffer = io.StringIO()
    with redirect_stdout(buffer):
        assert tool.main(["--root", str(root)]) == 1
    assert "changelog=0.4.0rc2" in buffer.getvalue()
    errors = tool.consistency_errors(root)
    assert any("changelog=0.4.0rc2" in item for item in errors)
    assert any("mismatch:" in item for item in errors)

    listed = subprocess.run(
        ["git", "tag", "--list", "v0.4.0-rc.1"],
        cwd=REPO,
        check=False,
        capture_output=True,
        text=True,
    )
    assert listed.returncode == 0
    assert listed.stdout.strip() == ""
    source = _text(WG / "tools" / "check_version_consistency.py")
    assert "git tag" not in source
    assert "subprocess" not in source


def _text_from_main(tool) -> str:
    buffer = io.StringIO()
    with redirect_stdout(buffer):
        code = tool.main([])
    assert code == 0
    return buffer.getvalue()


def test_mcm_share_s8_05_changelog_nonclaims() -> None:
    """S8-05. The changelog names the preview, the gaps, and the stable blockers."""
    changelog = _text(CHANGELOG)
    assert "## [0.4.0rc1]" in changelog
    assert "### Known limitations / not claimed" in changelog
    assert "### Release blockers for stable" in changelog
    assert "NOT_RUN" in changelog
    assert "v0.4.0-rc.1" in changelog
    preview = _text(PREVIEW)
    matrix = _text(MATRIX)
    assert "has not been run" in matrix
    assert "NOT_RUN" in preview
    assert "v0.4.0-rc.1" in preview
    claimed = []
    for path in (WG / "docs").rglob("*.md"):
        for line in _text(path).splitlines():
            if _CLAIM.search(line) and not _NEGATION.search(line):
                claimed.append(f"{path.name}: {line.strip()}")
    for line in changelog.splitlines():
        if _CLAIM.search(line) and not _NEGATION.search(line):
            claimed.append("CHANGELOG: " + line.strip())
    assert claimed == []
