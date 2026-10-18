"""Base endpoint fallback and value-free reports; synthetic credentials, no network."""

import argparse
import json
import logging
from types import SimpleNamespace

import pytest

from guardian.cli import _mcm_coverage
from guardian.diagnostics import diagnostic_fields
from guardian.errors import SecurityError
from guardian.security.redact import redact
from guardian.security.transport_policy import Operation
from guardian.service import doctor
from guardian.sources.alchemy import AlchemyClient
from guardian.sources.budget import BudgetBook
from guardian.sources.fixtures import RawResponse
from guardian.sources.provenance import CallLedger
from guardian.sources.transport import ReadOnlyTransport, build_endpoint_registry

KEY = "synthetic-alchemy-fallback-key"
URL = "https://base-mainnet.g.alchemy.com/v2/" + KEY
SOURCE = {
    "adapter": "alchemy_transfers",
    "endpoint_env": "ALCHEMY_TRANSFERS_URL",
    "credential_env": "ALCHEMY_API_KEY",
    "budget_group": "alchemy",
}
DOCUMENT = {"sources": {"alchemy-base": SOURCE}}


@pytest.mark.parametrize("blank", [None, "", " \t "])
@pytest.mark.parametrize("pool", ["env", "profile"])
def test_derive_base_endpoint(blank, pool):
    values = {"ALCHEMY_API_KEY": KEY}
    if blank is not None:
        values["ALCHEMY_TRANSFERS_URL"] = blank
    registry = build_endpoint_registry(
        DOCUMENT, secrets=values if pool == "profile" else {},
        environ=values if pool == "env" else {},
    )
    assert registry.get("alchemy-base").endpoint == URL


@pytest.mark.parametrize("profile_url,env_url,expected", [
    ("https://profile.example.invalid/rpc", "https://env.example.invalid/rpc", "https://profile.example.invalid/rpc"),
    (" ", "https://env.example.invalid/rpc", "https://env.example.invalid/rpc"),
])
def test_explicit_endpoint_wins(profile_url, env_url, expected):
    registry = build_endpoint_registry(
        DOCUMENT, secrets={"ALCHEMY_TRANSFERS_URL": profile_url, "ALCHEMY_API_KEY": KEY},
        environ={"ALCHEMY_TRANSFERS_URL": env_url},
    )
    assert registry.get("alchemy-base").endpoint == expected


def test_key_precedence_and_narrow_scope():
    registry = build_endpoint_registry(
        DOCUMENT, secrets={"ALCHEMY_API_KEY": " "}, environ={"ALCHEMY_API_KEY": KEY},
    )
    assert registry.get("alchemy-base").endpoint == URL
    registry = build_endpoint_registry(
        DOCUMENT, secrets={"ALCHEMY_API_KEY": KEY}, environ={"ALCHEMY_API_KEY": "other-synthetic-key"},
    )
    assert registry.get("alchemy-base").endpoint == URL
    for source, env in [
        (SOURCE, {}),
        (SOURCE, {"ALCHEMY_API_KEY": " \t "}),
        ({**SOURCE, "adapter": "evm_rpc"}, {"ALCHEMY_API_KEY": KEY}),
        ({**SOURCE, "endpoint_env": "CUSTOM_URL"}, {"ALCHEMY_API_KEY": KEY}),
    ]:
        registry = build_endpoint_registry({"sources": {"test": source}}, environ=env)
        assert registry.get("test").endpoint == ""


def test_derived_endpoint_reports_and_logs_are_secret_free(monkeypatch, caplog, capsys):
    env = {"ALCHEMY_API_KEY": KEY}
    monkeypatch.setenv("ALCHEMY_API_KEY", KEY)
    registry = build_endpoint_registry(DOCUMENT, environ=env)
    budgets = BudgetBook.from_document({"alchemy": {
        "max_rps": 100, "max_concurrency": 1, "max_requests_per_day": 100, "retry_attempts": 0,
    }}, clock=lambda: 0.0, sleep=lambda _seconds: None)
    calls = []

    class Backend:
        def send(self, call):
            calls.append(call)
            logging.getLogger(__name__).info("request %r", call)
            return RawResponse(200, {"x-ratelimit-remaining": URL}, b'{"result":{"transfers":[]}}')

    transport = ReadOnlyTransport(registry, budgets, Backend(), resolve=lambda _host: ["1.1.1.1"])
    transport.provenance = CallLedger()
    with caplog.at_level(logging.INFO):
        read = AlchemyClient(transport, "alchemy-base", network="base-mainnet").transfers(
            address="0x0000000000000000000000000000000000000001",
            start_block=1, end_block=2, side="from",
        )
        logging.getLogger(__name__).info("sources %r", registry)
    assert read.category == "empty"
    assert calls[0].url == URL
    assert redact(URL) == "https://base-mainnet.g.alchemy.com/v2/***"

    monkeypatch.setattr("guardian.configuration.load_public_config", lambda _path: SimpleNamespace(document=DOCUMENT))
    doctor_report = doctor(mcm_config="synthetic.json", environ=env)
    monkeypatch.setattr("guardian.cli._open_mcm", lambda _path: (
        SimpleNamespace(document=DOCUMENT), SimpleNamespace(accounts=SimpleNamespace(tenant_id="synthetic")),
        SimpleNamespace(close=lambda: None),
    ))
    monkeypatch.setattr("guardian.cli.stored_coverage", lambda *_args: [])
    coverage, status = _mcm_coverage(argparse.Namespace(mcm_config="synthetic.json"))
    assert status == 0
    reports = [doctor_report, coverage, diagnostic_fields(DOCUMENT, env)]
    for report in reports:
        provenance = report["endpoints"]["alchemy-base"]
        assert provenance["variable"] == "ALCHEMY_TRANSFERS_URL"
        assert provenance["selected_from"] == "derived_from_ALCHEMY_API_KEY"
        assert provenance["present"] is True
    ledger = transport.provenance.as_dict()
    assert ledger["providers"]["alchemy-base"]["endpoint"]["selected_from"] == "derived_from_ALCHEMY_API_KEY"
    output = capsys.readouterr()
    blob = json.dumps([*reports, ledger]) + caplog.text + output.out + output.err
    assert URL not in blob
    assert KEY not in blob


def test_derived_endpoint_keeps_transport_guards():
    registry = build_endpoint_registry(DOCUMENT, environ={"ALCHEMY_API_KEY": KEY})
    transport = ReadOnlyTransport(registry, BudgetBook.from_document({}, clock=lambda: 0.0, sleep=lambda _seconds: None), None, resolve=lambda _host: ["127.0.0.1"])
    with pytest.raises(SecurityError, match="private_endpoint"):
        transport.request("alchemy-base", Operation("jsonrpc", "alchemy_transfers", method="alchemy_getAssetTransfers", params=[]))
    with pytest.raises(SecurityError):
        transport.request("alchemy-base", Operation("jsonrpc", "alchemy_transfers", method="eth_sendRawTransaction", params=[]))
