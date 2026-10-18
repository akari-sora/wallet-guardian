"""Offline contract tests for the optional-key Routescan history fallback."""

from __future__ import annotations

import json
import re
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from guardian.errors import SecurityError
from guardian.configuration import _source_status, load_public_config
from guardian.onboarding import _build
from guardian.pipeline import _indexer_ready, probe_sources
from guardian.scan import drive
from guardian.security.transport_policy import assert_operation_allowed
from guardian.sources.budget import BudgetBook
from guardian.sources.capabilities import load_source_capabilities
from guardian.sources.etherscan import EtherscanClient
from guardian.sources.fixtures import FixtureBackend, RawResponse, request_key
from guardian.sources.key_guide import _ANCHOR
from guardian.sources.transport import ReadOnlyTransport, build_endpoint_registry

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "tests" / "fixtures" / "routescan"
ENDPOINT = "https://api.routescan.io"
ADDRESS = "0x0000000000000000000000000000000000000001"
SYNTHETIC_KEY = "synthetic-routescan-test-secret"


def _payload(name: str) -> dict[str, object]:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def _world(
    *,
    credential: str = "",
    daily: int = 10_000,
    retry_attempts: int = 0,
    endpoint: str | None = ENDPOINT,
) -> tuple[ReadOnlyTransport, EtherscanClient]:
    source = {
        "adapter": "routescan",
        "budget_group": "routescan-account",
        "endpoint_env": "ROUTESCAN_API_BASE",
        "credential_env": "ROUTESCAN_API_KEY",
    }
    budgets = {
        "routescan-account": {
            "max_concurrency": 1,
            "max_requests_per_day": daily,
            "max_rps": 100,
            "retry_attempts": retry_attempts,
        }
    }
    secrets = {}
    if endpoint is not None:
        secrets["ROUTESCAN_API_BASE"] = endpoint
    if credential:
        secrets["ROUTESCAN_API_KEY"] = credential
    endpoints = build_endpoint_registry(
        {"budgets": budgets, "sources": {"routescan": source}}, secrets=secrets
    )
    transport = ReadOnlyTransport(
        endpoints,
        BudgetBook.from_document(budgets, clock=lambda: 1_700_000_000.0, sleep=lambda _seconds: None),
        FixtureBackend({}),
        resolve=lambda _host: ["1.1.1.1"],
    )
    client = EtherscanClient(
        transport,
        "routescan",
        credential=credential,
        credential_env="ROUTESCAN_API_KEY",
        adapter="routescan",
        clock=lambda: 1_700_000_000.0,
        sleep=lambda _seconds: None,
        random=lambda: 0.0,
    )
    return transport, client


def _operation(client: EtherscanClient, chain_id: int, page: int, *, offset: int = 2):
    return client.operation(
        chain_id=chain_id,
        module="account",
        action="txlist",
        address=ADDRESS,
        start_block=100,
        end_block=600,
        page=page,
        offset=offset,
    )


def _install(transport: ReadOnlyTransport, operation: object, fixture: str) -> None:
    call = transport.encode("routescan", operation)
    raw = RawResponse(200, {}, json.dumps(_payload(fixture)).encode("utf-8"))
    transport.backend.mapping[request_key(call.method, call.url, call.body)] = raw


@pytest.mark.parametrize(
    ("chain_id", "first", "second", "hashes"),
    [
        (1, "ethereum-page-1.json", "ethereum-page-2.json", [f"0x{number:064x}" for number in (1, 2, 3)]),
        (43114, "avalanche-page-1.json", "avalanche-page-2.json", [f"0x{number:064x}" for number in (4, 5, 6)]),
    ],
)
def test_keyless_routescan_paginates_supported_chains(
    chain_id: int, first: str, second: str, hashes: list[str]
) -> None:
    transport, client = _world()
    _install(transport, _operation(client, chain_id, 1), first)
    _install(transport, _operation(client, chain_id, 2), second)

    read = client.account_history(
        chain_id=chain_id,
        action="txlist",
        address=ADDRESS,
        start_block=100,
        end_block=600,
        offset=2,
        previous_cursor="99",
    )

    assert read.category == "ok"
    assert read.pagination_complete is True
    assert read.cursor == "600"
    assert [item["hash"] for item in read.items] == hashes
    assert len(transport.backend.calls) == 2
    assert all("apikey=" not in call.url.lower() for call in transport.backend.calls)
    assert all(f"/evm/{chain_id}/etherscan/api" in call.url for call in transport.backend.calls)


