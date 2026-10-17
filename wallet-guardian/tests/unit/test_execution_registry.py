"""Execution registry loader, policy rules, and candidate generation."""

from __future__ import annotations

import ast
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[2]
FIXTURE = ROOT / "tests" / "fixtures" / "execution" / "valid"
ORACLE = ROOT / "tests" / "fixtures" / "execution"
ACCOUNT = "0x0000000000000000000000000000000000000001"
OTHER = "0x0000000000000000000000000000000000000002"
VAULT = "0x0000000000000000000000000000000000000003"


@pytest.fixture(scope="module")
def api():
    from guardian.execution.candidates import propose_execution_candidates
    from guardian.execution.digest import policy_digest, registry_digest
    from guardian.execution.errors import ExecutionRegistryError
    from guardian.execution.identity import check_call, check_identity, retain_after_account_switch
    from guardian.execution.loader import (
        load_chains_text,
        load_execution_directory,
        load_registry_text,
        user_approval_granted,
    )
    from guardian.execution.model import ACTIONS, ADDRESS_ROLES, PERMISSION_TYPES, PROXY_KINDS
    from guardian.execution.strict_json import parse_strict_json

    return SimpleNamespace(
        ACTIONS=ACTIONS,
        ADDRESS_ROLES=ADDRESS_ROLES,
        ExecutionRegistryError=ExecutionRegistryError,
        PERMISSION_TYPES=PERMISSION_TYPES,
        PROXY_KINDS=PROXY_KINDS,
        check_call=check_call,
        check_identity=check_identity,
        load_chains_text=load_chains_text,
        load_execution_directory=load_execution_directory,
        load_registry_text=load_registry_text,
        parse_strict_json=parse_strict_json,
        policy_digest=policy_digest,
        propose_execution_candidates=propose_execution_candidates,
        registry_digest=registry_digest,
        retain_after_account_switch=retain_after_account_switch,
        user_approval_granted=user_approval_granted,
    )


def _text() -> str:
    return (FIXTURE / "registry.json").read_text(encoding="utf-8")


def _chains_text() -> str:
    return (FIXTURE / "chains.json").read_text(encoding="utf-8")


def _chains(api):
    return api.load_chains_text(_chains_text())


def _load(api, text: str):
    return api.load_registry_text(text, _chains(api))


def _code(api, text: str) -> str:
    with pytest.raises(api.ExecutionRegistryError) as caught:
        _load(api, text)
    assert api.user_approval_granted(text, _chains_text()) is False
    return caught.value.code


def _identity(account: str, siwe: str | None | object = "absent") -> dict:
    body = {key: account for key in (
        "from",
        "onBehalf",
        "receiver",
        "transfer_to",
        "eth_accounts_0",
        "plan_sender",
        "plan_recipient",
        "execution_account",
    )}
    if siwe != "absent":
        body["siwe_address"] = siwe
    return body


def test_float_rejected(api) -> None:
    text = _text().replace('"penalty_cap": "0"', '"penalty_cap": 1.0', 1)
    assert _code(api, text) == "float_rejected"
    with pytest.raises(api.ExecutionRegistryError) as caught:
        api.parse_strict_json("1e2")
    assert caught.value.code == "float_rejected"


def test_duplicate_key_rejected(api) -> None:
    for raw in ('{"a":1,"a":2}', '{"nested":{"a":1,"a":2}}', '{"a":1,"\\u0061":2}'):
        with pytest.raises(api.ExecutionRegistryError) as caught:
            api.parse_strict_json(raw)
        assert caught.value.code == "duplicate_key"


def test_unknown_field_rejected(api) -> None:
    text = _text().replace('"send_enabled": false', '"send_enabled": false, "extra": 1', 1)
    assert _code(api, text) == "unknown_field"


def test_proxy_kind_rejected(api) -> None:
    for kind in ("beacon", "uups"):
        text = _text().replace('"proxy_kind": "eip1967_impl"', f'"proxy_kind": "{kind}"', 1)
        assert _code(api, text) == "proxy_kind_rejected"


def test_recipient_not_self_rejected(api) -> None:
    text = _text().replace('"recipient_rule": "self"', '"recipient_rule": "other"', 1)
    assert _code(api, text) == "recipient_not_self"


