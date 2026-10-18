"""Official Etherscan origin when ETHERSCAN_API_BASE is unset. No network."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from guardian.cli import _mcm_coverage
from guardian.configuration import _source_status
from guardian.diagnostics import diagnostic_fields
from guardian.errors import SecurityError
from guardian.scan import drive
from guardian.security.transport_policy import Operation
from guardian.service import doctor
from guardian.sources.budget import BudgetBook
from guardian.sources.etherscan import EtherscanClient
from guardian.sources.fixtures import FixtureBackend
from guardian.sources.transport import ReadOnlyTransport, build_endpoint_registry

ROOT = Path(__file__).resolve().parents[2]
ORIGIN = "https://api.etherscan.io"
KEY = "synthetic-etherscan-default-key"
ADDRESS = "0x0000000000000000000000000000000000000001"
SOURCE = {
    "adapter": "etherscan_v2",
    "budget_group": "etherscan-account",
    "credential_env": "ETHERSCAN_API_KEY",
    "endpoint_env": "ETHERSCAN_API_BASE",
    "enabled": True,
}
DOCUMENT = {"sources": {"etherscan": SOURCE}}
BUDGETS = {
    "etherscan-account": {
        "max_concurrency": 1,
        "max_requests_per_day": 10,
        "max_rps": 10,
        "retry_attempts": 0,
    }
}


def _registry(secrets=None, environ=None):
    return build_endpoint_registry(DOCUMENT, secrets=secrets or {}, environ=environ or {})


@pytest.mark.parametrize("blank", [None, "", " \t "])
@pytest.mark.parametrize("pool", ["env", "profile"])
def test_etherscan_uses_the_public_default_base_when_unset(blank, pool) -> None:
    values: dict[str, str] = {}
    if blank is not None:
        values["ETHERSCAN_API_BASE"] = blank
    registry = _registry(
        secrets=values if pool == "profile" else {},
        environ=values if pool == "env" else {},
    )
    source = registry.get("etherscan")
    assert source.endpoint == ORIGIN
    assert source.endpoint_provenance["variable"] == "ETHERSCAN_API_BASE"
    assert source.endpoint_provenance["selected_from"] == "default"
    assert source.endpoint_provenance["present"] is True
    assert source.endpoint_provenance["shadowed"] is False
    assert "api.etherscan.io" not in json.dumps(source.endpoint_provenance)
    assert KEY not in json.dumps(source.endpoint_provenance)


def test_explicit_etherscan_base_wins() -> None:
    explicit = "https://etherscan.example.invalid"
    registry = _registry(
        secrets={"ETHERSCAN_API_BASE": explicit},
        environ={"ETHERSCAN_API_BASE": "https://env.example.invalid"},
    )
    assert registry.get("etherscan").endpoint == explicit
    assert registry.get("etherscan").endpoint_provenance["selected_from"] == "profile"
    env_only = _registry(environ={"ETHERSCAN_API_BASE": "https://env.example.invalid"})
    assert env_only.get("etherscan").endpoint == "https://env.example.invalid"
    assert env_only.get("etherscan").endpoint_provenance["selected_from"] == "env"


def test_default_is_only_the_etherscan_v2_base() -> None:
    other = {**SOURCE, "adapter": "blockscout"}
    registry = build_endpoint_registry({"sources": {"etherscan": other}})
    assert registry.get("etherscan").endpoint == ""
    renamed = {**SOURCE, "endpoint_env": "CUSTOM_ETHERSCAN_URL"}
    registry = build_endpoint_registry({"sources": {"etherscan": renamed}})
    assert registry.get("etherscan").endpoint == ""


def test_default_origin_reports_are_secret_free(monkeypatch, capsys) -> None:
    monkeypatch.delenv("ETHERSCAN_API_BASE", raising=False)
    monkeypatch.setenv("ETHERSCAN_API_KEY", KEY)
    document = {
        "sources": {"etherscan": SOURCE},
        "private_inputs": {"secret_profile_file_env": "MONITOR_SECRET_PROFILE"},
    }
    monkeypatch.setattr(
        "guardian.configuration.load_public_config",
        lambda _path: SimpleNamespace(document=document),
    )
    doctor_report = doctor(mcm_config="synthetic.json", environ={"ETHERSCAN_API_KEY": KEY})
    monkeypatch.setattr("guardian.cli._open_mcm", lambda _path: (
        SimpleNamespace(document=document),
        SimpleNamespace(accounts=SimpleNamespace(tenant_id="synthetic")),
        SimpleNamespace(close=lambda: None),
    ))
    monkeypatch.setattr("guardian.cli.stored_coverage", lambda *_args: [])
    coverage, status = _mcm_coverage(argparse.Namespace(mcm_config="synthetic.json"))
    assert status == 0
    reports = [doctor_report, coverage, diagnostic_fields(document, {"ETHERSCAN_API_KEY": KEY})]
    for report in reports:
        provenance = report["endpoints"]["etherscan"]
        assert provenance == {
            "variable": "ETHERSCAN_API_BASE",
            "selected_from": "default",
            "present": True,
            "shadowed": False,
        }
        credential = next(row for row in report["credentials"] if row["variable"] == "ETHERSCAN_API_KEY")
        assert credential["present"] is True
        assert set(credential) == {"variable", "selected_from", "present", "shadowed"}
    captured = capsys.readouterr()
    blob = json.dumps(reports) + captured.out + captured.err
    assert KEY not in blob
    assert "apikey=" not in blob.lower()
    assert ORIGIN not in json.dumps([report["endpoints"] for report in reports])


@pytest.mark.parametrize(
    ("answer", "reason"),
    [(["127.0.0.1"], "private_endpoint"), (["198.18.0.1"], "benchmark_address")],
)
def test_default_origin_keeps_transport_host_checks(answer, reason) -> None:
    registry = _registry()
    backend = FixtureBackend({})
    transport = ReadOnlyTransport(
        registry,
        BudgetBook.from_document(BUDGETS, clock=lambda: 0.0, sleep=lambda _seconds: None),
        backend,
        resolve=lambda _host: list(answer),
    )
    client = EtherscanClient(transport, "etherscan", credential=KEY, credential_env="ETHERSCAN_API_KEY")
    operation = client.operation(
        chain_id=1,
        module="account",
        action="txlist",
        address=ADDRESS,
        start_block=1,
        end_block=2,
        page=1,
        offset=1,
    )
    prepared = transport.encode("etherscan", operation)
    assert prepared.url.startswith(ORIGIN + "/v2/api?")
    assert "apikey=" + KEY in prepared.url
    with pytest.raises(SecurityError, match=reason):
        transport.request("etherscan", operation)
    with pytest.raises(SecurityError):
        transport.request(
            "etherscan",
            Operation(
                "rest",
                "etherscan_v2",
                http_method="POST",
                path="/v2/api",
                query={"action": "eth_sendRawTransaction", "module": "proxy"},
            ),
        )
    assert backend.calls == []


def test_missing_etherscan_key_does_not_call_the_default_origin() -> None:
    document = {
        "sources": {"etherscan": SOURCE},
        "routing": [{
            "network": "polygon-pos",
            "capability": "history.native_external",
            "source_ids": ["etherscan"],
        }],
    }
    status = _source_status(document, {}, {})
    assert status["etherscan"].blocked_reason == "credential_missing"
    assert status["etherscan"].credential_present is False
    registry = build_endpoint_registry(document)
    assert registry.get("etherscan").endpoint == ORIGIN
    backend = FixtureBackend({})
    transport = ReadOnlyTransport(
        registry,
        BudgetBook.from_document(BUDGETS, clock=lambda: 0.0, sleep=lambda _seconds: None),
        backend,
        resolve=lambda _host: ["1.1.1.1"],
    )
    held: set = set()
    scan = SimpleNamespace(
        public_config=SimpleNamespace(document=document),
        transport=transport,
        network="polygon-pos",
        head=10,
        observations={},
        held_cursors=held,
        hold=lambda book, wallet, network, capability: book.add((wallet, network, capability)),
    )
    rows = drive.history_external(
        scan,
        SimpleNamespace(source_status=status),
        "wallet-1",
        ADDRESS,
        None,
        None,
    )
    assert rows == []
    assert backend.calls == []
    observation = scan.observations[("wallet-1", "polygon-pos", "history.native_external")]
    assert "credential_missing" in observation.reason_codes
    assert ("wallet-1", "polygon-pos", "history.native_external") in held


def test_docs_name_the_etherscan_default_origin() -> None:
    free_keys = (ROOT / "docs" / "free-keys.md").read_text(encoding="utf-8")
    api_keys = (ROOT / "docs" / "api-keys.md").read_text(encoding="utf-8")
    unreleased = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8").split("## [0.4.0rc6]", 1)[0]
    for text in (free_keys, api_keys):
        assert "There is no built-in Etherscan origin" not in text
        assert ORIGIN in text
    assert "unset variable skips that source" not in free_keys
    assert "Etherscan still needs `ETHERSCAN_API_BASE` or that source is skipped" not in free_keys
    assert "You must set `ETHERSCAN_API_BASE` or the Etherscan source is skipped" not in api_keys
    assert "credential_missing" in api_keys
    assert ORIGIN in unreleased
    assert "ETHERSCAN_API_BASE" in unreleased