def test_optional_key_is_omitted_when_empty_and_redacted_when_present() -> None:
    keyless_transport, keyless = _world()
    keyless_url = keyless_transport.encode("routescan", _operation(keyless, 1, 1)).url
    assert "apikey=" not in keyless_url.lower()

    keyed_transport, keyed = _world(credential=SYNTHETIC_KEY)
    keyed_url = keyed_transport.encode("routescan", _operation(keyed, 1, 1)).url
    assert f"apikey={SYNTHETIC_KEY}" in keyed_url
    assert SYNTHETIC_KEY not in keyed.redact_error(keyed_url)
    assert SYNTHETIC_KEY not in repr(keyed)


def test_chain_not_supported_is_blocked_and_does_not_advance_cursor() -> None:
    transport, client = _world()
    _install(transport, _operation(client, 1, 1), "unsupported.json")

    read = client.account_history(
        chain_id=1,
        action="txlist",
        address=ADDRESS,
        start_block=100,
        end_block=600,
        offset=2,
        previous_cursor="99",
    )

    assert read.category == "error"
    assert read.blocked is True
    assert read.reason == "plan_not_supported"
    assert read.reason_codes == ("plan_not_supported",)
    assert read.items == ()
    assert read.cursor == "99"


def test_genuine_empty_advances_but_malformed_response_does_not() -> None:
    transport, client = _world()
    _install(transport, _operation(client, 43114, 1), "empty.json")
    empty = client.account_history(
        chain_id=43114,
        action="txlist",
        address=ADDRESS,
        start_block=100,
        end_block=600,
        offset=2,
        previous_cursor="99",
    )
    assert empty.category == "empty"
    assert empty.pagination_complete is True
    assert empty.cursor == "600"

    transport, client = _world()
    _install(transport, _operation(client, 43114, 1), "malformed.json")
    failed = client.account_history(
        chain_id=43114,
        action="txlist",
        address=ADDRESS,
        start_block=100,
        end_block=600,
        offset=2,
        previous_cursor="99",
    )
    assert failed.category == "error"
    assert failed.reason == "malformed"
    assert failed.cursor == "99"


def test_transport_policy_allows_only_the_two_declared_routescan_chain_paths() -> None:
    transport, client = _world()
    for chain_id in (1, 43114):
        operation = _operation(client, chain_id, 1)
        assert_operation_allowed(operation)
        _install(transport, operation, "empty.json")
        result = transport.request("routescan", operation)
        assert result.status == 200
    for chain_id in (10, 56, 8453, 42161, 137):
        with pytest.raises(SecurityError) as denied:
            transport.request("routescan", _operation(client, chain_id, 1))
        assert denied.value.reason == "rest_denied"
    assert len(transport.backend.calls) == 2


def test_routescan_path_and_chainid_mismatch_is_refused_before_send() -> None:
    transport, client = _world()
    operation = _operation(client, 1, 1)
    mismatched = replace(operation, query={**dict(operation.query or {}), "chainid": "43114"})
    with pytest.raises(SecurityError) as denied:
        transport.request("routescan", mismatched)
    assert denied.value.reason == "rest_denied"
    assert transport.backend.calls == []


