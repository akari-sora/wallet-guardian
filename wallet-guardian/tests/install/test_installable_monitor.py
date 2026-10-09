"""MCM-SHARE-01 S4. The monitor installs and runs outside this checkout.

Builds use the local uv cache (``--offline``). Private files in these tests are synthetic.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import re
import shutil
import sqlite3
import stat
import subprocess
import sys
import tarfile
import tempfile
import tomllib
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
_FORBIDDEN_PARTS = frozenset({
    ".env",
    ".env.local",
    "action-client",
    "data",
    "evidence",
    "keystore",
    "private",
    "secrets",
    "tests",
})
# Credential shapes, not the detector literals shipped in source (for example ``glpat-|``).
_SECRET_PATTERNS = (
    re.compile(r"BEGIN OPENSSH PRIVATE KEY"),
    re.compile(r"BEGIN PRIVATE KEY"),
    re.compile(r"BEGIN RSA PRIVATE KEY"),
    re.compile(r"(?<![A-Za-z0-9_])glpat-[A-Za-z0-9]"),
    re.compile(r"(?<![A-Za-z0-9_])sk_live_[A-Za-z0-9]"),
)


def _outside_checkout() -> Path:
    """A directory that is not inside this git work tree."""
    from guardian.configuration import path_inside_work_tree

    path = Path(tempfile.mkdtemp(prefix="wg-s4-"))
    if path_inside_work_tree(path):
        shutil.rmtree(path, ignore_errors=True)
        raise RuntimeError("temporary directory is inside the git work tree")
    return path


def _child_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    env = os.environ.copy()
    for key in ("PYTHONPATH", "VIRTUAL_ENV", "PYTHONHOME"):
        env.pop(key, None)
    env["UV_OFFLINE"] = "1"
    if extra:
        env.update(extra)
    return env


def _run(command: list[str], *, cwd: Path, env: dict[str, str] | None = None, timeout: int = 180) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        cwd=cwd,
        env=env if env is not None else _child_env(),
        text=True,
        capture_output=True,
        timeout=timeout,
        check=False,
    )


def _venv_python(venv: Path) -> Path:
    if os.name == "nt":
        return venv / "Scripts" / "python.exe"
    return venv / "bin" / "python"


def _venv_guardian(venv: Path) -> Path:
    if os.name == "nt":
        return venv / "Scripts" / "guardian.exe"
    return venv / "bin" / "guardian"


def _wheel(dist: Path) -> Path:
    wheels = sorted(dist.glob("*.whl"))
    assert len(wheels) == 1
    return wheels[0]


def _write_private(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")
    if os.name == "posix":
        os.chmod(path.parent, 0o700)
        os.chmod(path, 0o600)


def _archive_names(path: Path) -> list[str]:
    if path.suffix == ".whl":
        with zipfile.ZipFile(path) as archive:
            return archive.namelist()
    with tarfile.open(path) as archive:
        return [name for name in archive.getnames() if name]


def _archive_text(path: Path, name: str) -> str:
    if path.suffix == ".whl":
        with zipfile.ZipFile(path) as archive:
            return archive.read(name).decode("utf-8")
    with tarfile.open(path) as archive:
        member = archive.extractfile(name)
        assert member is not None
        return member.read().decode("utf-8")


def _dep_name(item: object) -> str:
    if isinstance(item, dict):
        return str(item["name"])
    return str(item)


def _dep_extras(item: object) -> tuple[str, ...]:
    if not isinstance(item, dict):
        return ()
    extras = item.get("extra") or item.get("extras") or ()
    if isinstance(extras, str):
        return (extras,)
    return tuple(str(extra) for extra in extras)


def _runtime_pins(lock_path: Path) -> dict[str, str]:
    document = tomllib.loads(lock_path.read_text(encoding="utf-8"))
    packages = {item["name"]: item for item in document["package"]}
    seen: set[str] = set()
    seen_edges: set[tuple[str, tuple[str, ...]]] = set()
    stack = [(_dep_name(item), _dep_extras(item)) for item in packages["guardian"].get("dependencies", [])]
    while stack:
        name, extras = stack.pop()
        edge = (name, extras)
        if name == "guardian" or edge in seen_edges:
            continue
        seen_edges.add(edge)
        seen.add(name)
        package = packages[name]
        for dep in package.get("dependencies", []):
            stack.append((_dep_name(dep), _dep_extras(dep)))
        optional = package.get("optional-dependencies", {})
        for extra in extras:
            for dep in optional.get(extra, []):
                stack.append((_dep_name(dep), _dep_extras(dep)))
    return {name: packages[name]["version"] for name in seen}


def _constraint_pins(text: str) -> dict[str, str]:
    pins: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        name, sep, rest = line.partition("==")
        assert sep == "==", line
        name = name.split("[", 1)[0].strip().lower().replace("_", "-")
        version = rest.split(";", 1)[0].strip()
        assert version
        pins[name] = version
    return pins


def _installed_versions(python: Path, cwd: Path) -> dict[str, str]:
    script = (
        "import importlib.metadata, json\n"
        "rows = {}\n"
        "for dist in importlib.metadata.distributions():\n"
        "    rows[dist.metadata['Name'].lower().replace('_', '-')] = dist.version\n"
        "print(json.dumps(rows))\n"
    )
    completed = _run([str(python), "-c", script], cwd=cwd)
    assert completed.returncode == 0, completed.stderr
    return json.loads(completed.stdout)


@pytest.fixture(scope="module")
def release_dist(tmp_path_factory: pytest.TempPathFactory) -> Path:
    out = tmp_path_factory.mktemp("release")
    completed = _run(
        [sys.executable, str(ROOT / "tools" / "build_release.py"), "--out-dir", str(out)],
        cwd=ROOT,
        timeout=240,
    )
    assert completed.returncode == 0, completed.stderr + completed.stdout
    assert _wheel(out).is_file()
    assert list(out.glob("*.tar.gz"))
    assert (out / "constraints.txt").is_file()
    assert (out / "SHA256SUMS").is_file()
    return out


def test_mcm_share_s4_02_no_repo_path_lookups(tmp_path: Path) -> None:
    """MCM-SHARE-S4-02. Runtime code does not walk to the repo, and packaged bytes match."""
    offenders = []
    for path in (ROOT / "src" / "guardian").rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        if "parents[" in text or "parent.parent" in text:
            offenders.append(path.relative_to(ROOT).as_posix())
    assert offenders == []

    import guardian
    from guardian.resources import packaged_root, resource_path

    package = Path(guardian.__file__).resolve().parent
    root = packaged_root().resolve()
    root.relative_to(package)
    schema = json.loads(resource_path("schemas", "coverage.schema.json").read_text(encoding="utf-8"))
    assert schema.get("type") == "object"
    assert "$schema" in schema
    assert (root / "config" / "chains").is_dir()
    assert (root / "migrations" / "monitor").is_dir()

    packaged = ROOT / "src" / "guardian" / "_resources"
    for relative in ("config", "schemas", "migrations/monitor", "examples"):
        source = _tree_bytes(ROOT / relative)
        copy = _tree_bytes(packaged / relative)
        assert source == copy
        assert source
    plan = ROOT / "docs" / "mcm-01" / "network-tiers.plan.json"
    packaged_plan = packaged / "docs" / "mcm-01" / "network-tiers.plan.json"
    assert plan.read_bytes() == packaged_plan.read_bytes()

    tool = _shadow_tool()
    tree = tmp_path / "hash-tree"
    code = tree / "src" / "guardian" / "mod.py"
    packaged_config = tree / "src" / "guardian" / "_resources" / "config" / "sample.json"
    stray = tree / "config" / "sample.json"
    docs = tree / "docs" / "note.md"
    for path, body in (
        (code, b"alpha\n"),
        (packaged_config, b'{"ok": true}\n'),
        (stray, b'{"ignored": true}\n'),
        (docs, b"doc\n"),
    ):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(body)
    first = tool.content_hash(tree)
    assert len(first) == 64
    docs.write_bytes(b"doc changed\n")
    stray.write_bytes(b'{"ignored": false}\n')
    assert tool.content_hash(tree) == first
    packaged_config.write_bytes(b'{"ok": false}\n')
    changed = tool.content_hash(tree)
    assert changed != first
    code.write_bytes(b"alpha!\n")
    assert tool.content_hash(tree) not in {first, changed}


def test_mcm_share_s4_04_private_state_xdg_perms(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """MCM-SHARE-S4-04. Default private files follow XDG and stay out of the package."""
    from guardian.configuration import file_permission_bits, load_public_config
    from guardian.locations import config_dir, default_env_file, prepare_private_dirs, secure_write_text, state_dir
    from guardian.storage import Database

    config_home = tmp_path / "xdg-config"
    state_home = tmp_path / "xdg-state"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config_home))
    monkeypatch.setenv("XDG_STATE_HOME", str(state_home))
    monkeypatch.delenv("GUARDIAN_CONFIG_DIR", raising=False)
    monkeypatch.delenv("GUARDIAN_STATE_DIR", raising=False)

    config, state = prepare_private_dirs()
    assert config == config_home / "guardian"
    assert state == state_home / "guardian"
    assert default_env_file() == config / ".env.local"
    secure_write_text(config / "operator.env", "ETHERSCAN_API_KEY=synthetic-not-a-real-key\n")
    database_path = state / "monitor.sqlite"
    Database(database_path).close()

    import guardian

    package = Path(guardian.__file__).resolve().parent
    for path in (config, state, database_path, config / "operator.env"):
        assert not _inside(path, package)
        assert "site-packages" not in path.parts
    if os.name == "posix":
        assert file_permission_bits(config) == 0o700
        assert file_permission_bits(state) == 0o700
        assert file_permission_bits(config / "operator.env") == 0o600
        assert file_permission_bits(database_path) == 0o600
    else:
        assert stat.S_ISDIR(config.stat().st_mode)
        assert database_path.is_file()
        assert (config / "operator.env").read_text(encoding="utf-8").startswith("ETHERSCAN_API_KEY=")

    explicit = tmp_path / "explicit" / "chosen.sqlite"
    Database(explicit).close()
    assert explicit.is_file()
    assert not _inside(explicit, state)
    public = tmp_path / "explicit-monitor.json"
    public.write_text((ROOT / "examples" / "monitor.json").read_text(encoding="utf-8"), encoding="utf-8")
    loaded = load_public_config(public)
    assert loaded.path == public
    assert loaded.document["schema_version"] == "guardian.monitor.v1"


def test_mcm_share_s4_01_wheel_sdist_outside_repo(release_dist: Path) -> None:
    """MCM-SHARE-S4-01. An installed wheel runs with no checkout on sys.path."""
    work = _outside_checkout()
    try:
        venv = work / "venv"
        created = _run(["uv", "venv", "--python", sys.executable, "--offline", str(venv)], cwd=work)
        assert created.returncode == 0, created.stderr
        python = _venv_python(venv)
        wheel = _wheel(release_dist)
        installed = _run(
            [
                "uv", "pip", "install", "--offline", "--python", str(python),
                "--constraints", str(release_dist / "constraints.txt"), str(wheel),
            ],
            cwd=work,
        )
        assert installed.returncode == 0, installed.stderr
        guardian = _venv_guardian(venv)
        help_run = _run([str(guardian), "--help"], cwd=work)
        assert help_run.returncode == 0, help_run.stderr
        assert "run-once" in help_run.stdout

        accounts = work / "accounts.json"
        secret = work / "secret.json"
        state = work / "state"
        config = work / "monitor.json"
        _write_private(accounts, (ROOT / "examples" / "accounts.empty.template.json").read_text(encoding="utf-8"))
        _write_private(secret, "{}\n")
        config.write_text((ROOT / "examples" / "monitor.json").read_text(encoding="utf-8"), encoding="utf-8")
        env = _child_env({
            "MONITOR_ACCOUNTS_FILE": str(accounts),
            "MONITOR_SECRET_PROFILE": str(secret),
            "MONITOR_STATE_DIR": str(state),
        })
        ran = _run(
            [str(guardian), "run-once", "--mcm-config", str(config), "--no-send"],
            cwd=work,
            env=env,
        )
        payload = json.loads(ran.stdout)
        assert ran.returncode in {0, 2}, ran.stderr
        assert payload.get("status") != "error"
        assert payload["runtime"]["binding"] == "installed"
        assert isinstance(payload["coverage"], list)
        assert "site-packages" in Path(payload["runtime"]["guardian_file"]).parts
        connection = sqlite3.connect(state / "monitor.sqlite")
        try:
            migrations = [row[0] for row in connection.execute("SELECT migration_id FROM mcm_schema_migrations")]
        finally:
            connection.close()
        assert "0001_mcm_identity" in migrations
        assert "0002_mcm_events_coverage" in migrations
        assert "0003_mcm_delivery" in migrations

        probe = _run([str(python), "-c", _RESOURCE_PROBE], cwd=work)
        assert probe.returncode == 0, probe.stderr + probe.stdout
        report = json.loads(probe.stdout)
        assert report["capabilities"] >= 8
        assert report["networks"] >= 8
        assert "ethereum-mainnet" in report["network_ids"]
        assert report["migrations"] >= 3
        assert report["schema_type"] == "object"
        assert report["inside_package"] is True
        assert report["package_in_site_packages"] is True
        assert report["sys_path_hits"]
    finally:
        shutil.rmtree(work, ignore_errors=True)


def test_mcm_share_s4_03_pinned_install_uv_pip(release_dist: Path) -> None:
    """MCM-SHARE-S4-03. Constraints from uv.lock pin runtime deps for uv pip and pip."""
    pins = _constraint_pins((release_dist / "constraints.txt").read_text(encoding="utf-8"))
    locked = _runtime_pins(ROOT / "uv.lock")
    assert pins == locked
    assert "pytest" not in pins
    assert "guardian" not in pins
    for name in ("eth-abi", "eth-hash", "eth-utils", "httpx", "pycryptodome"):
        assert name in pins

    work = _outside_checkout()
    try:
        venv = work / "venv"
        created = _run(["uv", "venv", "--python", sys.executable, "--offline", str(venv)], cwd=work)
        assert created.returncode == 0, created.stderr
        python = _venv_python(venv)
        constraints = release_dist / "constraints.txt"
        deps = _run(
            ["uv", "pip", "install", "--offline", "--python", str(python), "-r", str(constraints)],
            cwd=work,
        )
        assert deps.returncode == 0, deps.stderr
        ensured = _run([str(python), "-m", "ensurepip", "--upgrade"], cwd=work)
        assert ensured.returncode == 0, ensured.stderr
        pip_env = _child_env({"PIP_NO_INDEX": "1", "PIP_DISABLE_PIP_VERSION_CHECK": "1"})
        pip_installed = _run(
            [
                str(python), "-m", "pip", "install", "--no-index",
                "--find-links", str(release_dist),
                "--constraint", str(constraints),
                str(_wheel(release_dist)),
            ],
            cwd=work,
            env=pip_env,
        )
        assert pip_installed.returncode == 0, pip_installed.stderr
        installed = _installed_versions(python, work)
        assert installed["guardian"] == "0.4.0rc1"
        for name, version in pins.items():
            assert installed[name] == version
    finally:
        shutil.rmtree(work, ignore_errors=True)


def test_mcm_share_s4_05_upgrade_rollback_artifact_hygiene(release_dist: Path) -> None:
    """MCM-SHARE-S4-05. Newer schema migrates, an older build refuses it, archives stay clean."""
    _assert_archives_clean(release_dist)
    sums = (release_dist / "SHA256SUMS").read_text(encoding="utf-8").splitlines()
    assert sums
    seen = {}
    for line in sums:
        digest, name = line.split()
        artifact = release_dist / name
        assert hashlib.sha256(artifact.read_bytes()).hexdigest() == digest
        seen[name] = digest
    assert any(name.endswith(".whl") for name in seen)
    assert any(name.endswith(".tar.gz") for name in seen)
    assert "constraints.txt" in seen

    work = _outside_checkout()
    try:
        database = work / "state.sqlite"
        connection = sqlite3.connect(database)
        connection.execute("PRAGMA user_version=1")
        connection.execute("CREATE TABLE tenants (tenant_id TEXT PRIMARY KEY, label TEXT NOT NULL)")
        connection.execute("INSERT INTO tenants(tenant_id, label) VALUES (?, ?)", ("synthetic-tenant", "kept-across-upgrade"))
        connection.commit()
        connection.close()

        venv = work / "venv"
        created = _run(["uv", "venv", "--python", sys.executable, "--offline", str(venv)], cwd=work)
        assert created.returncode == 0, created.stderr
        python = _venv_python(venv)
        constraints = release_dist / "constraints.txt"
        first = _run(
            ["uv", "pip", "install", "--offline", "--python", str(python), "--constraints", str(constraints), str(_wheel(release_dist))],
            cwd=work,
        )
        assert first.returncode == 0, first.stderr
        opened = _open_database(python, database, work)
        assert opened.returncode == 0, opened.stderr + opened.stdout
        assert "0001_mcm_identity" in _migration_ids(database)
        assert _tenant_label(database) == "kept-across-upgrade"

        newer = _build_future_wheel(work / "future-src", work / "future-dist")
        upgraded = _run(
            ["uv", "pip", "install", "--offline", "--reinstall", "--python", str(python), "--constraints", str(constraints), str(newer)],
            cwd=work,
        )
        assert upgraded.returncode == 0, upgraded.stderr
        migrated = _open_database(python, database, work)
        assert migrated.returncode == 0, migrated.stderr + migrated.stdout
        assert "0004_s4_future" in _migration_ids(database)
        assert _tenant_label(database) == "kept-across-upgrade"
        marker = sqlite3.connect(database)
        try:
            note = marker.execute("SELECT note FROM s4_future_marker WHERE id=1").fetchone()[0]
        finally:
            marker.close()
        assert note == "synthetic-future"

        rolled = _run(
            [
                "uv", "pip", "install", "--offline", "--reinstall", "--python", str(python),
                "--constraints", str(constraints), str(_wheel(release_dist)),
            ],
            cwd=work,
        )
        assert rolled.returncode == 0, rolled.stderr
        refused = _open_database(python, database, work)
        assert refused.returncode == 2
        assert "database schema is newer than this build" in refused.stdout
        assert "0004_s4_future" in refused.stdout
        assert _tenant_label(database) == "kept-across-upgrade"
        assert "0004_s4_future" in _migration_ids(database)
        kept = sqlite3.connect(database)
        try:
            still = kept.execute("SELECT note FROM s4_future_marker WHERE id=1").fetchone()[0]
        finally:
            kept.close()
        assert still == "synthetic-future"
    finally:
        shutil.rmtree(work, ignore_errors=True)


def _assert_archives_clean(dist: Path) -> None:
    required = (
        "schemas/coverage.schema.json",
        "config/source-capabilities/evm_rpc.json",
        "migrations/monitor/0001_mcm_identity.sql",
        "docs/mcm-01/network-tiers.plan.json",
        "examples/accounts.empty.template.json",
    )
    for artifact in [*dist.glob("*.whl"), *dist.glob("*.tar.gz")]:
        names = _archive_names(artifact)
        assert names
        for name in names:
            parts = set(Path(name).parts)
            assert not (parts & _FORBIDDEN_PARTS), name
            assert ".git" not in parts
            lowered = name.lower()
            assert ".env" not in Path(name).name
            assert "id_rsa" not in lowered
        blob = "\n".join(names)
        for marker in required:
            assert any(marker in name.replace("\\", "/") for name in names), marker
        accounts = next(name for name in names if name.endswith("examples/accounts.empty.template.json"))
        document = json.loads(_archive_text(artifact, accounts))
        assert document["accounts"] == []
        for name in names:
            if not name.endswith((".py", ".json", ".md", ".sql", ".txt", ".toml")):
                continue
            text = _archive_text(artifact, name)
            for pattern in _SECRET_PATTERNS:
                assert pattern.search(text) is None, name
        assert "action-client" not in blob


def _build_future_wheel(source: Path, out: Path) -> Path:
    ignore = shutil.ignore_patterns(".venv", "dist", "__pycache__", ".pytest_cache", "private", "data", "evidence", "node_modules")
    shutil.copytree(ROOT, source, ignore=ignore)
    future = source / "src" / "guardian" / "_resources" / "migrations" / "monitor" / "0004_s4_future.sql"
    future.write_text(
        "CREATE TABLE IF NOT EXISTS s4_future_marker (\n"
        "  id INTEGER PRIMARY KEY,\n"
        "  note TEXT NOT NULL\n"
        ");\n"
        "INSERT INTO s4_future_marker (id, note) VALUES (1, 'synthetic-future');\n",
        encoding="utf-8",
        newline="\n",
    )
    for relative in (
        "pyproject.toml",
        "src/guardian/__init__.py",
        "src/guardian/_resources/runtime-pin.json",
    ):
        path = source / relative
        path.write_text(path.read_text(encoding="utf-8").replace("0.4.0rc1", "0.4.0rc2"), encoding="utf-8")
    built = _run(
        ["uv", "build", "--offline", "--no-create-gitignore", "--out-dir", str(out), str(source)],
        cwd=source,
        timeout=240,
    )
    assert built.returncode == 0, built.stderr + built.stdout
    return _wheel(out)


def _open_database(python: Path, database: Path, cwd: Path) -> subprocess.CompletedProcess[str]:
    script = (
        "import sys\n"
        "from pathlib import Path\n"
        "from guardian.errors import ConfigError\n"
        "from guardian.storage import Database\n"
        "try:\n"
        "    Database(Path(sys.argv[1])).close()\n"
        "except ConfigError as exc:\n"
        "    print(exc)\n"
        "    raise SystemExit(2)\n"
        "print('opened')\n"
    )
    return _run([str(python), "-c", script, str(database)], cwd=cwd)


def _migration_ids(database: Path) -> list[str]:
    connection = sqlite3.connect(database)
    try:
        return [row[0] for row in connection.execute("SELECT migration_id FROM mcm_schema_migrations ORDER BY migration_id")]
    finally:
        connection.close()


def _tenant_label(database: Path) -> str:
    connection = sqlite3.connect(database)
    try:
        return connection.execute("SELECT label FROM tenants WHERE tenant_id=?", ("synthetic-tenant",)).fetchone()[0]
    finally:
        connection.close()


def _inside(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
    except ValueError:
        return False
    return True


def _tree_bytes(root: Path) -> dict[str, bytes]:
    found = {}
    for path in root.rglob("*"):
        if not path.is_file() or "__pycache__" in path.parts or path.suffix in {".pyc", ".pyo"}:
            continue
        found[path.relative_to(root).as_posix()] = path.read_bytes()
    return found


def _shadow_tool():
    name = "mcm_shadow_round_s4"
    path = ROOT / "tools" / "mcm_shadow_round.py"
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec is not None and spec.loader is not None
    spec.loader.exec_module(module)
    return module


_RESOURCE_PROBE = r"""
import json
import sys
from pathlib import Path

import guardian
from guardian.coverage import coverage_schema
from guardian.network_registry import load_registry
from guardian.resources import packaged_root, resource_path
from guardian.sources.capabilities import load_source_capabilities
from guardian.storage import mcm_migration_directory

package = Path(guardian.__file__).resolve().parent
root = packaged_root().resolve()
root.relative_to(package)
schema = coverage_schema()
registry = load_registry(resource_path("config", "chains"))
catalog = load_source_capabilities()
migrations = list(mcm_migration_directory().glob("*.sql"))
hits = []
for entry in sys.path:
    if not entry:
        continue
    resolved = str(Path(entry).resolve()).replace("\\", "/")
    if "site-packages" in resolved:
        hits.append(resolved)
print(json.dumps({
    "capabilities": len(catalog.adapters),
    "inside_package": True,
    "migrations": len(migrations),
    "network_ids": sorted(registry),
    "networks": len(registry),
    "package_in_site_packages": "site-packages" in package.parts,
    "schema_type": schema.get("type"),
    "sys_path_hits": hits,
}))
"""
