"""Mode A WP-2 route contract.

The production view origin is loaded from the policy file. This test does not
hardcode it.

AC5 allowlist (files that may contain that origin):
- config/deployments/private-task-origins.json and the packaged copy under
  src/guardian/_resources: the versioned policy is the source of the origin.
  resource_path loads the packaged copy, so the two files stay byte-identical.
- action-client/wrangler.jsonc: Pages config cannot import the policy. AC1
  compares AUTH_URI and AUTH_DOMAIN to the policy.
- docs/ including docs/reviews: operator-facing addresses, not the issuer.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from urllib.parse import urlparse

import pytest

from guardian.actions.workbench import (
    build_lista_task,
    evidence_status,
    issue_private_task_link,
    issue_task_link,
)

ROOT = Path(__file__).resolve().parents[2]
CANON = "^ref_[a-f0-9]{32}$"
TOKEN = "ref_[a-f0-9]{32}"
REF = "ref_" + "ab" * 16
POLICY_PATH = ROOT / "config/deployments/private-task-origins.json"
PACKAGED_PATH = ROOT / "src/guardian/_resources/config/deployments/private-task-origins.json"
ALLOW = {
    "config/deployments/private-task-origins.json",
    "src/guardian/_resources/config/deployments/private-task-origins.json",
    "action-client/wrangler.jsonc",
}
SKIP_DIRS = {
    "node_modules",
    "dist",
    ".git",
    "__pycache__",
    ".pytest_cache",
    ".venv",
    "coverage",
    ".mypy_cache",
}
SENDER = "0x1111111111111111111111111111111111111111"


def _read_jsonc(path: Path) -> dict:
    lines = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.lstrip().startswith("//"):
            continue
        lines.append(line)
    return json.loads("\n".join(lines))


def _tokens(source: str) -> list[str]:
    return re.findall(r"ref_\[[^\]]+\](?:\{[0-9,]+\})?", source)


def _sources() -> dict[str, str]:
    names = (
        "src/guardian/actions/workbench.py",
        "action-client/src/server/tasks.mjs",
        "action-client/src/server/handle.mjs",
        "action-client/src/private-task.mjs",
    )
    return {name: (ROOT / name).read_text(encoding="utf-8") for name in names}


def _task_get_route(handle: str) -> re.Pattern[str]:
    line = next(item for item in handle.splitlines() if 'method: "GET"' in item and "tasks" in item)
    marker = "pattern: /"
    start = line.index(marker) + len(marker)
    literal = line[start : line.index("/,", start)]
    return re.compile(literal.replace(r"\/", "/"))


def _path_route(private_task: str) -> re.Pattern[str]:
    match = re.search(r"const match = /(\^\\/t\\/\(ref_\[a-f0-9\]\{32\}\)\\/\?\$)/", private_task)
    assert match is not None
    return re.compile(match.group(1).replace(r"\/", "/"))


def _plan() -> dict:
    return {
        "sender": SENDER,
        "recipient": SENDER,
        "intent": {"idx": 0, "uuid": "1", "type": "lista_claim_withdraw"},
        "deployment": {"implementation": "0xF9a3702659d8bFDb2ff5E3c3f264E479801F63Bf"},
        "asset_out": {"raw_amount": "10"},
        "calls": [{"to": "0x1adB950d8bB3dA4bE104211D5AB038628e477fE6", "data": "0xb13acedd", "value": "0"}],
    }


def _origin_hits(needles: list[str]) -> list[str]:
    hits: list[str] = []
    for path in ROOT.rglob("*"):
        if not path.is_file():
            continue
        if any(part in SKIP_DIRS for part in path.relative_to(ROOT).parts):
            continue
        if path.stat().st_size > 2_000_000:
            continue
        rel = path.relative_to(ROOT).as_posix()
        if rel in ALLOW or rel.startswith("docs/"):
            continue
        data = path.read_bytes()
        if b"\0" in data:
            continue
        text = data.decode("utf-8", errors="ignore")
        if any(needle in text for needle in needles):
            hits.append(rel)
    return hits


def test_ref_syntax_matches_across_issuer_and_client():
    assert POLICY_PATH.read_bytes() == PACKAGED_PATH.read_bytes()
    sources = _sources()
    for name, source in sources.items():
        found = _tokens(source)
        assert found, name
        assert set(found) == {TOKEN}, name
    assert CANON in sources["src/guardian/actions/workbench.py"]
    assert CANON in sources["action-client/src/server/tasks.mjs"]
    assert CANON in sources["action-client/src/private-task.mjs"]
    assert re.search(r"\^\\/api\\/tasks\\/\(ref_\[a-f0-9\]\{32\}\)\$", sources["action-client/src/server/handle.mjs"])


def test_ac1_production_entries_share_wrangler_and_the_view_route():
    policy = json.loads(POLICY_PATH.read_text(encoding="utf-8"))
    wrangler = _read_jsonc(ROOT / "action-client/wrangler.jsonc")
    handle = (ROOT / "action-client/src/server/handle.mjs").read_text(encoding="utf-8")
    private_task = (ROOT / "action-client/src/private-task.mjs").read_text(encoding="utf-8")
    route = _task_get_route(handle)
    path_route = _path_route(private_task)
    redirects = (ROOT / "action-client/public/_redirects").read_text(encoding="utf-8")
    routes = json.loads((ROOT / "action-client/public/_routes.json").read_text(encoding="utf-8"))
    assert "/t/*" in redirects
    assert "/api/*" in routes["include"]
    assert policy["deployments"]
    for entry in policy["deployments"]:
        auth = urlparse(wrangler["vars"]["AUTH_URI"])
        assert f"{auth.scheme}://{auth.netloc}" == entry["origin"]
        assert wrangler["vars"]["AUTH_DOMAIN"] == auth.hostname
        assert wrangler["vars"]["AUTH_DOMAIN"] == urlparse(entry["origin"]).hostname
        link = issue_private_task_link(REF, entry["deployment_id"])
        expected = entry["origin"] + entry["task_path"].replace("{ref}", REF)
        assert link["issued"] is True
        assert link["url"] == expected
        assert link["execution_authorized"] is False
        parsed = path_route.fullmatch(urlparse(link["url"]).path)
        assert parsed is not None
        assert parsed.group(1) == REF
        concrete = entry["data_path"].replace("{ref}", REF)
        assert route.fullmatch(concrete)
        assert route.fullmatch(entry["data_path"].replace("{ref}", "ref_" + "AB" * 16)) is None
        assert route.fullmatch(entry["data_path"].replace("{ref}", "ref_" + "a" * 31)) is None
        assert route.fullmatch(concrete + "/x") is None
    assert evidence_status()["fixed_https_origin"] is False


def test_ac2_caller_origin_does_not_issue():
    policy = json.loads(POLICY_PATH.read_text(encoding="utf-8"))
    entry = policy["deployments"][0]
    arbitrary = issue_task_link(REF, "https://wallet.example")
    assert arbitrary["issued"] is not True
    assert arbitrary["url"] is None
    assert arbitrary["execution_authorized"] is False
    echoed = issue_task_link(REF, entry["origin"])
    assert echoed["issued"] is not True
    assert echoed["url"] is None
    blocked = issue_task_link(REF, None)
    local = issue_task_link(REF, "http://127.0.0.1:43127")
    assert blocked["issued"] is False
    assert local["issued"] is False
    assert local["url"] is None
    with pytest.raises(TypeError):
        build_lista_task(_plan(), ref=REF, origin="https://evil.example")
    task = build_lista_task(_plan(), ref=REF)
    assert "evil.example" not in json.dumps(task["link"])
    assert task["link"]["issued"] is True
    assert task["link"]["url"] == entry["origin"] + entry["task_path"].replace("{ref}", REF)
    assert task["link"]["execution_authorized"] is False
    assert task["evidence"]["fixed_https_origin"] is False
    unknown = issue_private_task_link(REF, "https://evil.example")
    assert unknown["issued"] is False
    assert unknown["url"] is None


def test_ac5_code_and_tests_do_not_hardcode_the_production_view_origin():
    policy = json.loads(POLICY_PATH.read_text(encoding="utf-8"))
    needles = [entry["origin"] for entry in policy["deployments"]]
    assert needles
    assert _origin_hits(needles) == []
