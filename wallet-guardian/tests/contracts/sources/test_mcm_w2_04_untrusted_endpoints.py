"""MCM-W2-04. Endpoints come from the private profile. Redirects and DNS are checked again."""

from __future__ import annotations

import json
from urllib.parse import urljoin

import pytest

from guardian.configuration import load_public_config
from guardian.errors import ConfigError, SecurityError
from guardian.security.transport_policy import Operation
from guardian.sources.fixtures import RawResponse, request_key
from guardian.sources.transport import TransportError, build_endpoint_registry
from support import ROOT, SYNTH, budget, harness, install, json_body, source


def _rpc(**kwargs):
    return harness(
        {"rpc": source("evm_rpc", "RPC_URL", "readers")},
        {"readers": budget(retry_attempts=0)},
        {"RPC_URL": "https://rpc.example.invalid/rpc?apikey=synthetic-secret-value-9f3a7c"},
        **kwargs,
    )


def _read() -> Operation:
    return Operation("jsonrpc", "evm_rpc", method="eth_getBalance", params=[SYNTH, "latest"])


def _redirect(location: str) -> RawResponse:
    return RawResponse(302, {"location": location}, b"")


def test_mcm_w2_04_registered_read_returns_nonzero_and_ignores_response_urls():
    transport, backend, _clock, _sleep = _rpc()
    operation = _read()
    payload = {
        "id": 1,
        "jsonrpc": "2.0",
        "result": "0x3",
        "rpc_url": "https://evil.example/rpc",
    }
    install(transport, "rpc", operation, json_body(payload))
    result = transport.request("rpc", operation)
    assert int(result.body["result"], 16) == 3
    assert transport.endpoints.get("rpc").endpoint == "https://rpc.example.invalid/rpc?apikey=synthetic-secret-value-9f3a7c"
    before = len(backend.calls)
    with pytest.raises(SecurityError) as caught:
        transport.request("rpc", operation, response_metadata=result.body)
    assert caught.value.reason == "untrusted_endpoint"
    assert len(backend.calls) == before
    assert transport.budgets.group("readers").requests_today == 1


def test_mcm_w2_04_task_and_metadata_urls_are_refused():
    transport, backend, _clock, _sleep = _rpc()
    operation = _read()
    payloads = (
        {"url": "https://evil.example/rpc"},
        {"rpc_url": "https://evil.example/rpc"},
        {"endpoint_url": "https://evil.example/rpc"},
        {"nested": {"rpc": "https://evil.example/rpc"}},
        {"note": "https://evil.example/steal"},
    )
    for payload in payloads:
        for channel in ("task", "metadata", "response_metadata"):
            with pytest.raises(SecurityError) as caught:
                transport.request("rpc", operation, **{channel: payload})
            assert caught.value.reason == "untrusted_endpoint"
    with pytest.raises(SecurityError) as caught:
        transport.request(
            "rpc",
            Operation("jsonrpc", "evm_rpc", method="eth_sendRawTransaction", params=["0x01"]),
            task={"url": "https://evil.example/rpc"},
        )
    assert caught.value.reason == "method_denied"
    mismatched = Operation("jsonrpc", "evm_rpc", method="eth_getBalance", params=[SYNTH, "latest"], url="https://evil.example/rpc")
    with pytest.raises(SecurityError) as caught:
        transport.request("rpc", mismatched)
    assert caught.value.reason == "endpoint_mismatch"
    with pytest.raises(SecurityError) as caught:
        transport.request("missing", operation)
    assert caught.value.reason == "unregistered_source"
    assert backend.calls == []
    assert transport.budgets.group("readers").requests_today == 0


def test_mcm_w2_04_redirect_host_is_refused_and_same_host_is_rechecked():
    seen: list[str] = []

    def resolve(host: str) -> list[str]:
        seen.append(host)
        return ["1.1.1.1"]

    transport, backend, _clock, _sleep = _rpc(resolve=resolve)
    operation = _read()
    first = transport.encode("rpc", operation)
    nxt = urljoin(first.url, "/next")
    key = request_key(first.method, first.url, first.body)
    transport.backend.mapping[key] = _redirect("/next")
    transport.backend.mapping[request_key(first.method, nxt, first.body)] = json_body({"id": 1, "jsonrpc": "2.0", "result": "0x4"})
    result = transport.request("rpc", operation)
    assert int(result.body["result"], 16) == 4
    assert len(backend.calls) == 2
    assert seen.count("rpc.example.invalid") >= 2

    seen.clear()
    transport.backend.calls.clear()
    transport.backend.mapping[key] = _redirect("https://evil.example/rpc")
    with pytest.raises(SecurityError) as caught:
        transport.request("rpc", operation)
    assert caught.value.reason == "redirect_host"
    assert len(backend.calls) == 1
    assert "evil.example" not in seen


