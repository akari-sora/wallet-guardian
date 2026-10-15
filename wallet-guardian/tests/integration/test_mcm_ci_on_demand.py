"""MCM-SHARE-01 S6. On-demand runner isolation, opt-in, and release evidence.

Offline: the YAML subset parser and tomllib read committed files, and the
evidence tool reads a synthetic gate directory. Sockets are blocked.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import re
import socket
import sys
import tomllib
from pathlib import Path

import pytest

WG = Path(__file__).resolve().parents[2]
REPO = WG.parent
CI_PATH = REPO / ".gitlab-ci.yml"
CONFIG_PATH = REPO / "ci" / "box-runner.config.example.toml"
PROFILE_PATH = REPO / "ci" / "profile" / "wg-box-ondemand.md"
DOC_PATH = WG / "docs" / "ci.md"
SCHEMA_PATH = WG / "schemas" / "release-evidence.schema.json"
BOX_JOBS = ("box-python-tests", "box-client-tests", "box-build-checks")
SHA = "0123456789abcdef0123456789abcdef01234567"
DIGEST = hashlib.sha256(b"synthetic-runtime-s6").hexdigest()
BLOB = b"synthetic-wheel-s6"
BLOB_HASH = hashlib.sha256(BLOB).hexdigest()
VERSIONS = {
    "python": "3.12.3",
    "uv": "0.8.0",
    "pytest": "9.1.1",
    "node": "22.16.0",
    "npm": "10.9.2",
}
COMMANDS = {
    "python-tests": "uv run --frozen pytest -q --junitxml=test-results/python.xml",
    "client-tests": "npm ci --ignore-scripts --no-audit --no-fund && npm run test:canonical && npm run test:validation && npm run test:gate && npm run test:api && npm run build",
    "build-checks": "uv build --wheel --sdist",
    "post-install-functional": "uv run --frozen python -c import-guardian",
    "read-only-negative": "uv run --frozen python -c assert-eth-send-forbidden",
    "public-sample-probe": "uv run --frozen python -c read-synthetic-public-sample",
}
_KEY_VALUE = re.compile(r"^([A-Za-z0-9_-]+):\s*(.*)$")
_FORBIDDEN = re.compile(
    r"docker\.sock|privileged\s*[:=]\s*true|network_mode\s*[:=]\s*['\"]?host['\"]?|--network=host|--privileged",
    re.IGNORECASE,
)


@pytest.fixture(autouse=True)
def _block_network(monkeypatch: pytest.MonkeyPatch) -> None:
    def blocked(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("network is disabled")

    monkeypatch.setattr(socket, "socket", blocked)
    monkeypatch.setattr(socket, "create_connection", blocked)
    monkeypatch.setattr(socket, "getaddrinfo", blocked)


def _tool():
    name = "mcm_share_release_evidence"
    if name in sys.modules:
        return sys.modules[name]
    path = WG / "tools" / "release_evidence.py"
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec is not None and spec.loader is not None
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _strip_comment(line: str) -> str:
    in_single = False
    in_double = False
    out: list[str] = []
    for char in line:
        if char == "'" and not in_double:
            in_single = not in_single
        elif char == '"' and not in_single:
            in_double = not in_double
        elif char == "#" and not in_single and not in_double:
            break
        out.append(char)
    return "".join(out).rstrip()


def _scalar(text: str):
    text = text.strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in {"'", '"'}:
        inner = text[1:-1]
        if text[0] == "'":
            inner = inner.replace("''", "'")
        return inner
    if text in {"true", "false"}:
        return text == "true"
    if text in {"null", "~"}:
        return None
    if re.fullmatch(r"-?\d+", text):
        return int(text)
    return text


def parse_yaml_subset(text: str) -> dict:
    """Indentation subset for this repository's GitLab CI file."""
    lines: list[tuple[int, str, int]] = []
    for lineno, raw in enumerate(text.splitlines(), 1):
        if "\t" in raw:
            raise ValueError(f"tab on line {lineno}")
        cleaned = _strip_comment(raw)
        if not cleaned.strip():
            continue
        indent = len(cleaned) - len(cleaned.lstrip(" "))
        lines.append((indent, cleaned.strip(), lineno))
    value, index = _parse_block(lines, 0, 0)
    if index != len(lines):
        raise ValueError(f"unparsed line {lines[index][2]}")
    if not isinstance(value, dict):
        raise ValueError("document is not a mapping")
    return value