def test_address_role_required(api) -> None:
    missing = _text().replace(
        '{"name": "receiver", "solidity_type": "address", "role": "receiver"}',
        '{"name": "receiver", "solidity_type": "address"}',
        1,
    )
    assert _code(api, missing) == "address_role_required"
    labeled = _text().replace(
        '{"name": "receiver", "solidity_type": "address", "role": "receiver"}',
        '{"name": "receiver", "solidity_type": "address", "label": "to"}',
        1,
    )
    assert _code(api, labeled) == "address_role_required"
    extra = _text().replace(
        '{"name": "receiver", "solidity_type": "address", "role": "receiver"}',
        '{"name": "receiver", "solidity_type": "address", "role": "receiver", "label": "to"}',
        1,
    )
    assert _code(api, extra) == "unknown_field"


def test_penalty_cap_not_zero(api) -> None:
    for raw in ('"penalty_cap": "1"', '"penalty_cap": 0', '"penalty_cap": "00"', '"penalty_cap": "0.0"'):
        text = _text().replace('"penalty_cap": "0"', raw, 1)
        assert _code(api, text) == "penalty_cap_not_zero"


def test_approved_by_user_has_no_effect(api) -> None:
    text = _text().replace('"user_approved": false', '"user_approved": false, "approved_by_user": true', 1)
    assert _code(api, text) == "approved_by_user_has_no_effect"
    granted = _text().replace('"user_approved": false', '"user_approved": true', 1)
    assert _code(api, granted) == "user_approved_has_no_effect"
    loaded = _load(api, _text())
    assert loaded["user_approved"] is False
    assert api.user_approval_granted(_text(), _chains_text()) is False
    assert all(item["user_approved"] is False for item in loaded["deployments"])
    assert all(item["approval"] == "not_approved" for item in loaded["policies"])


def test_valid_fixture_matches_oracle_and_digest(api) -> None:
    loaded = {"chains": _chains(api), "registry": _load(api, _text())}
    oracle = json.loads((FIXTURE / "semantic.json").read_text(encoding="utf-8"))
    assert loaded == oracle
    digests = json.loads((FIXTURE / "digests.json").read_text(encoding="utf-8"))
    assert api.registry_digest(loaded["registry"]) == digests["registry"]
    assert api.policy_digest(loaded["registry"]) == digests["policy"]
    assert loaded["chains"]["enables_send"] is False
    assert loaded["registry"]["send_enabled"] is False


def test_checked_in_catalog_matches_oracle(api) -> None:
    loaded = api.load_execution_directory(ROOT / "config" / "execution")
    oracle = json.loads((ORACLE / "catalog.semantic.json").read_text(encoding="utf-8"))
    assert loaded == oracle
    assert loaded["registry"]["deployments"] == []
    assert loaded["registry"]["policies"] == []
    assert loaded["chains"]["chains"] == []
    assert loaded["chains"]["enables_send"] is False
    note = "Changing this list is a trust-critical review change. The list is not a boundary."
    assert loaded["trust_critical_paths"]["note"] == note
    assert "wallet-guardian/config/execution/trust-critical-paths.json" in loaded["trust_critical_paths"]["paths"]
    assert "wallet-guardian/action-client/src/send/" in loaded["trust_critical_paths"]["paths"]


def test_schema_enums_match_loader(api) -> None:
    schema = json.loads((ROOT / "config" / "execution" / "schemas" / "registry.schema.json").read_text(encoding="utf-8"))
    deployment = schema["$defs"]["deployment"]["properties"]
    policy = schema["$defs"]["policy"]["properties"]
    parameter = schema["$defs"]["parameter"]["properties"]
    assert tuple(deployment["proxy_kind"]["enum"]) == api.PROXY_KINDS
    assert tuple(deployment["permission_type"]["enum"]) == api.PERMISSION_TYPES
    assert tuple(policy["action"]["enum"]) == api.ACTIONS
    assert tuple(parameter["role"]["enum"]) == api.ADDRESS_ROLES
    assert policy["penalty_cap"]["const"] == "0"
    assert policy["recipient_rule"]["const"] == "self"
    release = json.loads((ROOT / "action-client" / "deploy" / "release.schema.json").read_text(encoding="utf-8"))
    release_def = release["$defs"]["release"]
    assert release_def["properties"]["mainnet_send"]["const"] is False
    assert "digests" not in release_def["required"]
    assert set(release_def["properties"]["digests"]["required"]) == {
        "execution_registry",
        "policy",
        "core_bundle",
        "dist",
    }


