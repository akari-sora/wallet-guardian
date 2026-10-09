"""MCM-SHARE-01 S1. Base Alchemy transfers and chunked logs, through the real CLI.

Sockets stay blocked. Addresses and keys in this file are synthetic.
"""

from __future__ import annotations

import importlib.util
import json
import socket
from pathlib import Path

import httpx
import pytest

from guardian.chains.evm.accounting import TRANSFER_TOPIC
from guardian.chains.evm.permissions import APPROVAL_FOR_ALL_TOPIC, ERC20_APPROVAL_TOPIC
from guardian.errors import SecurityError
from guardian.security.redact import redact
from guardian.security.transport_policy import Operation, assert_operation_allowed
from guardian.sources.fixtures import RawResponse

_SUPPORT = Path(__file__).with_name("test_mcm_free_tier_catchup.py")
_SPEC = importlib.util.spec_from_file_location("mcm_s1_free_tier_support", _SUPPORT)
assert _SPEC is not None and _SPEC.loader is not None
_free = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_free)

ADDRESS_A = _free.ADDRESS_A
LARGE = _free.LARGE
OTHER = _free.OTHER
WALLET_A = _free.WALLET_A
FixtureBackend = _free.FixtureBackend

ALCHEMY_KEY = "syntheticAlchemyKey99"
ALCHEMY_URL = f"https://alchemy.example.invalid/v2/{ALCHEMY_KEY}"
TENDERLY_URL = "https://tenderly.example.invalid/rpc"
ONE_RPC_URL = "https://base-1rpc.example.invalid/rpc"
TENDERLY_HOST = "tenderly.example.invalid"
ONE_RPC_HOST = "base-1rpc.example.invalid"
HEAD_SHORT = 20
HEAD_LONG = 2500


