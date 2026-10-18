"""Offline contract tests for the optional-key Routescan history fallback."""

from __future__ import annotations

import json
import re
from pathlib import Path
from types import SimpleNamespace

import pytest

from guardian.errors import SecurityError
from guardian.configuration import _source_status, load_public_config
from guardian.pipeline import _indexer_ready, probe_sources
from guardian.scan import drive
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


def _world(*, credential: str = "") -> tuple[ReadOnlyTransport, EtherscanClient]:
    source = {
        "adapter": "routescan",
        "budget_group": "routescan-account",
        "endpoint_env": "ROUTESCAN_API_BASE",
        "credential_env": "ROUTESCAN_API_KEY",
    }
    budgets = {
        "routescan-account": {
            "max_concurrency": 1,
            "max_requests_per_day": 10_000,
            "max_rps": 100,
            "retry_attempts": 0,
        }
    }
    secrets = {"ROUTESCAN_API_BASE": ENDPOINT}
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
        transport.encode("routescan", _operation(client, chain_id, 1))
    for chain_id in (10, 56, 8453, 42161, 137):
        with pytest.raises(SecurityError):
            transport.request("routescan", _operation(client, chain_id, 1))
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