def _parse_block(lines: list[tuple[int, str, int]], index: int, min_indent: int):
    if index >= len(lines):
        raise ValueError("expected a block")
    indent, content, lineno = lines[index]
    if indent < min_indent:
        raise ValueError(f"line {lineno} is out of place")
    if content.startswith("- "):
        return _parse_list(lines, index, indent)
    return _parse_map(lines, index, indent)


def _parse_map(lines: list[tuple[int, str, int]], index: int, indent: int):
    mapping: dict = {}
    while index < len(lines):
        cur_indent, content, lineno = lines[index]
        if cur_indent < indent or content.startswith("- "):
            break
        if cur_indent > indent:
            raise ValueError(f"unexpected indent on line {lineno}")
        match = _KEY_VALUE.match(content)
        if match is None:
            raise ValueError(f"expected a key on line {lineno}")
        key, rest = match.group(1), match.group(2).strip()
        index += 1
        if rest == "":
            if index < len(lines) and lines[index][0] > cur_indent:
                child, index = _parse_block(lines, index, cur_indent + 1)
                mapping[key] = child
            else:
                mapping[key] = None
        else:
            mapping[key] = _scalar(rest)
    return mapping, index


def _parse_list(lines: list[tuple[int, str, int]], index: int, indent: int):
    items: list = []
    while index < len(lines):
        cur_indent, content, lineno = lines[index]
        if cur_indent < indent:
            break
        if cur_indent > indent:
            raise ValueError(f"unexpected indent on line {lineno}")
        if not content.startswith("- "):
            break
        item_text = content[2:].strip()
        index += 1
        match = _KEY_VALUE.match(item_text)
        if item_text and match and not item_text.startswith(("'", '"')):
            key, rest = match.group(1), match.group(2).strip()
            item: dict = {}
            if rest == "":
                if index < len(lines) and lines[index][0] > cur_indent:
                    child, index = _parse_block(lines, index, cur_indent + 2)
                    item[key] = child
                else:
                    item[key] = None
            else:
                item[key] = _scalar(rest)
            extra = cur_indent + 2
            while index < len(lines) and lines[index][0] == extra and not lines[index][1].startswith("- "):
                extra_match = _KEY_VALUE.match(lines[index][1])
                if extra_match is None:
                    raise ValueError(f"expected a key on line {lines[index][2]}")
                extra_key, extra_rest = extra_match.group(1), extra_match.group(2).strip()
                index += 1
                if extra_rest == "":
                    if index < len(lines) and lines[index][0] > extra:
                        child, index = _parse_block(lines, index, extra + 1)
                        item[extra_key] = child
                    else:
                        item[extra_key] = None
                else:
                    item[extra_key] = _scalar(extra_rest)
            items.append(item)
        else:
            items.append(_scalar(item_text))
    return items, index


def _split_top(expr: str, op: str) -> list[str]:
    parts: list[str] = []
    buf: list[str] = []
    in_single = False
    in_double = False
    index = 0
    while index < len(expr):
        char = expr[index]
        if char == "'" and not in_double:
            in_single = not in_single
            buf.append(char)
            index += 1
            continue
        if char == '"' and not in_single:
            in_double = not in_double
            buf.append(char)
            index += 1
            continue
        if not in_single and not in_double and expr.startswith(op, index):
            parts.append("".join(buf))
            buf = []
            index += len(op)
            continue
        buf.append(char)
        index += 1
    parts.append("".join(buf))
    return parts


def eval_if(expr: str, env: dict[str, str]) -> bool:
    parts = _split_top(expr, "||")
    if len(parts) > 1:
        return any(eval_if(part.strip(), env) for part in parts)
    parts = _split_top(expr, "&&")
    if len(parts) > 1:
        return all(eval_if(part.strip(), env) for part in parts)
    return _eval_atom(expr.strip(), env)