@pytest.fixture(autouse=True)
def _block_network(monkeypatch: pytest.MonkeyPatch) -> None:
    def blocked(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("network is disabled")

    monkeypatch.setattr(socket, "socket", blocked)
    monkeypatch.setattr(socket, "create_connection", blocked)
    monkeypatch.setattr(socket, "getaddrinfo", blocked)
    monkeypatch.setattr(httpx, "Client", blocked)


class ProfileBackend(FixtureBackend):
    """Alchemy paging plus optional log-host failures. Still fully offline."""

    def __init__(self) -> None:
        super().__init__()
        self.alchemy_seen = {"from": 0, "to": 0}
        self.alchemy_status = 0
        self.alchemy_retries_left = 0
        self.fail_log_hosts: set[str] = set()

    def _rpc_response(self, host: str, payload: dict) -> RawResponse:
        method = str(payload.get("method") or "")
        params = payload.get("params") if isinstance(payload.get("params"), list) else []
        if method == "alchemy_getAssetTransfers":
            return self._alchemy(params)
        return super()._rpc_response(host, payload)

    def _logs(self, host: str, params: list) -> object | None:
        if host in self.fail_log_hosts:
            filt = params[0] if params and isinstance(params[0], dict) else {}
            start = int(str(filt.get("fromBlock", "0x0")), 16)
            end = int(str(filt.get("toBlock", "0x0")), 16)
            topics = filt.get("topics") if isinstance(filt.get("topics"), list) else []
            address = filt.get("address")
            self.calls.append({
                "address": address.lower() if isinstance(address, str) else None,
                "from": start,
                "host": host,
                "kind": "log",
                "to": end,
                "topics": topics,
            })
            return None
        return super()._logs(host, params)

    def _alchemy(self, params: list) -> RawResponse:
        item = params[0] if params and isinstance(params[0], dict) else {}
        self.calls.append({"kind": "alchemy", "params": dict(item)})
        if self.alchemy_status == 429 and self.alchemy_retries_left > 0:
            self.alchemy_retries_left -= 1
            return RawResponse(429, {"retry-after": "2"}, b"{}")
        if self.alchemy_status in {401, 402, 403}:
            body = {"error": {"code": self.alchemy_status, "message": "unauthorized"}}
            return RawResponse(self.alchemy_status, {}, json.dumps(body).encode())
        side = "from" if "fromAddress" in item else "to"
        seen = self.alchemy_seen.get(side, 0)
        self.alchemy_seen[side] = seen + 1
        if seen == 0:
            result = {"pageKey": "page-2", "transfers": [_alchemy_transfer(side)]}
        else:
            result = {"transfers": []}
        return RawResponse(200, {}, json.dumps({"id": 1, "jsonrpc": "2.0", "result": result}).encode())


def _alchemy_transfer(side: str) -> dict:
    incoming = side == "to"
    return {
        "blockNum": "0x5",
        "category": "external",
        "from": OTHER if incoming else ADDRESS_A,
        "hash": _free._tx(11 if incoming else 12),
        "rawContract": {"value": hex(LARGE)},
        "to": ADDRESS_A if incoming else OTHER,
    }


def _base_desk(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, secrets_extra=None, omit_secrets=()):
    return _free._desk(
        tmp_path,
        monkeypatch,
        [_free._account(WALLET_A, ADDRESS_A, ("base-mainnet",))],
        secrets_extra=secrets_extra,
        omit_secrets=omit_secrets,
    )


def _use(desk, *, head: int) -> ProfileBackend:
    backend = ProfileBackend()
    backend.head = head
    desk.backend = backend
    return backend


def _enable_one_rpc(path: Path, daily: int) -> None:
    document = json.loads(path.read_text(encoding="utf-8"))
    document["sources"]["base-1rpc"]["enabled"] = True
    document["budgets"]["base-1rpc"]["max_requests_per_day"] = daily
    path.write_text(json.dumps(document), encoding="utf-8")


def _required(report: dict, capability: str) -> str:
    found = [key for key in report["required"] if key.endswith(f"|base-mainnet|{capability}")]
    assert found, capability
    return report["required"][found[0]]


def _assert_base_stays_required(report: dict, config: Path) -> None:
    document = json.loads(config.read_text(encoding="utf-8"))
    assert "base-mainnet" in document["enabled_networks"]
    assert _required(report, "balances.native")
    assert _required(report, "history.native_external")
    row = _free._coverage(report, WALLET_A, "base-mainnet", "history.native_external")
    assert "expansion_pending" not in row["reason_codes"]


def _log_calls(backend, host: str | None = None) -> list[dict]:
    return [
        row for row in backend.calls
        if row.get("kind") == "log" and (host is None or row.get("host") == host)
    ]


def _assert_not_emitter(calls: list[dict]) -> None:
    for row in calls:
        address = row.get("address")
        assert address is None or address != ADDRESS_A.lower()


def _topic_blob(calls: list[dict]) -> set[str]:
    found: set[str] = set()
    for row in calls:
        for topic in row.get("topics") or []:
            if isinstance(topic, list):
                found.update(str(item).lower() for item in topic)
            elif topic is not None:
                found.add(str(topic).lower())
    return found


def _assert_spans(calls: list[dict], *, limit: int, head: int) -> None:
    spans = sorted({(row["from"], row["to"]) for row in calls})
    assert spans
    assert all(end - start <= limit for start, end in spans)
    assert spans[0][0] == 0
    previous = -1
    for start, end in spans:
        assert start <= previous + 1
        previous = max(previous, end)
    assert previous == head


def test_mcm_share_s1_01_alchemy_transfers_both_directions_paging(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    """S1-01. from and to are separate, pageKey is exhausted, and a net-zero pair stays visible."""
    desk = _base_desk(
        tmp_path,
        monkeypatch,
        secrets_extra={"ALCHEMY_API_KEY": ALCHEMY_KEY, "ALCHEMY_TRANSFERS_URL": ALCHEMY_URL},
    )
    backend = _use(desk, head=HEAD_SHORT)
    report = _free._cli(capsys, desk)
    alchemy = [row for row in backend.calls if row.get("kind") == "alchemy"]
    assert alchemy
    sides = set()
    for row in alchemy:
        params = row["params"]
        has_from = "fromAddress" in params
        has_to = "toAddress" in params
        assert has_from ^ has_to
        sides.add("from" if has_from else "to")
        assert params["category"] == ["external", "erc20", "erc721", "erc1155"]
        assert "internal" not in params["category"]
        assert params["excludeZeroValue"] is False
        assert params["order"] == "asc"
        assert params["maxCount"] == "0x3e8"
        assert params["fromBlock"] != "latest"
        assert params["toBlock"] != "latest"
        assert int(params["fromBlock"], 16) == 0
        assert int(params["toBlock"], 16) == HEAD_SHORT
    assert sides == {"from", "to"}
    for side, key in (("from", "fromAddress"), ("to", "toAddress")):
        rows = [row for row in alchemy if key in row["params"]]
        assert len(rows) == 2
        assert "pageKey" not in rows[0]["params"]
        assert rows[1]["params"]["pageKey"] == "page-2"
        assert side in {"from", "to"}
    events = [
        row for row in _free._events(desk, WALLET_A, "base-mainnet")
        if row["kind"] in {"asset_in", "asset_out"}
    ]
    assert {row["kind"] for row in events} == {"asset_in", "asset_out"}
    assert {int(row["raw_amount"]) for row in events} == {LARGE}
    signed = sum(int(row["raw_amount"]) if row["kind"] == "asset_in" else -int(row["raw_amount"]) for row in events)
    assert signed == 0
    native = next(row for row in report["native_reads"] if row["network"] == "base-mainnet")
    assert native["raw_amount"] == "0"
    history = _free._coverage(report, WALLET_A, "base-mainnet", "history.native_external")
    assert history["quality"] == "complete_in_declared_scope"
    assert "alchemy-base" in history["sources"]


def test_mcm_share_s1_02_base_logs_chunked_1000(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    """S1-02. Topic-only Base logs stay within range_limit and a funded fallback can reach the target."""
    secrets = {"TENDERLY_BASE_URL": TENDERLY_URL, "BASE_1RPC_URL": ONE_RPC_URL}

    primary = _base_desk(tmp_path / "primary", monkeypatch, secrets_extra=secrets)
    primary_backend = _use(primary, head=HEAD_LONG)
    primary_backend.logs = [
        {**_free._transfer_log(_free._tx(21), 1, 0, ADDRESS_A), "host": TENDERLY_HOST},
        {**_free._approval_log(_free._tx(22), 1, ADDRESS_A), "host": TENDERLY_HOST},
    ]
    primary_report = _free._cli(capsys, primary)
    tenderly_logs = _log_calls(primary_backend, TENDERLY_HOST)
    _assert_not_emitter(tenderly_logs)
    _assert_spans(tenderly_logs, limit=1000, head=HEAD_LONG)
    topics = _topic_blob(tenderly_logs)
    assert TRANSFER_TOPIC.lower() in topics
    assert ERC20_APPROVAL_TOPIC.lower() in topics
    assert APPROVAL_FOR_ALL_TOPIC.lower() in topics
    assert not _log_calls(primary_backend, ONE_RPC_HOST)
    events = _free._events(primary, WALLET_A, "base-mainnet")
    assert any(row["kind"] in {"asset_in", "asset_out"} and int(row["raw_amount"]) > 0 for row in events)
    assert any(row["kind"] == "approval_set" and int(row["raw_amount"]) > 0 for row in events)
    for capability in ("history.fungible", "permissions.erc20"):
        row = _free._coverage(primary_report, WALLET_A, "base-mainnet", capability)
        assert row["quality"] == "complete_in_declared_scope"
        assert "unread_gap" not in row["reason_codes"]
        assert not any(str(code).startswith("single_block_unreadable:") for code in row["reason_codes"])
        assert _free._cursor(primary, WALLET_A, "base-mainnet", capability) == str(HEAD_LONG)

    starved = _base_desk(tmp_path / "starved", monkeypatch, secrets_extra=secrets)
    _enable_one_rpc(starved.config, 1)
    starved_backend = _use(starved, head=HEAD_LONG)
    starved_backend.fail_log_hosts.add(TENDERLY_HOST)
    starved_report = _free._cli(capsys, starved)
    assert _log_calls(starved_backend, TENDERLY_HOST)
    assert not _log_calls(starved_backend, ONE_RPC_HOST)
    _assert_not_emitter(_log_calls(starved_backend))
    starved_row = _free._coverage(starved_report, WALLET_A, "base-mainnet", "history.fungible")
    assert starved_row["quality"] != "complete_in_declared_scope"
    assert starved_row["quality"] != "empty"

    caught = _base_desk(tmp_path / "caught", monkeypatch, secrets_extra=secrets)
    _enable_one_rpc(caught.config, 10000)
    caught_backend = _use(caught, head=HEAD_LONG)
    caught_backend.fail_log_hosts.add(TENDERLY_HOST)
    caught_backend.logs = [
        {**_free._transfer_log(_free._tx(23), 1, 0, ADDRESS_A), "host": ONE_RPC_HOST},
        {**_free._approval_log(_free._tx(24), 1, ADDRESS_A), "host": ONE_RPC_HOST},
    ]
    caught_report = _free._cli(capsys, caught)
    assert _log_calls(caught_backend, TENDERLY_HOST)
    fallback_logs = _log_calls(caught_backend, ONE_RPC_HOST)
    _assert_not_emitter(_log_calls(caught_backend))
    _assert_spans(fallback_logs, limit=50, head=HEAD_LONG)
    caught_events = _free._events(caught, WALLET_A, "base-mainnet")
    assert any(row["kind"] in {"asset_in", "asset_out"} and int(row["raw_amount"]) > 0 for row in caught_events)
    for capability in ("history.fungible", "permissions.erc20"):
        assert _free._coverage(caught_report, WALLET_A, "base-mainnet", capability)["quality"] == "complete_in_declared_scope"
        assert _free._cursor(caught, WALLET_A, "base-mainnet", capability) == str(HEAD_LONG)


def test_mcm_share_s1_03_transfers_not_approval(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    """S1-03. Alchemy transfers stay visible and do not complete Approval coverage."""
    desk = _base_desk(
        tmp_path,
        monkeypatch,
        secrets_extra={
            "ALCHEMY_API_KEY": ALCHEMY_KEY,
            "ALCHEMY_TRANSFERS_URL": ALCHEMY_URL,
            "BASE_1RPC_URL": ONE_RPC_URL,
            "TENDERLY_BASE_URL": TENDERLY_URL,
        },
    )
    backend = _use(desk, head=HEAD_SHORT)
    backend.fail_log_hosts.add(TENDERLY_HOST)
    report = _free._cli(capsys, desk)
    events = _free._events(desk, WALLET_A, "base-mainnet")
    assets = [row for row in events if row["kind"] in {"asset_in", "asset_out"}]
    assert {row["kind"] for row in assets} == {"asset_in", "asset_out"}
    assert all(int(row["raw_amount"]) > 0 for row in assets)
    assert not [row for row in events if row["kind"] == "approval_set"]
    history = _free._coverage(report, WALLET_A, "base-mainnet", "history.native_external")
    assert history["quality"] == "complete_in_declared_scope"
    assert "alchemy-base" in history["sources"]
    for capability in (
        "permissions.erc1155_operator",
        "permissions.erc20",
        "permissions.erc721_operator",
        "permissions.erc721_token",
    ):
        row = _free._coverage(report, WALLET_A, "base-mainnet", capability)
        assert row["quality"] != "complete_in_declared_scope"
        assert "alchemy-base" not in row["sources"]
    assert not _log_calls(backend, ONE_RPC_HOST)
    with pytest.raises(SecurityError):
        assert_operation_allowed(Operation("jsonrpc", "alchemy_transfers", method="eth_getLogs", params=[{}]))
    assert_operation_allowed(
        Operation("jsonrpc", "alchemy_transfers", method="alchemy_getAssetTransfers", params=[{"category": ["external"]}]),
    )


def test_mcm_share_s1_04_gaps_preserved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    """S1-04. A missing key, a 402, and a challenge stay named. Base stays required."""
    missing = _base_desk(
        tmp_path / "missing",
        monkeypatch,
        secrets_extra={"ALCHEMY_TRANSFERS_URL": ALCHEMY_URL},
        omit_secrets=("ALCHEMY_API_KEY", "BLOCKSCOUT_BASE_API_KEY", "BLOCKSCOUT_BASE_URL"),
    )
    missing_report = _free._cli(capsys, missing)
    missing_row = _free._coverage(missing_report, WALLET_A, "base-mainnet", "history.native_external")
    assert "credential_missing" in missing_row["reason_codes"]
    assert missing_row["quality"] != "complete_in_declared_scope"
    assert missing_row["quality"] != "empty"
    assert "ALCHEMY_API_KEY" in missing_row["credential_hint"]
    assert "docs/api-keys.md#alchemy" in missing_row["credential_hint"]
    assert _required(missing_report, "history.native_external") == "blocked"
    _assert_base_stays_required(missing_report, missing.config)
    assert not [row for row in missing.backend.http_log if "alchemy.example.invalid" in row["url"]]

    featured = _base_desk(
        tmp_path / "featured",
        monkeypatch,
        secrets_extra={
            "BLOCKSCOUT_API_KEY": _free.BLOCKSCOUT_KEY,
            "BLOCKSCOUT_BASE_PUBLIC_URL": "https://base-public.example.invalid",
            "BLOCKSCOUT_PRO_API_BASE": "https://pro.example.invalid",
        },
    )
    featured.backend.pro_featured_chains.add("8453")
    featured.backend.rest_rows[("base-public.example.invalid", "txlist")] = [
        _free._rest_transfer(_free._tx(70), ADDRESS_A, OTHER, 4),
    ]
    featured_report = _free._cli(capsys, featured)
    featured_events = [
        row for row in _free._events(featured, WALLET_A, "base-mainnet")
        if row["kind"] in {"asset_in", "asset_out"}
    ]
    assert featured_events
    assert any(int(row["raw_amount"]) > 0 for row in featured_events)
    cache = json.loads((featured.state / "capability-failures.json").read_text(encoding="utf-8"))
    assert cache["blockscout-pro|base-mainnet"] == "plan_not_supported"
    pro_before = [row for row in featured.backend.calls if row.get("host") == "pro.example.invalid"]
    assert pro_before
    assert len(pro_before) <= 4
    _free._cli(capsys, featured)
    pro_after = [row for row in featured.backend.calls if row.get("host") == "pro.example.invalid"]
    assert len(pro_after) == len(pro_before)
    assert _free._coverage(featured_report, WALLET_A, "base-mainnet", "history.native_external")["quality"] == "complete_in_declared_scope"
    _assert_base_stays_required(featured_report, featured.config)

    challenged = _base_desk(
        tmp_path / "challenge",
        monkeypatch,
        secrets_extra={"BLOCKSCOUT_BASE_PUBLIC_URL": "https://base-public.example.invalid"},
        omit_secrets=("BLOCKSCOUT_BASE_API_KEY", "BLOCKSCOUT_BASE_URL"),
    )
    challenged.backend.cloudflare_hosts.add("base-public.example.invalid")
    challenged_report = _free._cli(capsys, challenged)
    challenged_row = _free._coverage(challenged_report, WALLET_A, "base-mainnet", "history.native_external")
    assert "bot_challenge" in challenged_row["reason_codes"]
    assert "blocked" in challenged_row["reason_codes"]
    assert challenged_row["quality"] != "complete_in_declared_scope"
    assert challenged_row["quality"] != "empty"
    assert not _free._events(challenged, WALLET_A, "base-mainnet")
    challenge_calls = [row for row in challenged.backend.calls if row.get("host") == "base-public.example.invalid"]
    assert challenge_calls
    assert len(challenge_calls) <= 2
    _assert_base_stays_required(challenged_report, challenged.config)

    denied = _base_desk(
        tmp_path / "denied",
        monkeypatch,
        secrets_extra={"ALCHEMY_API_KEY": ALCHEMY_KEY, "ALCHEMY_TRANSFERS_URL": ALCHEMY_URL},
        omit_secrets=("BLOCKSCOUT_BASE_API_KEY", "BLOCKSCOUT_BASE_URL"),
    )
    denied_backend = _use(denied, head=HEAD_SHORT)
    denied_backend.alchemy_status = 401
    denied_report = _free._cli(capsys, denied)
    denied_row = _free._coverage(denied_report, WALLET_A, "base-mainnet", "history.native_external")
    assert "unauthorized" in denied_row["reason_codes"]
    assert "blocked" in denied_row["reason_codes"]
    assert denied_row["quality"] != "complete_in_declared_scope"
    assert denied_row["quality"] != "empty"
    assert not [
        row for row in _free._events(denied, WALLET_A, "base-mainnet")
        if row["kind"] in {"asset_in", "asset_out"}
    ]
    assert [row for row in denied_backend.calls if row.get("kind") == "alchemy"]
    assert len([row for row in denied_backend.calls if row.get("kind") == "alchemy"]) <= 2
    _assert_base_stays_required(denied_report, denied.config)


def test_mcm_share_s1_05_provenance_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    """S1-05. CU estimates, pages, and 429 backoff are visible. The path key is not."""
    desk = _base_desk(
        tmp_path,
        monkeypatch,
        secrets_extra={
            "ALCHEMY_API_KEY": ALCHEMY_KEY,
            "ALCHEMY_TRANSFERS_URL": ALCHEMY_URL,
            "TENDERLY_BASE_URL": TENDERLY_URL,
        },
    )
    backend = _use(desk, head=HEAD_SHORT)
    backend.alchemy_status = 429
    backend.alchemy_retries_left = 1
    backend.logs = [{**_free._transfer_log(_free._tx(31), 1, 0, ADDRESS_A), "host": TENDERLY_HOST}]
    report = _free._cli(capsys, desk)
    provider = report["budget"]["providers"]["alchemy-base"]
    assert provider["credits_basis"] == "estimate"
    assert provider["logical_methods"] >= 2
    assert provider["credits"] == 120 * provider["logical_methods"]
    assert provider["pages"] >= 2
    assert provider["methods"]["alchemy_getAssetTransfers"] == provider["logical_methods"]
    waits = [item for item in report["budget"]["backoff"] if item["source_id"] == "alchemy-base"]
    assert waits
    assert any(item["seconds"] >= 2 for item in waits)
    others = {
        name: row for name, row in report["budget"]["providers"].items() if name != "alchemy-base"
    }
    assert others
    assert any(row["logical_methods"] > 0 and row["credits"] >= row["logical_methods"] for row in others.values())
    assert any(row.get("credits_basis") == "count" for row in others.values())
    blob = json.dumps(report)
    assert ALCHEMY_KEY not in blob
    assert any(ALCHEMY_KEY in row["url"] for row in backend.http_log)
    masked = redact("https://base-mainnet.g.alchemy.com/v2/syntheticAlchemyKey99")
    assert ALCHEMY_KEY not in masked
    assert masked.endswith("/v2/***")