def test_mcm_w2_04_dns_change_to_private_is_refused():
    for ip in ("127.0.0.1", "10.1.2.3", "169.254.9.9"):
        answers = ["1.1.1.1"]

        def resolve(_host: str, answers=answers) -> list[str]:
            return list(answers)

        transport, backend, _clock, _sleep = _rpc(resolve=resolve)
        operation = _read()
        install(transport, "rpc", operation, json_body({"id": 1, "jsonrpc": "2.0", "result": "0x1"}))
        first = transport.request("rpc", operation)
        assert int(first.body["result"], 16) == 1
        assert len(backend.calls) == 1
        answers[:] = [ip]
        with pytest.raises(SecurityError) as caught:
            transport.request("rpc", operation)
        assert caught.value.reason == "private_endpoint"
        assert len(backend.calls) == 1

    flipped = {"n": 0}

    def flip(_host: str) -> list[str]:
        flipped["n"] += 1
        if flipped["n"] == 1:
            return ["1.1.1.1"]
        return ["10.1.2.3"]

    transport, backend, _clock, _sleep = _rpc(resolve=flip)
    operation = _read()
    first = transport.encode("rpc", operation)
    transport.backend.mapping[request_key(first.method, first.url, first.body)] = _redirect("/next")
    with pytest.raises(SecurityError) as caught:
        transport.request("rpc", operation)
    assert caught.value.reason == "private_endpoint"
    assert len(backend.calls) == 1
    assert flipped["n"] == 2


def test_mcm_w2_04_private_endpoint_needs_a_local_profile():
    sources = {"local-rpc": source("evm_rpc", "RPC_LOCAL_URL", "local")}
    budgets = {"local": budget(retry_attempts=0)}
    secrets = {"RPC_LOCAL_URL": "http://127.0.0.1:8545"}
    transport, backend, _clock, _sleep = harness(sources, budgets, secrets)
    operation = _read()
    with pytest.raises(SecurityError) as caught:
        transport.request("local-rpc", operation)
    assert caught.value.reason == "private_endpoint"
    assert backend.calls == []
    allowed, allowed_backend, _clock, _sleep = harness(
        sources,
        budgets,
        secrets,
        local_profiles={"local-rpc": {"allow_private_network": True}},
    )
    install(allowed, "local-rpc", operation, json_body({"id": 1, "jsonrpc": "2.0", "result": "0x1"}))
    result = allowed.request("local-rpc", operation)
    assert int(result.body["result"], 16) == 1
    assert len(allowed_backend.calls) == 1
    stringly, string_backend, _clock, _sleep = harness(
        sources,
        budgets,
        secrets,
        local_profiles={"local-rpc": {"allow_private_network": "true"}},
    )
    with pytest.raises(SecurityError) as caught:
        stringly.request("local-rpc", operation)
    assert caught.value.reason == "private_endpoint"
    assert string_backend.calls == []
    metadata, meta_backend, _clock, _sleep = harness(
        {"meta": source("evm_rpc", "META_URL", "local")},
        budgets,
        {"META_URL": "http://169.254.169.254/latest"},
        local_profiles={"meta": {"allow_private_network": True}},
    )
    with pytest.raises(SecurityError) as caught:
        metadata.request("meta", operation)
    assert caught.value.reason == "private_endpoint"
    assert meta_backend.calls == []


def test_mcm_w2_04_public_config_cannot_set_the_local_flag(tmp_path):
    document = json.loads((ROOT / "examples" / "monitor.json").read_text(encoding="utf-8"))
    document["sources"]["rpc-ethereum-mainnet"]["allow_private_network"] = True
    path = tmp_path / "monitor.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(ConfigError):
        load_public_config(path)
    with pytest.raises(SecurityError) as caught:
        build_endpoint_registry(document, secrets={})
    assert caught.value.reason == "public_local_profile"


def test_mcm_w2_04_secrets_are_not_in_exceptions():
    transport, backend, _clock, _sleep = _rpc()
    secret = "synthetic-secret-value-9f3a7c"
    with pytest.raises(TransportError) as caught:
        transport.request("rpc", _read())
    assert caught.value.reason == "fixture_miss"
    rendered = str(caught.value) + repr(caught.value) + repr(backend.calls[-1])
    assert secret not in rendered
    assert "apikey=***" in repr(backend.calls[-1])