def _value(token: str, env: dict[str, str]) -> str:
    token = token.strip()
    if len(token) >= 2 and token[0] == token[-1] and token[0] in {"'", '"'}:
        return token[1:-1]
    if token.startswith("$"):
        return str(env.get(token[1:], ""))
    return token


def _eval_atom(atom: str, env: dict[str, str]) -> bool:
    match = re.fullmatch(r"\$([A-Za-z0-9_]+)\s*(==|!=)\s*(.+)", atom)
    if match:
        left = str(env.get(match.group(1), ""))
        right = _value(match.group(3), env)
        return left == right if match.group(2) == "==" else left != right
    match = re.fullmatch(r"\$([A-Za-z0-9_]+)", atom)
    if match:
        return bool(env.get(match.group(1), ""))
    raise AssertionError(f"unsupported rule atom: {atom}")


def runs(rules: list, env: dict[str, str]) -> bool:
    for rule in rules:
        if "if" in rule and not eval_if(str(rule["if"]), env):
            continue
        return str(rule.get("when", "on_success")) != "never"
    return False


def _jobs(document: dict) -> dict:
    return {key: value for key, value in document.items() if isinstance(value, dict) and "stage" in value}


def _ci() -> tuple[dict, dict]:
    document = parse_yaml_subset(CI_PATH.read_text(encoding="utf-8"))
    return document, _jobs(document)


def _junit_xml(*, tests: int = 1, failures: int = 0, errors: int = 0, skipped: int = 0, hidden_skip: bool = False) -> str:
    body = []
    for index in range(tests):
        if hidden_skip and index == 0:
            inner = '<skipped message="hidden"/>'
        elif index < skipped:
            inner = "<skipped/>"
        else:
            inner = ""
        body.append(f'<testcase classname="synthetic" name="case{index}">{inner}</testcase>')
    return (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        "<testsuites>\n"
        f'  <testsuite name="synthetic" tests="{tests}" failures="{failures}" errors="{errors}" skipped="{skipped}">\n'
        f"    {''.join(body)}\n"
        "  </testsuite>\n"
        "</testsuites>\n"
    )


def _log(step: str, *, allow: str = "false", exit_code: str = "0", claimed: str | None = None, junit: str | None = None) -> str:
    lines = [
        f"command: {COMMANDS[step]}",
        f"exit_code: {exit_code}",
        f"allow_failure: {allow}",
    ]
    if claimed:
        lines.append(f"status: {claimed}")
    if junit:
        lines.append(f"junit: {junit}")
    lines.extend(["---", "synthetic log"])
    return "\n".join(lines) + "\n"