def test_identity_binding_and_call_rules(api) -> None:
    registry = _load(api, _text())
    api.check_identity(ACCOUNT, _identity(ACCOUNT))
    api.check_identity(ACCOUNT, _identity(ACCOUNT, None))
    with pytest.raises(api.ExecutionRegistryError) as caught:
        api.check_identity(ACCOUNT, _identity(ACCOUNT, OTHER))
    assert caught.value.code == "identity_mismatch"
    mismatched = _identity(ACCOUNT)
    mismatched["from"] = OTHER
    with pytest.raises(api.ExecutionRegistryError) as caught:
        api.check_identity(ACCOUNT, mismatched)
    assert caught.value.code == "identity_mismatch"
    recipient = _identity(ACCOUNT)
    recipient["receiver"] = OTHER
    with pytest.raises(api.ExecutionRegistryError) as caught:
        api.check_identity(ACCOUNT, recipient)
    assert caught.value.code == "recipient_not_self"
    with pytest.raises(api.ExecutionRegistryError) as caught:
        api.check_identity(ACCOUNT, {**_identity(ACCOUNT), "own_addresses": [ACCOUNT]})
    assert caught.value.code == "own_address_list_rejected"
    kept = api.retain_after_account_switch({"evidence": {"gone": True}, "pending": [1], "unknown": {"left": True}})
    assert kept == {"pending": [1], "unknown": {"left": True}}
    api.check_call(
        registry,
        policy_id="fixture-redeem",
        account=ACCOUNT,
        identity=_identity(ACCOUNT),
        arguments={"owner": ACCOUNT, "receiver": ACCOUNT, "shares": "1"},
    )
    with pytest.raises(api.ExecutionRegistryError) as caught:
        api.check_call(
            registry,
            policy_id="fixture-redeem",
            account=ACCOUNT,
            identity=_identity(ACCOUNT),
            arguments={"owner": ACCOUNT, "receiver": OTHER, "shares": "1"},
        )
    assert caught.value.code == "recipient_not_self"
    with pytest.raises(api.ExecutionRegistryError) as caught:
        api.check_call(
            registry,
            policy_id="fixture-revoke",
            account=ACCOUNT,
            identity=_identity(ACCOUNT),
            arguments={"spender": OTHER},
        )
    assert caught.value.code == "not_registered"
    api.check_call(
        registry,
        policy_id="fixture-revoke",
        account=ACCOUNT,
        identity=_identity(ACCOUNT),
        arguments={"spender": VAULT},
    )


def test_candidates_are_unapproved_public_contracts(api) -> None:
    registry_bytes = (ROOT / "config" / "execution" / "registry.json").read_bytes()
    lista_bytes = (ROOT / "config" / "protocols" / "lista-bsc.json").read_bytes()
    document = api.propose_execution_candidates(ROOT)
    assert (ROOT / "config" / "execution" / "registry.json").read_bytes() == registry_bytes
    assert (ROOT / "config" / "protocols" / "lista-bsc.json").read_bytes() == lista_bytes
    oracle = json.loads((ORACLE / "candidates.semantic.json").read_text(encoding="utf-8"))
    assert document == oracle
    assert document["user_approved"] is False
    assert document["approval"] == "candidate_unapproved"
    assert len(document["deployments"]) == 11
    packaged = "\n".join(
        path.read_text(encoding="utf-8")
        for path in list((ROOT / "config" / "protocols").glob("*.json"))
        + list((ROOT / "config" / "deployments" / "permit2").glob("*.json"))
    ).lower()
    proxy_kinds = set()
    for item in document["deployments"]:
        assert item["user_approved"] is False
        assert item["approval"] == "candidate_unapproved"
        assert item["record_status"] == "candidate"
        assert item["pin_status"] == "not_read_offline"
        proxy_kinds.add(item["proxy_kind"])
        assert item["address"].lower() in packaged
        assert item["official_sources"]
        assert all(source["url"].startswith("https://") for source in item["official_sources"])
    assert proxy_kinds == {"none", "eip1967_impl"}
    lista = [item for item in document["deployments"] if item["protocol_id"] == "lista_slisbnb"]
    assert len(lista) == 1
    assert lista[0]["proxy_kind"] == "eip1967_impl"
    assert lista[0]["permission_type"] == "queue_vault"
    assert [source["retrieved"] for source in lista[0]["official_sources"] if source["url"].endswith("/smart-contract")] == ["2026-10-09"]
    assert sum(item["permission_type"] == "permit2" for item in document["deployments"]) == 7
    reasons = {item["reason"] for item in document["unclassified"]}
    assert "not_evm" in reasons
    assert "proxy_kind_not_documented" in reasons
    assert all("proxy_kind" not in item for item in document["unclassified"])
    assert all(item["user_approved"] is False for item in document["unclassified"])

    def keys(value):
        found = set()
        if isinstance(value, dict):
            found.update(value)
            for item in value.values():
                found.update(keys(item))
        elif isinstance(value, list):
            for item in value:
                found.update(keys(item))
        return found

    present = keys(document)
    assert "approved_by_user" not in present
    assert "execution_authorized" not in present
    assert "enabled" not in present