@pytest.mark.parametrize("filename", ["monitor.json", "monitor.nokeys.json"])
def test_examples_have_routescan_fallback_and_packaged_copy(filename: str) -> None:
    editable = ROOT / "examples" / filename
    packaged = ROOT / "src" / "guardian" / "_resources" / "examples" / filename
    assert editable.read_bytes() == packaged.read_bytes()
    document = json.loads(editable.read_text(encoding="utf-8"))
    load_public_config(editable)

    source = document["sources"]["routescan"]
    assert source["adapter"] == "routescan"
    assert source["credential_env"] == "ROUTESCAN_API_KEY"
    assert source["endpoint_env"] == "ROUTESCAN_API_BASE"
    assert source["enabled"] is True
    assert document["budgets"][source["budget_group"]]["max_rps"] == 1
    for network in ("ethereum-mainnet", "avalanche-c"):
        route = next(
            row
            for row in document["routing"]
            if row["network"] == network and row["capability"] == "history.native_external"
        )
        assert route["source_ids"][:2] == ["etherscan", "routescan"]


def test_routescan_capability_catalog_is_synced_and_scoped_to_two_networks() -> None:
    editable = ROOT / "config" / "source-capabilities" / "routescan.json"
    packaged = ROOT / "src" / "guardian" / "_resources" / "config" / "source-capabilities" / "routescan.json"
    assert editable.read_bytes() == packaged.read_bytes()

    adapter = load_source_capabilities(ROOT / "config" / "source-capabilities").get("routescan")
    assert adapter.networks == frozenset({"ethereum-mainnet", "avalanche-c"})
    assert adapter.declares_support("ethereum-mainnet", "history.native_external") == "partial"
    assert adapter.declares_support("avalanche-c", "history.native_external") == "partial"
    assert adapter.declares_support("optimism-mainnet", "history.native_external") is None