def _gate(
    root: Path,
    *,
    environment: str = "local_external",
    allow_failure: str | None = None,
    exit_code: str | None = None,
    claimed: str | None = None,
    junit_mode: str = "pass",
    drop_step: str | None = None,
    artifacts: str = "ok",
    pause: bool | None = None,
    cleanup: bool | None = None,
) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / "identity.json").write_text(
        json.dumps({"source_sha": SHA, "content_hash": DIGEST, "environment": environment}) + "\n",
        encoding="utf-8",
    )
    (root / "versions.json").write_text(json.dumps(VERSIONS) + "\n", encoding="utf-8")
    steps = root / "steps"
    steps.mkdir()
    junit_dir = root / "junit"
    junit_dir.mkdir()
    for step, junit in (("python-tests", "junit/python.xml"), ("client-tests", "junit/client.xml")):
        if drop_step == step:
            continue
        (steps / f"{step}.log").write_text(
            _log(
                step,
                allow="true" if allow_failure == step else "false",
                exit_code=exit_code or "0",
                claimed=claimed if step == "python-tests" else None,
                junit=junit,
            ),
            encoding="utf-8",
        )
    for step in ("build-checks", "post-install-functional", "read-only-negative", "public-sample-probe"):
        if drop_step == step:
            continue
        (steps / f"{step}.log").write_text(_log(step), encoding="utf-8")
    python_xml = _junit_xml()
    if junit_mode == "fail":
        python_xml = _junit_xml(failures=1)
    elif junit_mode == "skipped":
        python_xml = _junit_xml(skipped=1)
    elif junit_mode == "hidden":
        python_xml = _junit_xml(hidden_skip=True)
    elif junit_mode == "doctype":
        python_xml = '<?xml version="1.0"?>\n<!DOCTYPE synthetic>\n<testsuites></testsuites>\n'
    if junit_mode != "drop":
        (junit_dir / "python.xml").write_text(python_xml, encoding="utf-8")
    (junit_dir / "client.xml").write_text(_junit_xml(), encoding="utf-8")
    (root / "probe").mkdir()
    (root / "probe" / "provenance.json").write_text(
        json.dumps(
            {
                "status": "PASS",
                "provenance": "committed public sample fixture; synthetic; no monitored address",
                "sample_id": "synthetic-public-sample-0001",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    if artifacts != "drop":
        artifact_dir = root / "artifacts"
        artifact_dir.mkdir()
        (artifact_dir / "synthetic.whl").write_bytes(BLOB)
        recorded = "0" * 64 if artifacts == "mismatch" else BLOB_HASH
        (artifact_dir / "hashes.json").write_text(
            json.dumps([{"path": "artifacts/synthetic.whl", "sha256": recorded}]) + "\n",
            encoding="utf-8",
        )
    if pause is not None or cleanup is not None:
        (root / "runner").mkdir()
    if pause is not None:
        (root / "runner" / "pause.json").write_text(
            json.dumps(
                {
                    "paused_after_use": pause,
                    "readback": "runner paused after the gate; active jobs 0",
                    "recorded_at": "2026-10-09T06:00:00Z",
                }
            )
            + "\n",
            encoding="utf-8",
        )
    if cleanup is not None:
        (root / "runner" / "cleanup.json").write_text(
            json.dumps(
                {
                    "cleanup_confirmed": cleanup,
                    "readback": "job containers removed; cache only; no state mounts left",
                    "recorded_at": "2026-10-09T06:01:00Z",
                }
            )
            + "\n",
            encoding="utf-8",
        )
    return root


def test_mcm_share_s6_01_reuse_isolated_runner() -> None:
    """MCM-SHARE-S6-01. Box jobs use the dedicated tag and a concurrency-1 rootless profile."""
    document, jobs = _ci()
    assert set(BOX_JOBS) <= set(jobs)
    box = [jobs[name] for name in BOX_JOBS]
    for job in box:
        assert job["tags"] == ["wg-box-ondemand"]
        assert job["resource_group"] == "wg-box-ondemand"
        assert job["allow_failure"] is False
        assert job["interruptible"] is False
        assert job["stage"] == "test"
        assert "services" not in job
    assert jobs["box-python-tests"]["script"][-1] == "uv run --frozen pytest -q --junitxml=test-results/python.xml"
    assert "npm run build" in jobs["box-client-tests"]["script"]
    assert "npm run test:gate" in jobs["box-client-tests"]["script"]
    assert jobs["box-build-checks"]["script"][-1] == "uv build --wheel --sdist"
    assert "pages" not in jobs
    assert [name for name, job in jobs.items() if job.get("stage") == "deploy"] == []

    assert jobs["python-tests"]["timeout"] == "20m"
    assert jobs["python-tests"]["script"] == [
        "python -m pip install --disable-pip-version-check uv",
        "cd wallet-guardian",
        "uv sync --frozen --group dev",
        "mkdir -p test-results",
        "uv run --frozen pytest -q -rf --durations=25 --timeout=60 --junitxml=test-results/python.xml",
    ]
    assert jobs["python-tests"]["rules"] == [
        {"if": '$CI_PIPELINE_SOURCE == "push" || $CI_PIPELINE_SOURCE == "merge_request_event"'},
    ]
    assert "tags" not in jobs["python-tests"]
    assert jobs["client-tests"]["script"] == [
        "cd wallet-guardian/action-client",
        "npm ci --ignore-scripts --no-audit --no-fund",
        "npm run test:canonical",
        "npm run test:validation",
        "npm run test:gate",
        "npm run test:api",
        "npm run build",
    ]
    assert document["default"]["interruptible"] is True
    assert document["workflow"]["rules"][1]["when"] == "never"

    config_text = CONFIG_PATH.read_text(encoding="utf-8")
    config = tomllib.loads(config_text)
    assert config["concurrent"] == 1
    assert len(config["runners"]) == 1
    runner = config["runners"][0]
    assert runner["limit"] == 1
    assert runner["request_concurrency"] == 1
    assert runner["executor"] == "docker"
    assert runner["token"] == "PLACEHOLDER_RUNNER_TOKEN"
    docker = runner["docker"]
    assert docker["privileged"] is False
    assert docker["network_mode"] == "bridge"
    assert docker["volumes"] == ["/cache"]
    assert docker["cpus"] == "2"
    assert docker["memory"] == "4g"
    assert docker["memory_swap"] == "4g"
    assert docker["oom_kill_disable"] is False
    assert docker["cap_drop"] == ["ALL"]
    assert "PODMAN" in docker["host"].upper()
    assert docker["host"].startswith("unix://PLACEHOLDER_")

    ci_text = CI_PATH.read_text(encoding="utf-8")
    assert _FORBIDDEN.search(ci_text) is None
    assert _FORBIDDEN.search(config_text) is None
    for snippet in ("wallet-guardian/private", "wallet-guardian/data", "wallet-guardian/evidence", "/var/run/"):
        assert snippet not in ci_text
        assert snippet not in config_text
    profile = PROFILE_PATH.read_text(encoding="utf-8")
    assert "rootless" in profile.lower()
    assert "wg-box-ondemand" in profile
    assert "concurrent = 1" in profile


def test_mcm_share_s6_02_fixed_sha_trigger() -> None:
    """MCM-SHARE-S6-02. Box jobs run only on explicit opt-in and record CI_COMMIT_SHA."""
    _document, jobs = _ci()
    rules = jobs["box-python-tests"]["rules"]
    assert jobs["box-client-tests"]["rules"] == rules
    assert jobs["box-build-checks"]["rules"] == rules
    assert "CI_MERGE_REQUEST_SOURCE_PROJECT_ID" in rules[0]["if"]
    assert "!=" in rules[0]["if"]
    assert rules[0]["when"] == "never"
    assert rules[1] == {"if": '$CI_PIPELINE_SOURCE == "merge_request_event"', "when": "never"}

    web = {"CI_PIPELINE_SOURCE": "web", "WG_BOX_CI": "1", "CI_COMMIT_REF_PROTECTED": "false"}
    api = {"CI_PIPELINE_SOURCE": "api", "WG_BOX_CI": "1"}
    trigger = {"CI_PIPELINE_SOURCE": "trigger", "WG_BOX_CI": "1"}
    protected = {"CI_PIPELINE_SOURCE": "push", "WG_BOX_CI": "1", "CI_COMMIT_REF_PROTECTED": "true"}
    assert runs(rules, web) is True
    assert runs(rules, api) is True
    assert runs(rules, trigger) is True
    assert runs(rules, protected) is True

    fork = {
        "CI_PIPELINE_SOURCE": "merge_request_event",
        "CI_MERGE_REQUEST_SOURCE_PROJECT_ID": "200",
        "CI_MERGE_REQUEST_PROJECT_ID": "100",
        "WG_BOX_CI": "1",
        "CI_COMMIT_REF_PROTECTED": "true",
    }
    same_repo_mr = {
        "CI_PIPELINE_SOURCE": "merge_request_event",
        "CI_MERGE_REQUEST_SOURCE_PROJECT_ID": "100",
        "CI_MERGE_REQUEST_PROJECT_ID": "100",
        "WG_BOX_CI": "1",
        "CI_COMMIT_REF_PROTECTED": "true",
    }
    plain_push = {"CI_PIPELINE_SOURCE": "push", "WG_BOX_CI": "1", "CI_COMMIT_REF_PROTECTED": "false"}
    web_without_variable = {"CI_PIPELINE_SOURCE": "web", "WG_BOX_CI": "0"}
    assert runs(rules, fork) is False
    assert runs(rules, same_repo_mr) is False
    assert runs(rules, plain_push) is False
    assert runs(rules, web_without_variable) is False

    for name in BOX_JOBS:
        script = "\n".join(jobs[name]["script"])
        assert 'test -n "$CI_COMMIT_SHA"' in script
        assert "CI_COMMIT_SHA" in script
        assert "gate-run/CI_COMMIT_SHA" in script

    shared = jobs["python-tests"]["rules"]
    assert runs(shared, {"CI_PIPELINE_SOURCE": "push"}) is True
    assert runs(shared, {"CI_PIPELINE_SOURCE": "merge_request_event"}) is True
    assert "pages" not in jobs


def test_mcm_share_s6_03_real_artifact_gate(tmp_path: Path) -> None:
    """MCM-SHARE-S6-03. PASS needs every required step at exit 0 and real JUnit."""
    tool = _tool()
    source = (WG / "tools" / "release_evidence.py").read_text(encoding="utf-8")
    assert "httpx" not in source
    assert "urllib.request" not in source

    (tmp_path / "empty").mkdir()
    empty = tool.collect_release_evidence(tmp_path / "empty")
    assert empty["status"] == "NOT_RUN"
    assert "step_missing:python-tests" in empty["status_reasons"]
    assert empty["status"] != "PASS"

    gate = _gate(tmp_path / "pass")
    payload = tool.collect_release_evidence(gate)
    assert payload["status"] == "PASS"
    assert payload["status_reasons"] == []
    assert [row["id"] for row in payload["commands"]] == list(tool.REQUIRED_STEPS)
    for row in payload["commands"]:
        assert row["exit_code"] == 0
        assert row["allow_failure"] is False
        assert row["status"] == "PASS"
        assert row["command"] == COMMANDS[row["id"]]
    junit = {row["step"]: row for row in payload["junit"]}
    assert junit["python-tests"]["present"] is True
    assert junit["python-tests"]["tests"] >= 1
    assert junit["python-tests"]["failures"] == 0
    assert junit["python-tests"]["skipped"] == 0
    assert junit["client-tests"]["present"] is True
    assert tool.validate_release_evidence(payload) == []
    output = tmp_path / "release-evidence.json"
    assert tool.main(["--gate-dir", str(gate), "--output", str(output)]) == 0
    assert json.loads(output.read_text(encoding="utf-8"))["status"] == "PASS"
    assert tool.main(["--gate-dir", str(tmp_path / "missing-dir"), "--output", str(tmp_path / "nope.json")]) == 2

    missing_junit = _gate(tmp_path / "no-junit", junit_mode="drop")
    missed = tool.collect_release_evidence(missing_junit)
    assert missed["status"] == "NOT_RUN"
    assert "junit_missing:python-tests" in missed["status_reasons"]
    assert tool.main(["--gate-dir", str(missing_junit), "--output", str(tmp_path / "missed.json")]) == 1

    skipped = tool.collect_release_evidence(_gate(tmp_path / "skip", claimed="SKIP"))
    assert skipped["status"] == "SKIP"
    assert "step_skip:python-tests" in skipped["status_reasons"]

    hidden = tool.collect_release_evidence(_gate(tmp_path / "hidden", junit_mode="hidden"))
    assert hidden["status"] == "SKIP"
    assert "junit_skipped:python-tests" in hidden["status_reasons"]

    allowed = tool.collect_release_evidence(_gate(tmp_path / "allow", allow_failure="python-tests"))
    assert allowed["status"] == "BLOCKED"
    assert "allow_failure:python-tests" in allowed["status_reasons"]

    failed = tool.collect_release_evidence(_gate(tmp_path / "exit", exit_code="1"))
    assert failed["status"] == "FAIL"
    assert "exit_code:python-tests:1" in failed["status_reasons"]

    broken = tool.collect_release_evidence(_gate(tmp_path / "fail-xml", junit_mode="fail"))
    assert broken["status"] == "FAIL"
    assert "junit_failed:python-tests" in broken["status_reasons"]

    doctype = tool.collect_release_evidence(_gate(tmp_path / "doctype", junit_mode="doctype"))
    assert doctype["status"] == "BLOCKED"
    assert "junit_doctype:python-tests" in doctype["status_reasons"]

    absent = tool.collect_release_evidence(_gate(tmp_path / "absent", drop_step="client-tests"))
    assert absent["status"] == "NOT_RUN"
    assert "step_missing:client-tests" in absent["status_reasons"]

    mismatch = tool.collect_release_evidence(_gate(tmp_path / "mismatch", artifacts="mismatch"))
    assert mismatch["status"] == "FAIL"
    assert "artifact_hash_mismatch" in mismatch["status_reasons"]
    assert mismatch["artifact_hashes"][0]["sha256"] == BLOB_HASH

    no_artifacts = tool.collect_release_evidence(_gate(tmp_path / "no-artifacts", artifacts="drop"))
    assert no_artifacts["status"] == "BLOCKED"
    assert "artifact_hashes_missing" in no_artifacts["status_reasons"]


def test_mcm_share_s6_04_provenance(tmp_path: Path) -> None:
    """MCM-SHARE-S6-04. Evidence carries SHA, content hash, versions, commands, and the local label."""
    tool = _tool()
    payload = tool.collect_release_evidence(_gate(tmp_path / "local"))
    assert payload["status"] == "PASS"
    assert payload["source_sha"] == SHA
    assert payload["content_hash"] == DIGEST
    assert len(payload["content_hash"]) == 64
    assert payload["tool_versions"] == VERSIONS
    assert [row["command"] for row in payload["commands"]] == [COMMANDS[step] for step in tool.REQUIRED_STEPS]
    assert payload["artifact_hashes"] == [{"path": "artifacts/synthetic.whl", "sha256": BLOB_HASH}]
    assert payload["artifact_hashes"][0]["sha256"] == hashlib.sha256(BLOB).hexdigest()
    assert payload["environment"] == "local_external"
    assert payload["execution_label"] == "local_external"
    assert payload["external_execution"] is True
    assert payload["public_sample_probe"]["sample_id"] == "synthetic-public-sample-0001"
    assert payload["public_sample_probe"]["status"] == "PASS"
    assert payload["runner_pause_readback"]["status"] == "NOT_RUN"
    assert payload["runner_pause_readback"]["reason"] == "not_required_for_local_external"
    assert payload["runner_cleanup_readback"]["status"] == "NOT_RUN"
    assert payload["schema_version"] == "guardian.release-evidence.v1"


def test_mcm_share_s6_05_pause_cleanup_evidence(tmp_path: Path) -> None:
    """MCM-SHARE-S6-05. GitLab runner evidence must include pause and cleanup readback."""
    doc = DOC_PATH.read_text(encoding="utf-8")
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    for field in (
        "runner_pause_readback",
        "runner_cleanup_readback",
        "paused_after_use",
        "cleanup_confirmed",
    ):
        assert field in doc
        assert field in json.dumps(schema)
    assert "paused_after_use" in schema["properties"]["runner_pause_readback"]["required"]
    assert "cleanup_confirmed" in schema["properties"]["runner_cleanup_readback"]["required"]
    assert "workflow_dispatch" in doc
    assert "local_external" in doc
    assert not list(REPO.glob(".github/workflows/*"))

    tool = _tool()
    label = "gitlab_runner:wg-box-ondemand"
    refused = tool.collect_release_evidence(_gate(tmp_path / "refused", environment=label))
    assert refused["status"] == "BLOCKED"
    assert refused["status"] != "PASS"
    assert "runner_pause_readback_missing" in refused["status_reasons"]
    assert "runner_cleanup_readback_missing" in refused["status_reasons"]
    assert refused["execution_label"] == label

    rejected = tool.collect_release_evidence(
        _gate(tmp_path / "rejected", environment=label, pause=False, cleanup=True)
    )
    assert rejected["status"] == "FAIL"
    assert "runner_pause_readback_rejected" in rejected["status_reasons"]

    accepted = tool.collect_release_evidence(
        _gate(tmp_path / "accepted", environment=label, pause=True, cleanup=True)
    )
    assert accepted["status"] == "PASS"
    assert accepted["status_reasons"] == []
    assert accepted["execution_label"] == label
    assert accepted["external_execution"] is False
    assert accepted["runner_pause_readback"]["paused_after_use"] is True
    assert accepted["runner_pause_readback"]["status"] == "PASS"
    assert accepted["runner_cleanup_readback"]["cleanup_confirmed"] is True
    assert accepted["runner_cleanup_readback"]["readback"]
    assert tool.validate_release_evidence(accepted) == []