def test_execution_package_does_not_open_send(api) -> None:
    from guardian.actions.workbench import evidence_status

    status = evidence_status()
    assert status["fixed_https_origin"] is False
    assert status["mainnet_execution_authorized"] is False
    forbidden = {
        "eth_sendTransaction",
        "eth_sendRawTransaction",
        "wallet_sendCalls",
        "eth_sign",
        "personal_sign",
    }
    package = ROOT / "src" / "guardian" / "execution"
    for path in package.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        assert "parents[" not in text
        assert "parent.parent" not in text
        tree = ast.parse(text)
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module and "actions" in node.module.split("."):
                raise AssertionError(path.name)
            if isinstance(node, ast.Constant) and isinstance(node.value, str) and node.value in forbidden:
                raise AssertionError(node.value)
    registry = (ROOT / "src" / "guardian" / "network_registry.py").read_text(encoding="utf-8")
    assert "enabled must be false" in registry
    assert "non-synthetic address" in registry
    manifest = (ROOT / "src" / "guardian" / "protocols" / "manifest.py").read_text(encoding="utf-8")
    assert "enabled_not_allowed" in manifest
    assert "live_pass" in manifest
    assert api.user_approval_granted(_text(), _chains_text()) is False


def _negative_vectors() -> list[dict]:
    payload = json.loads((ORACLE / "negative-vectors.json").read_text(encoding="utf-8"))
    return payload["vectors"]


def _apply_pairs(text: str, pairs: list[list[str]] | None) -> str:
    for old, new in pairs or []:
        if old not in text:
            raise AssertionError(f"patch not found: {old[:80]!r}")
        text = text.replace(old, new, 1)
    return text


def _expect_code(api, fn, code: str) -> None:
    try:
        fn()
    except api.ExecutionRegistryError as exc:
        assert exc.code == code
        return
    except Exception as exc:
        raise AssertionError(f"expected {code}, got {type(exc).__name__}: {exc}") from exc
    raise AssertionError(f"expected {code}")


def _assert_not_approved(api, text: str, chains: str | None = None) -> None:
    try:
        granted = api.user_approval_granted(text, chains)
    except Exception as exc:
        raise AssertionError(f"user_approval_granted raised {type(exc).__name__}: {exc}") from exc
    assert granted is False


@pytest.mark.parametrize("vector", _negative_vectors(), ids=lambda item: item["id"])
def test_negative_vector(api, vector: dict) -> None:
    """Same crafted inputs and expected codes as the JS loader."""
    chains = _apply_pairs(_chains_text(), vector.get("chains_replace"))
    registry = _apply_pairs(_text(), vector.get("registry_replace"))
    code = vector.get("code")
    loader = vector["loader"]
    if loader == "parse":
        raw = vector["input"]
        if code is None:
            api.parse_strict_json(raw)
        else:
            _expect_code(api, lambda: api.parse_strict_json(raw), code)
        _assert_not_approved(api, raw)
        return
    if loader == "chains":
        if code is None:
            api.load_chains_text(chains)
        else:
            _expect_code(api, lambda: api.load_chains_text(chains), code)
        _assert_not_approved(api, registry, chains)
        return
    if loader == "registry":
        loaded_chains = api.load_chains_text(chains)

        def load_registry():
            return api.load_registry_text(registry, loaded_chains)

        if code is None:
            loaded = load_registry()
            if "expect_urls" in vector:
                urls = [item["url"] for item in loaded["deployments"][0]["official_sources"]]
                assert urls == vector["expect_urls"]
            if "registry_digest" in vector:
                assert api.registry_digest(loaded) == vector["registry_digest"]
            if "expect_protocol_version" in vector:
                assert loaded["deployments"][0]["protocol_version"] == vector["expect_protocol_version"]
        else:
            _expect_code(api, load_registry, code)
        _assert_not_approved(api, registry, chains)
        return
    if loader == "check_call":
        loaded = api.load_registry_text(registry, api.load_chains_text(chains))

        def call() -> None:
            api.check_call(
                loaded,
                policy_id=vector["policy_id"],
                account=ACCOUNT,
                identity=_identity(ACCOUNT),
                arguments=vector["arguments"],
            )

        if code is None:
            call()
        else:
            _expect_code(api, call, code)
        return
    raise AssertionError(loader)