def test_history_falls_through_missing_etherscan_key_to_keyless_routescan(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    document = {
        "sources": {
            "etherscan": {
                "adapter": "etherscan_v2",
                "credential_env": "ETHERSCAN_API_KEY",
                "enabled": True,
            },
            "routescan": {
                "adapter": "routescan",
                "credential_env": "ROUTESCAN_API_KEY",
                "enabled": True,
            },
        },
        "routing": [
            {
                "network": "ethereum-mainnet",
                "capability": "history.native_external",
                "source_ids": ["etherscan", "routescan"],
            }
        ],
    }
    scan = SimpleNamespace(
        public_config=SimpleNamespace(document=document),
        network="ethereum-mainnet",
        head=123,
        observations={},
    )
    private = SimpleNamespace(
        source_status={
            "etherscan": SimpleNamespace(credential_present=False),
            "routescan": SimpleNamespace(credential_present=False),
        }
    )
    attempted: list[tuple[str, str]] = []

    monkeypatch.setattr(drive, "_endpoint_ready", lambda _scan, _source_id: True)
    monkeypatch.setattr(drive, "_record_indexer", lambda *_args: None)
    monkeypatch.setattr(drive, "_note_bsc_internal", lambda *_args: None)

    def run(_scan, source_id, _source, adapter, *_args):
        attempted.append((source_id, adapter))
        return drive._Outcome(rows=[{"source_id": source_id}])

    monkeypatch.setattr(drive, "_run_indexer", run)

    rows = drive.history_external(scan, private, "wallet-1", ADDRESS, None, None)
    assert attempted == [("routescan", "routescan")]
    assert rows == [{"source_id": "routescan"}]


def test_etherscan_factory_preserves_routescan_adapter_and_optional_empty_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(drive, "_secret", lambda _scan, name: "" if name == "ROUTESCAN_API_KEY" else "unexpected")
    scan = SimpleNamespace(transport=object(), clock=lambda: 1.0, source_id="routescan")
    client = drive._etherscan(
        scan,
        "routescan",
        {"adapter": "routescan", "credential_env": "ROUTESCAN_API_KEY"},
        transport=object(),
    )
    assert client.adapter == "routescan"
    assert client.credential_env == "ROUTESCAN_API_KEY"
    assert "apikey" not in client.operation(
        chain_id=1,
        module="account",
        action="txlist",
        address=ADDRESS,
        start_block=1,
        end_block=2,
        page=1,
        offset=1,
    ).query


def test_every_key_guide_anchor_exists_and_documented_credentials_are_mapped() -> None:
    text = (ROOT / "docs" / "api-keys.md").read_text(encoding="utf-8")
    for env_name, anchor in _ANCHOR.items():
        assert f'<a id="{anchor}"></a>' in text, f"missing anchor for {env_name}"

    documented = set(
        re.findall(r"`([A-Z][A-Z0-9_]*(?:API_KEY|ACCESS_KEY|SLS_FEED))`", text)
    )
    assert documented <= set(_ANCHOR), f"missing key-guide entries: {sorted(documented - set(_ANCHOR))}"
    assert "ROUTESCAN_API_KEY" in documented


@pytest.mark.parametrize("network", ["ethereum-mainnet", "avalanche-c"])
def test_routescan_keyless_status_and_required_history(network: str) -> None:
    config = load_public_config(ROOT / "examples" / "monitor.nokeys.json")
    status = _source_status(config.document, {}, {})
    assert status["routescan"].availability == "keyless"
    assert status["routescan"].blocked_reason is None
    assert not status["routescan"].credential_present
    assert _indexer_ready(config, SimpleNamespace(source_status=status), network)


def test_source_probe_does_not_report_optional_routescan_key_as_required(monkeypatch):
    transport, _client = _world()
    config = load_public_config(ROOT / "examples" / "monitor.nokeys.json")
    private = SimpleNamespace(source_status=_source_status(config.document, {}, {}))
    monkeypatch.setattr("guardian.pipeline._transport", lambda *_args: transport)
    result = probe_sources(config, private, backend=transport.backend, clock=lambda: 0)
    source = next(row for row in result["sources"] if row["id"] == "routescan")
    assert source["credential_required"] is False
    assert source["credential_present"] is False
    assert "credential_hint" not in source
    assert source["identity"]["status"] == "not_probed"


def _history(client: EtherscanClient, chain_id: int = 1):
    return client.account_history(
        chain_id=chain_id,
        action="txlist",
        address=ADDRESS,
        start_block=100,
        end_block=600,
        offset=2,
        previous_cursor="99",
    )


def _install_json(transport: ReadOnlyTransport, operation: object, body: object, status: int = 200) -> None:
    call = transport.encode("routescan", operation)
    raw = RawResponse(status, {}, json.dumps(body).encode("utf-8"))
    transport.backend.mapping[request_key(call.method, call.url, call.body)] = raw


@pytest.mark.parametrize(
    "body",
    [
        {"message": "NOTOK", "result": []},
        {"status": "2", "message": "OK", "result": []},
        {"status": "1", "message": "NOTOK", "result": []},
        {"status": "1", "message": "No transactions found", "result": []},
        {"status": "1", "message": "OK", "result": []},
        {"status": "0", "message": "NOTOK", "result": []},
        {"status": "0", "message": "OK", "result": []},
        {"message": "OK", "result": [{"hash": "0x" + "11" * 32}]},
    ],
)
def test_unrecognized_scan_envelope_does_not_complete_or_advance(body: dict) -> None:
    transport, client = _world()
    _install_json(transport, _operation(client, 1, 1), body)
    read = _history(client)
    assert read.pagination_complete is False
    assert read.category != "empty"
    assert read.cursor == "99"


def test_routescan_429_does_not_advance_the_cursor() -> None:
    transport, client = _world()
    _install_json(transport, _operation(client, 1, 1), {"message": "NOTOK", "result": "rate limit"}, 429)
    read = _history(client)
    assert read.category == "error"
    assert read.reason == "rate_limited"
    assert read.pagination_complete is False
    assert read.cursor == "99"


def test_routescan_notok_string_is_not_empty_history() -> None:
    transport, client = _world()
    _install_json(
        transport,
        _operation(client, 43114, 1),
        {"status": "0", "message": "NOTOK", "result": "Max calls per sec rate limit reached"},
    )
    read = _history(client, 43114)
    assert read.category != "empty"
    assert read.pagination_complete is False
    assert read.cursor == "99"


def test_per_sec_limit_is_retried_then_empty_can_advance() -> None:
    transport, client = _world(retry_attempts=1)
    operation = _operation(client, 1, 1)
    call = transport.encode("routescan", operation)
    limited = {"status": "0", "message": "NOTOK", "result": "Max calls per sec rate limit reached"}
    empty = {"status": "0", "message": "No transactions found", "result": []}
    transport.backend.mapping[request_key(call.method, call.url, call.body)] = [
        RawResponse(200, {}, json.dumps(limited).encode("utf-8")),
        RawResponse(200, {}, json.dumps(empty).encode("utf-8")),
    ]
    read = _history(client)
    assert read.category == "empty"
    assert read.cursor == "600"
    assert len(transport.backend.calls) == 2


def test_request_cap_does_not_advance_the_cursor() -> None:
    transport, client = _world()
    _install(transport, _operation(client, 1, 1), "ethereum-page-1.json")
    read = client.account_history(
        chain_id=1,
        action="txlist",
        address=ADDRESS,
        start_block=100,
        end_block=600,
        offset=2,
        previous_cursor="99",
        request_cap=1,
    )
    assert read.pagination_complete is False
    assert read.cursor == "99"
    assert read.reason == "request_cap"


def test_page_cap_does_not_advance_the_cursor() -> None:
    transport, client = _world()
    _install(transport, _operation(client, 1, 1), "ethereum-page-1.json")
    read = client.account_history(
        chain_id=1,
        action="txlist",
        address=ADDRESS,
        start_block=100,
        end_block=600,
        offset=2,
        previous_cursor="99",
        page_cap=1,
    )
    assert read.pagination_complete is False
    assert read.cursor == "99"
    assert read.reason == "page_cap"


def test_budget_exhaustion_does_not_advance_the_cursor() -> None:
    transport, client = _world(daily=1)
    _install(transport, _operation(client, 1, 1), "ethereum-page-1.json")
    read = _history(client)
    assert read.pagination_complete is False
    assert read.cursor == "99"
    assert read.reason == "budget"


def test_routescan_uses_the_public_default_base_when_unset() -> None:
    endpoints = build_endpoint_registry(
        {"sources": {"routescan": {
            "adapter": "routescan",
            "budget_group": "routescan-account",
            "credential_env": "ROUTESCAN_API_KEY",
            "endpoint_env": "ROUTESCAN_API_BASE",
        }}},
        secrets={},
        environ={},
    )
    source = endpoints.get("routescan")
    assert source.endpoint == "https://api.routescan.io"
    assert source.endpoint_provenance["selected_from"] == "default"
    assert source.endpoint_provenance["present"] is True
    assert "api.routescan.io" not in json.dumps(source.endpoint_provenance)
    explicit = build_endpoint_registry(
        {"sources": {"routescan": {
            "adapter": "routescan",
            "budget_group": "routescan-account",
            "credential_env": "ROUTESCAN_API_KEY",
            "endpoint_env": "ROUTESCAN_API_BASE",
        }}},
        secrets={"ROUTESCAN_API_BASE": "https://routescan.example.invalid"},
    )
    assert explicit.get("routescan").endpoint == "https://routescan.example.invalid"


def _init_answers(networks: list[str]) -> dict:
    return {
        "accounts": [{
            "address": ADDRESS,
            "family": "evm",
            "label": "synthetic-desk",
            "networks": networks,
            "relationship": "watch_only",
        }],
        "consent": True,
        "credential_envs": {},
        "key_mode": "none",
        "plugins": [],
        "tenant_id": "local",
    }


def _history_route(monitor: dict, network: str) -> dict:
    return next(
        row for row in monitor["routing"]
        if row["network"] == network and row["capability"] == "history.native_external"
    )


def test_init_adds_keyless_routescan_and_base_alchemy() -> None:
    eth = _build(_init_answers(["ethereum-mainnet"]), recorded_at="2026-10-18T00:00:00Z")
    eth_route = _history_route(eth["monitor"], "ethereum-mainnet")
    assert eth_route["source_ids"][:2] == ["etherscan", "routescan"]
    routescan = eth["monitor"]["sources"]["routescan"]
    assert routescan["adapter"] == "routescan"
    assert routescan["credential_env"] == "ROUTESCAN_API_KEY"
    assert routescan["endpoint_env"] == "ROUTESCAN_API_BASE"
    assert routescan["enabled"] is True
    shared = next(row for row in eth["sharing"] if row["source_id"] == "routescan")
    assert shared["sends_addresses"] is True
    assert "keyless" in shared["data"]
    assert "credential_missing" not in shared["data"]

    avalanche = _build(_init_answers(["avalanche-c"]), recorded_at="2026-10-18T00:00:00Z")
    avalanche_route = _history_route(avalanche["monitor"], "avalanche-c")
    assert avalanche_route["source_ids"][:2] == ["etherscan", "routescan"]

    base = _build(_init_answers(["base-mainnet"]), recorded_at="2026-10-18T00:00:00Z")
    base_route = _history_route(base["monitor"], "base-mainnet")
    assert base_route["source_ids"][0] == "alchemy-base"
    alchemy = base["monitor"]["sources"]["alchemy-base"]
    assert alchemy["adapter"] == "alchemy_transfers"
    assert alchemy["credential_env"] == "ALCHEMY_API_KEY"
    assert alchemy["endpoint_env"] == "ALCHEMY_TRANSFERS_URL"
    status = _source_status(base["monitor"], {}, {})
    assert status["alchemy-base"].blocked_reason == "credential_missing"
    assert status["alchemy-base"].availability == "missing"

    other = _build(_init_answers(["polygon-pos"]), recorded_at="2026-10-18T00:00:00Z")
    assert "routescan" not in other["monitor"]["sources"]
    assert "alchemy-base" not in other["monitor"]["sources"]


def test_avalanche_credential_refusal_names_routescan() -> None:
    observations: dict = {}

    def hold(book, wallet, network, capability):
        book.add((wallet, network, capability))

    scan = SimpleNamespace(
        network="avalanche-c",
        observations=observations,
        held_cursors=set(),
        hold=hold,
        public_config=SimpleNamespace(document={
            "routing": [{
                "capability": "history.native_external",
                "network": "avalanche-c",
                "source_ids": ["etherscan", "routescan"],
            }],
            "sources": {
                "etherscan": {
                    "adapter": "etherscan_v2",
                    "credential_env": "ETHERSCAN_API_KEY",
                    "enabled": True,
                },
                "routescan": {
                    "adapter": "routescan",
                    "credential_env": "ROUTESCAN_API_KEY",
                    "enabled": True,
                },
            },
        }),
    )
    drive._refuse_history(scan, "wallet-1", "credential_missing", "etherscan_v2", "ETHERSCAN_API_KEY")
    hint = observations[("wallet-1", "avalanche-c", "history.native_external")].credential_hint
    assert "ROUTESCAN_API_KEY" in hint
    assert "ETHERSCAN_API_KEY" not in hint

    observations.clear()
    scan.public_config.document["sources"]["routescan"]["enabled"] = False
    drive._refuse_history(scan, "wallet-1", "credential_missing", "etherscan_v2", "ETHERSCAN_API_KEY")
    plain = observations[("wallet-1", "avalanche-c", "history.native_external")].credential_hint
    assert "ETHERSCAN_API_KEY" in plain
