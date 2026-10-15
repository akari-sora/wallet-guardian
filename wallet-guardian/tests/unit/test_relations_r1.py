"""R-1 counterparty reads. Synthetic addresses, fixture transport, no live network."""

from __future__ import annotations

import json
import os
import stat
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

from guardian.cli import main
from guardian.errors import SecurityError
from guardian.identities import parse_address
from guardian.service_install import CommandResult, execute_install_service
from guardian.sources.blockscout import BlockscoutClient
from guardian.sources.budget import BudgetExhausted
from guardian.sources.fixtures import FixtureFailure, FixtureMiss, RawResponse

CLOCK = 1_700_000_000.0
STAMP = 1_699_136_000
KEY = "synthetic-etherscan-key-r1"
_NEEDLES = (
    "per" + "son",
    "peo" + "ple",
    "individ" + "ual",
    "cowor" + "ker",
    "colle" + "ague",
    "employ" + "ee",
    "ident" + "ity",
    chr(0x4EBA),
    chr(0x540C) + chr(0x4E8B),
    chr(0x517C) + chr(0x8077),
    chr(0x8EAB) + chr(0x4EFD),
    chr(0x500B) + chr(0x4EBA),
)


def evm(number: int) -> str:
    return parse_address("evm", f"0x{number:040x}").canonical


def tx(number: int) -> str:
    return "0x" + f"{number:064x}"


def iso(stamp: int) -> str:
    return datetime.fromtimestamp(stamp, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


SEED = evm(1)
OTHER = evm(2)
LEFT = evm(3)
RIGHT = evm(4)
PAYER = evm(5)
TOKEN = evm(6)
TRON_PAYER = "TA4Wt1DUCqz6YegbnsmqsWC5uUfbdBqPxm"
TRON_SEED = "TA9pkx4DFxrEw8JZzUtyDrh2uAat1LDuJL"
TRON_TOKEN = "TAF8dttxK5iPKbvYC626aDBytrWANpLRXp"
TRON_DEST = "TALSWqjhNCaXi5YWPh9DvZgvtYRSfqVUwq"
SOL = "11111111111111111111111111111111"
BITCOIN = "bc1qw508d6qejxtdg4y5r3zarvary0c5xw7kv8f3t4"
LISTA = "0x1adb950d8bb3da4be104211d5ab038628e477fe6"
ENV_KEY = "env-only-etherscan-key-r1"


class PathBackend:
    def __init__(self) -> None:
        self.calls = []
        self._routes: list[tuple] = []

    def add(self, predicate, response) -> None:
        self._routes.append((predicate, response))

    def send(self, call):
        self.calls.append(call)
        for predicate, response in self._routes:
            if not predicate(call):
                continue
            chosen = response
            if isinstance(response, list):
                if not response:
                    raise FixtureMiss()
                chosen = response.pop(0)
            if isinstance(chosen, (SecurityError, BudgetExhausted)):
                raise chosen
            if isinstance(chosen, FixtureFailure):
                if chosen.reason == "connection_error":
                    raise ConnectionError("connection_error")
                raise FixtureMiss()
            return chosen
        raise FixtureMiss()


def raw(payload: dict, status: int = 200) -> RawResponse:
    return RawResponse(status, {"content-type": "application/json"}, json.dumps(payload).encode())


def scout(items: list, nxt=None) -> RawResponse:
    return raw({"items": items, "next_page_params": nxt})


def empty_scout() -> RawResponse:
    return scout([])


def challenge() -> RawResponse:
    return RawResponse(403, {"content-type": "text/html"}, b"<html>Just a moment...</html>")


def es_ok(rows: list) -> RawResponse:
    return raw({"status": "1", "message": "OK", "result": rows})


def es_empty() -> RawResponse:
    return raw({"status": "0", "message": "No transactions found", "result": []})


def tail_is(name: str):
    def predicate(call) -> bool:
        return urlsplit(call.url).path.rstrip("/").endswith("/" + name)

    return predicate


def action_is(name: str):
    def predicate(call) -> bool:
        return parse_qs(urlsplit(call.url).query).get("action") == [name]

    return predicate


def native_item(sender: str, recipient: str, value: int, stamp: int, digest: str, *, contract: bool | None = False) -> dict:
    party = {"hash": sender}
    if contract is not None:
        party["is_contract"] = contract
    return {
        "block_number": 20,
        "from": party,
        "hash": digest,
        "status": "ok",
        "timestamp": iso(stamp),
        "to": {"hash": recipient},
        "value": str(value),
    }


def token_item(
    sender: str,
    recipient: str,
    value: int,
    stamp: int,
    digest: str,
    *,
    contract: bool | None = False,
    token: str = TOKEN,
) -> dict:
    party = {"hash": sender}
    if contract is not None:
        party["is_contract"] = contract
    return {
        "block_number": 21,
        "from": party,
        "status": "ok",
        "timestamp": iso(stamp),
        "to": {"hash": recipient},
        "token": {"address": token, "decimals": "6", "symbol": "SYN", "type": "ERC-20"},
        "total": {"decimals": "6", "value": str(value)},
        "tx_hash": digest,
    }


def es_token(sender: str, recipient: str, value: int, stamp: int, digest: str, contract: str = TOKEN) -> dict:
    return {
        "blockNumber": "31",
        "contractAddress": contract,
        "from": sender,
        "hash": digest,
        "timeStamp": str(stamp),
        "to": recipient,
        "tokenDecimal": "6",
        "tokenSymbol": "SYN",
        "value": str(value),
    }


def es_native(sender: str, recipient: str, value: int, stamp: int, digest: str) -> dict:
    return {
        "blockNumber": "30",
        "from": sender,
        "hash": digest,
        "isError": "0",
        "timeStamp": str(stamp),
        "to": recipient,
        "value": str(value),
    }


def _private(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(text)
    if os.name == "posix":
        os.chmod(path, 0o600)


def _budget(name: str, daily: int = 10000) -> dict:
    return {"max_concurrency": 1, "max_requests_per_day": daily, "max_rps": 1000, "retry_attempts": 0}


def _source(adapter: str, env: str, group: str, credential: str | None = None) -> dict:
    row = {"adapter": adapter, "budget_group": group, "enabled": True, "endpoint_env": env}
    if credential:
        row["credential_env"] = credential
    return row


def install(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    networks: list[str],
    sources: dict,
    routes: list[dict],
    secrets: dict,
    address: str,
    family: str,
    account_networks: list[str],
    daily: int = 10000,
    extra_accounts: list[dict] | None = None,
) -> PathBackend:
    accounts = tmp_path / "accounts.json"
    profile = tmp_path / "secrets.json"
    state = tmp_path / "state"
    config = tmp_path / "monitor.json"
    state.mkdir(mode=0o700, exist_ok=True)
    if os.name == "posix":
        os.chmod(state, 0o700)
    account_rows = [{
        "wallet_id": "11111111-1111-4111-8111-111111111111",
        "label": "desk",
        "family": family,
        "address": address,
        "networks": account_networks,
        "relationship": "watch_only",
    }]
    if extra_accounts:
        account_rows.extend(extra_accounts)
    _private(accounts, json.dumps({
        "schema_version": "guardian.accounts.v1",
        "tenant_id": "local",
        "accounts": account_rows,
    }))
    _private(profile, json.dumps(secrets))
    budgets = {source["budget_group"]: _budget(source["budget_group"], daily) for source in sources.values()}
    config.write_text(json.dumps({
        "schema_version": "guardian.monitor.v1",
        "mode": "read_only",
        "enabled_networks": networks,
        "private_inputs": {
            "accounts_file_env": "MONITOR_ACCOUNTS_FILE",
            "state_dir_env": "MONITOR_STATE_DIR",
            "secret_profile_file_env": "MONITOR_SECRET_PROFILE",
        },
        "polling": {
            "interval_seconds": 10800,
            "around_the_clock": True,
            "discovery_interval_seconds": 86400,
            "bootstrap_lookback_days": 30,
            "bootstrap_request_cap": 2000,
            "webhooks_enabled": False,
        },
        "alerts": {
            "native_eth_threshold": "0.5",
            "asset_usd_threshold": "1000",
            "threshold_operator": "any",
            "permission_changes": True,
            "exposure_risk_changes": True,
            "exposure_news_only": True,
            "generic_news": False,
            "unknown_price_policy": "show_unknown_not_zero",
            "baseline_policy": "initial_summary_and_existing_risk_not_new_history",
            "quiet_if_no_change": True,
            "monitor_health_changes": True,
        },
        "sources": sources,
        "routing": routes,
        "budgets": budgets,
        "plugins": [],
        "sanctions_lists": [],
        "privacy": {
            "upload_state": False,
            "third_party_wallet_queries": "deny_until_local_consent",
            "public_report_mode": "aggregate_no_wallet_identifiers",
        },
        "delivery": {"mode": "outbox_only", "profile_env": None},
        "execution": {"sign": False, "broadcast": False, "auto_purchase": False, "auto_disable_schedule": False},
    }), encoding="utf-8")
    monkeypatch.setenv("MONITOR_ACCOUNTS_FILE", str(accounts))
    monkeypatch.setenv("MONITOR_STATE_DIR", str(state))
    monkeypatch.setenv("MONITOR_SECRET_PROFILE", str(profile))
    return PathBackend()


def etherscan_profile(network: str = "ethereum-mainnet") -> tuple[dict, list[dict], dict]:
    sources = {"etherscan": _source("etherscan_v2", "ETHERSCAN_API_BASE", "etherscan-account", "ETHERSCAN_API_KEY")}
    routes = [
        {"network": network, "capability": name, "source_ids": ["etherscan"]}
        for name in ("history.native_external", "history.native_internal", "history.fungible", "history.token")
    ]
    return sources, routes, {"ETHERSCAN_API_BASE": "https://etherscan.example.invalid", "ETHERSCAN_API_KEY": KEY}


def blockscout_profile(network: str, source_id: str, env: str, url: str) -> tuple[dict, list[dict], dict]:
    sources = {source_id: _source("blockscout", env, "blockscout-public")}
    routes = [
        {"network": network, "capability": name, "source_ids": [source_id]}
        for name in ("history.native_external", "history.native_internal", "history.fungible", "history.token")
    ]
    return sources, routes, {env: url}


def fill_tails(backend: PathBackend, transfers: list, transactions: list) -> None:
    backend.add(tail_is("token-transfers"), scout(transfers))
    backend.add(tail_is("transactions"), scout(transactions))
    backend.add(tail_is("internal-transactions"), empty_scout())


def invoke(capsys: pytest.CaptureFixture[str], argv: list[str], backend: PathBackend) -> tuple[int, dict]:
    code = main(argv, backend=backend, clock=lambda: CLOCK)
    body = json.loads(capsys.readouterr().out)
    rendered = json.dumps(body, ensure_ascii=False).lower()
    for needle in _NEEDLES:
        assert needle.lower() not in rendered
    assert KEY not in rendered
    return code, body


def inbound_argv(config: Path, *extra: str) -> list[str]:
    return ["relations", "inbound", "--mcm-config", str(config), *extra]


def expand_argv(config: Path, *extra: str) -> list[str]:
    return ["relations", "expand", "--mcm-config", str(config), *extra]


def _modes(path: Path) -> tuple[int, int]:
    return stat.S_IMODE(path.stat().st_mode), stat.S_IMODE(path.parent.stat().st_mode)


def test_inbound_groups_two_counterparties(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    sources, routes, secrets = blockscout_profile(
        "ethereum-mainnet", "blockscout-eth", "BLOCKSCOUT_ETH_URL", "https://eth.blockscout.example.invalid",
    )
    backend = install(
        tmp_path, monkeypatch, networks=["ethereum-mainnet"], sources=sources, routes=routes, secrets=secrets,
        address=SEED, family="evm", account_networks=["ethereum-mainnet"],
    )
    fill_tails(backend, [token_item(OTHER, SEED, 7, STAMP, tx(2), contract=None)], [
        native_item(SEED, SEED, 9, STAMP, tx(9)),
        native_item(LEFT, SEED, 100, STAMP, tx(3)),
        native_item(LEFT, SEED, 50, STAMP + 3600, tx(4)),
    ])
    code, body = invoke(capsys, inbound_argv(
        tmp_path / "monitor.json", "--yes", "--seed", SEED, "--network", "ethereum-mainnet",
    ), backend)
    assert code == 0, (body, [getattr(call, "url", "") for call in backend.calls])
    assert body["status"] == "complete"
    assert body["plan"]["note"] == "Responses include other addresses. Those addresses are not queried."
    assert body["plan"]["queries"][0]["host"] == "eth.blockscout.example.invalid"
    assert "http" not in json.dumps(body["plan"])
    assert [row["read_status"] for row in body["reads"]] == ["complete"]
    rows = body["counterparties"]
    assert [row["address"] for row in rows] == [LEFT, OTHER]
    assert SEED not in [row["address"] for row in rows]
    first = rows[0]
    assert first["count"] == 2
    assert first["tx_hash_count"] == 2
    assert first["tx_hashes"] == [tx(3), tx(4)]
    assert first["is_contract"] is False
    assert first["label"] is None
    assert first["label_source"] is None
    assert first["assets"] == [{
        "count": 2, "decimals": 18, "kind": "native", "raw_amount": "150", "symbol": "ETH",
    }]
    assert first["first_timestamp"] == iso(STAMP)
    assert first["last_timestamp"] == iso(STAMP + 3600)
    second = rows[1]
    assert second["count"] == 1
    assert second["is_contract"] == "unknown"
    assert second["assets"][0]["kind"] == "token"
    assert second["assets"][0]["raw_amount"] == "7"
    assert second["assets"][0]["decimals"] == 6
    assert second["assets"][0]["contract"] == TOKEN
    assert second["assets"][0]["symbol"] == "SYN"
    assert all(call_query(call).get("filter") == ["to"] for call in backend.calls)
    assert len(backend.calls) == 3
    file_mode, dir_mode = _modes(Path(body["result_path"]))
    assert file_mode == 0o600
    assert dir_mode == 0o700


def test_empty_complete_read_is_an_empty_list(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    sources, routes, secrets = blockscout_profile(
        "ethereum-mainnet", "blockscout-eth", "BLOCKSCOUT_ETH_URL", "https://eth.blockscout.example.invalid",
    )
    backend = install(
        tmp_path, monkeypatch, networks=["ethereum-mainnet"], sources=sources, routes=routes, secrets=secrets,
        address=SEED, family="evm", account_networks=["ethereum-mainnet"],
    )
    fill_tails(backend, [], [])
    code, body = invoke(capsys, inbound_argv(
        tmp_path / "monitor.json", "--yes", "--seed", SEED, "--network", "ethereum-mainnet",
    ), backend)
    assert code == 0
    assert body["status"] == "complete"
    assert body["counterparties"] == []


def test_expand_sorts_same_tx_ahead_of_a_larger_count(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    sources, routes, secrets = blockscout_profile(
        "ethereum-mainnet", "blockscout-eth", "BLOCKSCOUT_ETH_URL", "https://eth.blockscout.example.invalid",
    )
    backend = install(
        tmp_path, monkeypatch, networks=["ethereum-mainnet"], sources=sources, routes=routes, secrets=secrets,
        address=SEED, family="evm", account_networks=["ethereum-mainnet"],
    )
    shared = tx(10)
    transfers = [
        token_item(PAYER, SEED, 1, STAMP, shared),
        token_item(PAYER, LEFT, 1, STAMP, shared),
        token_item(PAYER, RIGHT, 1, STAMP, shared),
        token_item(PAYER, OTHER, 4, STAMP, tx(11)),
        token_item(PAYER, OTHER, 4, STAMP + 10, tx(12)),
        token_item(PAYER, OTHER, 4, STAMP + 20, tx(13)),
    ]
    fill_tails(backend, transfers, [])
    code, body = invoke(capsys, expand_argv(
        tmp_path / "monitor.json", "--yes", "--payer", PAYER, "--network", "ethereum-mainnet", "--seed", SEED,
    ), backend)
    assert code == 0
    assert body["status"] == "complete"
    assert body["hub"] is False
    assert body["hub_reasons"] == []
    assert "noise" not in body
    found = body["co_recipients"]
    addresses = [row["address"] for row in found]
    assert SEED not in addresses
    assert PAYER not in addresses
    assert addresses.index(LEFT) < addresses.index(OTHER)
    assert addresses.index(RIGHT) < addresses.index(OTHER)
    by_address = {row["address"]: row for row in found}
    assert by_address[LEFT]["same_tx_as_seed"] is True
    assert by_address[RIGHT]["same_tx_as_seed"] is True
    assert by_address[OTHER]["same_tx_as_seed"] is False
    assert by_address[OTHER]["count"] == 3
    assert all(call_query(call).get("filter") == ["from"] for call in backend.calls)


def test_tron_expand_merges_two_fingerprint_pages(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    sources = {"tron-grid": _source("tron_grid", "TRONGRID_API_URL", "tron-public")}
    routes = [{"network": "tron-mainnet", "capability": "history.fungible", "source_ids": ["tron-grid"]}]
    backend = install(
        tmp_path, monkeypatch, networks=["tron-mainnet"], sources=sources, routes=routes,
        secrets={"TRONGRID_API_URL": "https://tron.example.invalid"},
        address=TRON_PAYER, family="tron", account_networks=["tron-mainnet"],
    )
    backend.add(lambda call: "transactions/trc20" in call.url, [
        tron_page([
            tron_row(TRON_PAYER, TRON_SEED, "5", "aa" * 32),
            tron_row(TRON_PAYER, TRON_DEST, "5", "bb" * 32),
        ], "pageTwo"),
        tron_page([tron_row(TRON_PAYER, TRON_DEST, "7", "cc" * 32)], None),
    ])
    code, body = invoke(capsys, expand_argv(
        tmp_path / "monitor.json", "--yes", "--payer", TRON_PAYER, "--network", "tron-mainnet", "--seed", TRON_SEED,
    ), backend)
    assert code == 0
    assert body["status"] == "complete"
    assert len(backend.calls) == 2
    first = call_query(backend.calls[0])
    second = call_query(backend.calls[1])
    assert first.get("only_from") == ["true"]
    assert "fingerprint" not in first
    assert "only_to" not in first
    assert second.get("only_from") == ["true"]
    assert second.get("fingerprint") == ["pageTwo"]
    rows = body["co_recipients"]
    assert [row["address"] for row in rows] == [TRON_DEST]
    assert rows[0]["count"] == 2
    assert rows[0]["assets"][0]["raw_amount"] == "12"
    assert rows[0]["assets"][0]["contract"] == TRON_TOKEN
    assert TRON_SEED not in [row["address"] for row in rows]
    assert TRON_PAYER not in [row["address"] for row in rows]


def test_etherscan_fallback_filters_direction_on_the_client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    sources = {
        "blockscout-arb": _source("blockscout", "BLOCKSCOUT_ARB_URL", "blockscout-public"),
        "etherscan": _source("etherscan_v2", "ETHERSCAN_API_BASE", "etherscan-account", "ETHERSCAN_API_KEY"),
    }
    routes = [{
        "network": "arbitrum-one",
        "capability": "history.native_external",
        "source_ids": ["blockscout-arb", "etherscan"],
    }]
    backend = install(
        tmp_path, monkeypatch, networks=["arbitrum-one"], sources=sources, routes=routes,
        secrets={
            "BLOCKSCOUT_ARB_URL": "https://arb.blockscout.example.invalid",
            "ETHERSCAN_API_BASE": "https://etherscan.example.invalid",
            "ETHERSCAN_API_KEY": KEY,
        },
        address=SEED, family="evm", account_networks=["arbitrum-one"],
    )
    backend.add(tail_is("token-transfers"), challenge())
    backend.add(tail_is("transactions"), challenge())
    backend.add(tail_is("internal-transactions"), challenge())
    backend.add(action_is("txlist"), es_ok([
        es_native(LEFT, SEED, 8, STAMP, tx(21)),
        es_native(SEED, RIGHT, 9, STAMP, tx(22)),
        es_native(OTHER, RIGHT, 3, STAMP, tx(23)),
    ]))
    backend.add(action_is("tokentx"), es_empty())
    backend.add(action_is("txlistinternal"), es_empty())
    code, body = invoke(capsys, inbound_argv(
        tmp_path / "monitor.json", "--yes", "--seed", SEED, "--network", "arbitrum-one",
    ), backend)
    assert code == 0
    assert body["status"] == "partial"
    by_source = {row["source_id"]: row for row in body["reads"]}
    assert by_source["blockscout-arb"]["read_status"] == "gap"
    assert by_source["blockscout-arb"]["reason"] == "bot_challenge"
    assert by_source["etherscan"]["read_status"] == "complete"
    assert [row["address"] for row in body["counterparties"]] == [LEFT]
    hosts = {row["host"] for row in body["plan"]["queries"]}
    assert hosts == {"arb.blockscout.example.invalid", "etherscan.example.invalid"}
    etherscan_calls = [call for call in backend.calls if "chainid=42161" in call.url]
    assert etherscan_calls
    assert all("filter=" not in call.url for call in etherscan_calls)
    assert any(KEY in call.url for call in etherscan_calls)


def test_missing_yes_sends_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    sources, routes, secrets = blockscout_profile(
        "ethereum-mainnet", "blockscout-eth", "BLOCKSCOUT_ETH_URL", "https://eth.blockscout.example.invalid",
    )
    backend = install(
        tmp_path, monkeypatch, networks=["ethereum-mainnet"], sources=sources, routes=routes, secrets=secrets,
        address=SEED, family="evm", account_networks=["ethereum-mainnet"],
    )
    code, body = invoke(capsys, inbound_argv(
        tmp_path / "monitor.json", "--seed", SEED, "--network", "ethereum-mainnet",
    ), backend)
    assert code == 2
    assert body["status"] == "consent_required"
    assert backend.calls == []
    assert body["plan"]["queries"][0]["addresses"] == [SEED]
    assert body["reads"] == []


def test_dry_run_sends_nothing_even_with_yes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    sources, routes, secrets = blockscout_profile(
        "ethereum-mainnet", "blockscout-eth", "BLOCKSCOUT_ETH_URL", "https://eth.blockscout.example.invalid",
    )
    backend = install(
        tmp_path, monkeypatch, networks=["ethereum-mainnet"], sources=sources, routes=routes, secrets=secrets,
        address=SEED, family="evm", account_networks=["ethereum-mainnet"],
    )
    code, body = invoke(capsys, inbound_argv(
        tmp_path / "monitor.json", "--dry-run", "--yes", "--seed", SEED, "--network", "ethereum-mainnet",
    ), backend)
    assert code == 0
    assert body["status"] == "dry_run"
    assert backend.calls == []


def test_page_cap_keeps_rows_and_stays_partial(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    sources, routes, secrets = blockscout_profile(
        "ethereum-mainnet", "blockscout-eth", "BLOCKSCOUT_ETH_URL", "https://eth.blockscout.example.invalid",
    )
    backend = install(
        tmp_path, monkeypatch, networks=["ethereum-mainnet"], sources=sources, routes=routes, secrets=secrets,
        address=SEED, family="evm", account_networks=["ethereum-mainnet"],
    )
    backend.add(tail_is("token-transfers"), scout(
        [token_item(LEFT, SEED, 4, STAMP, tx(30))],
        {"block_number": 4, "index": 0},
    ))
    backend.add(tail_is("transactions"), empty_scout())
    backend.add(tail_is("internal-transactions"), empty_scout())
    code, body = invoke(capsys, inbound_argv(
        tmp_path / "monitor.json", "--yes", "--max-pages", "1", "--seed", SEED, "--network", "ethereum-mainnet",
    ), backend)
    assert code == 0
    assert body["status"] == "partial"
    assert body["reads"][0]["read_status"] == "partial"
    assert body["reads"][0]["reason"] == "page_cap"
    assert body["counterparties"][0]["address"] == LEFT
    assert len(backend.calls) == 3
    assert all("block_number=" not in call.url for call in backend.calls)


def test_transport_error_is_a_gap_not_an_empty_list(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    sources, routes, secrets = blockscout_profile(
        "ethereum-mainnet", "blockscout-eth", "BLOCKSCOUT_ETH_URL", "https://eth.blockscout.example.invalid",
    )
    backend = install(
        tmp_path, monkeypatch, networks=["ethereum-mainnet"], sources=sources, routes=routes, secrets=secrets,
        address=SEED, family="evm", account_networks=["ethereum-mainnet"],
    )
    backend.add(lambda _call: True, FixtureFailure("connection_error"))
    code, body = invoke(capsys, inbound_argv(
        tmp_path / "monitor.json", "--yes", "--seed", SEED, "--network", "ethereum-mainnet",
    ), backend)
    assert code == 0
    assert body["status"] == "partial"
    assert body["counterparties"] is None
    assert body["reads"][0]["read_status"] == "gap"
    assert body["reads"][0]["reason"] == "connection_error"
    assert backend.calls


def test_cloudflare_challenge_is_a_gap(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    sources, routes, secrets = blockscout_profile(
        "base-mainnet", "blockscout-base", "BLOCKSCOUT_BASE_URL", "https://base.blockscout.example.invalid",
    )
    backend = install(
        tmp_path, monkeypatch, networks=["base-mainnet"], sources=sources, routes=routes, secrets=secrets,
        address=SEED, family="evm", account_networks=["base-mainnet"],
    )
    backend.add(lambda _call: True, challenge())
    code, body = invoke(capsys, inbound_argv(
        tmp_path / "monitor.json", "--yes", "--seed", SEED, "--network", "base-mainnet",
    ), backend)
    assert code == 0
    assert body["status"] == "partial"
    assert body["counterparties"] is None
    assert body["reads"][0]["read_status"] == "gap"
    assert body["reads"][0]["reason"] == "bot_challenge"
    assert all(row["host"] != "etherscan.example.invalid" for row in body["plan"]["queries"])


def test_contract_payer_is_a_hub_until_expand_hub(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    sources, routes, secrets = blockscout_profile(
        "ethereum-mainnet", "blockscout-eth", "BLOCKSCOUT_ETH_URL", "https://eth.blockscout.example.invalid",
    )
    backend = install(
        tmp_path, monkeypatch, networks=["ethereum-mainnet"], sources=sources, routes=routes, secrets=secrets,
        address=SEED, family="evm", account_networks=["ethereum-mainnet"],
    )
    fill_tails(backend, [token_item(PAYER, LEFT, 2, STAMP, tx(40), contract=True)], [])
    hidden, first = invoke(capsys, expand_argv(
        tmp_path / "monitor.json", "--yes", "--payer", PAYER, "--network", "ethereum-mainnet", "--seed", SEED,
    ), backend)
    assert hidden == 0
    assert first["hub"] is True
    assert first["hub_reasons"] == ["contract"]
    assert "co_recipients" not in first
    assert first["recipient_count"] == 1
    shown, second = invoke(capsys, expand_argv(
        tmp_path / "monitor.json", "--yes", "--expand-hub", "--payer", PAYER, "--network", "ethereum-mainnet", "--seed", SEED,
    ), backend)
    assert shown == 0
    assert second["noise"] == "high"
    assert second["co_recipients"][0]["address"] == LEFT


def test_recipient_count_above_threshold_is_a_hub(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    sources, routes, secrets = blockscout_profile(
        "ethereum-mainnet", "blockscout-eth", "BLOCKSCOUT_ETH_URL", "https://eth.blockscout.example.invalid",
    )
    backend = install(
        tmp_path, monkeypatch, networks=["ethereum-mainnet"], sources=sources, routes=routes, secrets=secrets,
        address=SEED, family="evm", account_networks=["ethereum-mainnet"],
    )
    fill_tails(backend, [token_item(PAYER, evm(20 + index), 1, STAMP, tx(100 + index)) for index in range(201)], [])
    code, body = invoke(capsys, expand_argv(
        tmp_path / "monitor.json", "--yes", "--payer", PAYER, "--network", "ethereum-mainnet", "--seed", SEED,
    ), backend)
    assert code == 0
    assert body["hub"] is True
    assert "recipient_count" in body["hub_reasons"]
    assert body["recipient_count"] == 201
    assert "co_recipients" not in body


def test_recipient_count_at_threshold_is_not_a_hub(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    sources, routes, secrets = blockscout_profile(
        "ethereum-mainnet", "blockscout-eth", "BLOCKSCOUT_ETH_URL", "https://eth.blockscout.example.invalid",
    )
    backend = install(
        tmp_path, monkeypatch, networks=["ethereum-mainnet"], sources=sources, routes=routes, secrets=secrets,
        address=SEED, family="evm", account_networks=["ethereum-mainnet"],
    )
    fill_tails(backend, [token_item(PAYER, evm(300 + index), 1, STAMP, tx(400 + index)) for index in range(200)], [])
    code, body = invoke(capsys, expand_argv(
        tmp_path / "monitor.json", "--yes", "--payer", PAYER, "--network", "ethereum-mainnet", "--seed", SEED,
    ), backend)
    assert code == 0
    assert body["hub"] is False
    assert len(body["co_recipients"]) == 200


def test_bsc_is_gated_with_zero_requests(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    sources = {"nodereal-bsc": _source("nodereal_transfers", "NODEREAL_BSC_URL", "nodereal-bsc")}
    routes = [{"network": "bsc-mainnet", "capability": "history.native_external", "source_ids": ["nodereal-bsc"]}]
    backend = install(
        tmp_path, monkeypatch, networks=["bsc-mainnet"], sources=sources, routes=routes,
        secrets={"NODEREAL_BSC_URL": "https://bsc.example.invalid"},
        address=SEED, family="evm", account_networks=["bsc-mainnet"],
    )
    code, body = invoke(capsys, inbound_argv(
        tmp_path / "monitor.json", "--yes", "--seed", SEED, "--network", "bsc-mainnet",
    ), backend)
    assert code == 0
    assert backend.calls == []
    assert body["status"] == "partial"
    assert body["counterparties"] is None
    assert body["reads"][0]["read_status"] == "gap"
    assert body["reads"][0]["gap"] == "credential_missing"
    assert body["reads"][0]["reason"] == "bsc_source_gated"


def test_solana_and_bitcoin_seeds_are_unsupported(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    sources, routes, secrets = blockscout_profile(
        "ethereum-mainnet", "blockscout-eth", "BLOCKSCOUT_ETH_URL", "https://eth.blockscout.example.invalid",
    )
    backend = install(
        tmp_path, monkeypatch, networks=["ethereum-mainnet"], sources=sources, routes=routes, secrets=secrets,
        address=SEED, family="evm", account_networks=["ethereum-mainnet"],
    )
    code, body = invoke(capsys, inbound_argv(
        tmp_path / "monitor.json", "--yes", "--seed", SOL, "--seed", BITCOIN,
    ), backend)
    assert code == 0, body
    assert backend.calls == []
    assert body["status"] == "partial"
    assert body["counterparties"] is None
    assert {row["network"] for row in body["reads"]} == {"solana-mainnet", "bitcoin-mainnet"}
    assert {row["read_status"] for row in body["reads"]} == {"unsupported"}


def test_omitted_seed_uses_the_account_and_stores_a_private_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    sources, routes, secrets = blockscout_profile(
        "ethereum-mainnet", "blockscout-eth", "BLOCKSCOUT_ETH_URL", "https://eth.blockscout.example.invalid",
    )
    backend = install(
        tmp_path, monkeypatch, networks=["ethereum-mainnet"], sources=sources, routes=routes, secrets=secrets,
        address=SEED, family="evm", account_networks=["ethereum-mainnet"],
    )
    fill_tails(backend, [], [native_item(LEFT, SEED, 11, STAMP, tx(70))])
    code, body = invoke(capsys, inbound_argv(tmp_path / "monitor.json", "--yes", "--network", "ethereum-mainnet"), backend)
    assert code == 0
    assert body["counterparties"][0]["address"] == LEFT
    assert body["plan"]["queries"][0]["addresses"] == [SEED]
    path = Path(body["result_path"])
    file_mode, dir_mode = _modes(path)
    assert file_mode == 0o600
    assert dir_mode == 0o700
    assert json.loads(path.read_text(encoding="utf-8"))["status"] == "complete"


def test_static_import_and_install_text_stay_isolated(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root = Path(__file__).resolve().parents[2] / "src" / "guardian"
    for path in root.rglob("*.py"):
        if "relations" in path.parts or path.name == "cli.py":
            continue
        text = path.read_text(encoding="utf-8")
        assert "guardian.relations" not in text
    base = Path(tempfile.mkdtemp(prefix="wgiso"))
    assert "relations" not in str(base)
    executable = base / "bin" / "guardian"
    config = base / "config"
    state = base / "state"
    monitor = config / "monitor.json"

    def runner(_argv: list[str]) -> CommandResult:
        return CommandResult(1, "", "")

    for systemd_present, cron_present in ((True, False), (False, True)):
        payload = execute_install_service(
            dry_run=True,
            apply=False,
            mcm_config=str(monitor),
            output_dir=str(base / "out"),
            executable=str(executable),
            config_directory=str(config),
            state_directory=str(state),
            runner=runner,
            systemd_present=systemd_present,
            cron_present=cron_present,
        )
        assert "relations" not in json.dumps(payload["rendered"])
    launchd = execute_install_service(
        dry_run=True,
        apply=False,
        mcm_config=str(monitor),
        output_dir=str(base / "launch"),
        executable=str(executable),
        config_directory=str(config),
        state_directory=str(state),
        schedule_mode="launchd_agent",
    )
    assert "relations" not in json.dumps(launchd["rendered"])
    code = main(
        [
            "install-service",
            "--dry-run",
            "--schedule-mode",
            "launchd_agent",
            "--output",
            str(base / "cli"),
            "--executable",
            str(executable),
            "--config-dir",
            str(config),
            "--state-dir",
            str(state),
            "--mcm-config",
            str(monitor),
        ],
        service_runner=runner,
    )
    printed = json.loads(capsys.readouterr().out)
    assert code == 1
    assert "relations" not in json.dumps(printed["rendered"])


def test_read_v2_keeps_previous_behavior_until_the_new_arguments() -> None:
    page = {"items": [native_item(LEFT, SEED, 1, STAMP, tx(1))], "next_page_params": {"block_number": 3, "index": 1}}
    capped, transport = _scout_client([page, page])
    dropped = capped.read_v2(SEED, "transactions", page_cap=1)
    assert dropped.items == ()
    assert dropped.category == "error"
    assert dropped.reason == "page_cap"
    assert transport.calls[0].query in (None, {})
    kept_client, kept_transport = _scout_client([page])
    kept = kept_client.read_v2(SEED, "transactions", page_cap=1, keep_partial=True)
    assert len(kept.items) == 1
    assert kept.category == "partial"
    assert kept.pagination_complete is False
    assert kept.reason == "page_cap"
    assert kept_transport.calls[0].query in (None, {})
    filtered, filtered_transport = _scout_client([
        {"items": [native_item(LEFT, SEED, 1, STAMP, tx(1))], "next_page_params": {"block_number": 3, "index": 1}},
        {"items": [], "next_page_params": None},
    ])
    filtered.read_v2(SEED, "transactions", query={"filter": "to"})
    assert filtered_transport.calls[0].query == {"filter": "to"}
    assert filtered_transport.calls[1].query == {"block_number": "3", "index": "1"}
    assert "limit" not in filtered_transport.calls[0].query
    assert "limit" not in filtered_transport.calls[1].query
    refused, refused_transport = _scout_client([])
    bad = refused.read_v2(SEED, "transactions", query={"filter": "both"})
    assert refused_transport.calls == []
    assert bad.reason == "bad_page"
    older = native_item(LEFT, SEED, 1, STAMP - 20 * 86400, tx(2))
    newer = native_item(OTHER, SEED, 1, STAMP, tx(3))
    window, window_transport = _scout_client([{
        "items": [newer, older],
        "next_page_params": {"block_number": 1, "index": 0},
    }])
    done = window.read_v2(SEED, "transactions", stop_timestamp=STAMP - 86400)
    assert [item["hash"] for item in done.items] == [tx(3)]
    assert done.pagination_complete is True
    assert done.category == "ok"
    assert len(window_transport.calls) == 1


def test_history_without_page_cap_keeps_the_previous_length() -> None:
    page_cap, _client = _tron_types()
    pages = [
        {"data": [{"n": 1}], "meta": {"fingerprint": "pageTwo"}, "success": True},
        {"data": [{"n": 2}], "meta": {"fingerprint": "pageThree"}, "success": True},
        {"data": [{"n": 3}], "success": True},
    ]
    default_walk, default_calls = _tron_history(pages, None)
    assert len(default_calls) == page_cap
    assert default_walk.complete is False
    assert default_walk.reason == "page_cap"
    assert len(default_walk.rows) == 2
    one_walk, one_calls = _tron_history(pages, 1)
    assert len(one_calls) == 1
    assert one_walk.reason == "page_cap"
    three_walk, three_calls = _tron_history(pages, 3)
    assert len(three_calls) == 3
    assert three_walk.complete is True
    assert three_walk.reason == ""
    assert len(three_walk.rows) == 3


def test_etherscan_reads_newest_first_and_stops_at_the_window(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    sources, routes, secrets = etherscan_profile()
    backend = install(
        tmp_path, monkeypatch, networks=["ethereum-mainnet"], sources=sources, routes=routes, secrets=secrets,
        address=SEED, family="evm", account_networks=["ethereum-mainnet"],
    )
    page = [es_native(LEFT, SEED, 1, STAMP, tx(1))] * 1000
    backend.add(action_is("txlist"), [
        es_ok(page),
        es_ok([es_native(OTHER, SEED, 2, STAMP, tx(2)), es_native(RIGHT, SEED, 3, 1, tx(3))]),
    ])
    backend.add(action_is("tokentx"), es_empty())
    backend.add(action_is("txlistinternal"), es_empty())
    code, body = invoke(capsys, inbound_argv(
        tmp_path / "monitor.json", "--yes", "--seed", SEED, "--network", "ethereum-mainnet",
    ), backend)
    assert code == 0, body
    assert body["status"] == "complete"
    assert body["reads"][0]["read_status"] == "complete"
    found = [row["address"] for row in body["counterparties"]]
    assert LEFT in found
    assert OTHER in found
    assert RIGHT not in found
    queries = [call_query(call) for call in backend.calls if call_query(call).get("action") == ["txlist"]]
    assert queries
    assert all(item.get("sort") == ["desc"] for item in queries)
    assert all(item.get("endblock") == ["9999999999"] for item in queries)
    assert all(item.get("endblock") != ["99999999"] for item in queries)
    assert not any(item.get("page") == ["3"] for item in queries)


def test_etherscan_result_window_is_partial(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from guardian.relations.fetch import read_etherscan

    page = {"status": "1", "message": "OK", "result": [es_native(LEFT, SEED, 1, STAMP, tx(1))] * 1000}
    transport = _WindowTransport(page)
    outcome = read_etherscan(
        transport,
        "etherscan",
        {"credential_env": "ETHERSCAN_API_KEY"},
        secrets={"ETHERSCAN_API_KEY": KEY},
        address=SEED,
        direction="to",
        network="ethereum-mainnet",
        family="evm",
        page_cap=50,
        stop_timestamp=STAMP - 86400,
        entry={"evm_chain_id": 1, "native_asset": {"symbol": "ETH", "decimals": 18}},
        clock=lambda: CLOCK,
    )
    assert outcome.status == "partial"
    assert outcome.reason == "result_window"
    assert len(outcome.edges) == 10_000
    assert transport.tx_pages == 10


def test_etherscan_error_bodies_are_gaps(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    cases = (
        {"status": "0", "message": "NOTOK", "result": "Query Timeout occured"},
        {"status": "0", "message": "NOTOK", "result": []},
        {"status": "1", "message": "OK", "result": [es_native(LEFT, SEED, 4, STAMP, tx(4))], "error": "lag"},
    )
    for payload in cases:
        sources, routes, secrets = etherscan_profile()
        backend = install(
            tmp_path, monkeypatch, networks=["ethereum-mainnet"], sources=sources, routes=routes, secrets=secrets,
            address=SEED, family="evm", account_networks=["ethereum-mainnet"],
        )
        backend.add(action_is("txlist"), raw(payload))
        backend.add(action_is("tokentx"), es_empty())
        backend.add(action_is("txlistinternal"), es_empty())
        code, body = invoke(capsys, inbound_argv(
            tmp_path / "monitor.json", "--yes", "--seed", SEED, "--network", "ethereum-mainnet",
        ), backend)
        assert code == 0, body
        assert body["reads"][0]["read_status"] == "gap"
        assert body["counterparties"] is None
        assert LEFT not in json.dumps(body["counterparties"])
        if str(payload.get("status")) == "0":
            assert body["reads"][0]["reason"] == "upstream_error"


def test_etherscan_second_page_failure_drops_edges(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    sources, routes, secrets = etherscan_profile()
    backend = install(
        tmp_path, monkeypatch, networks=["ethereum-mainnet"], sources=sources, routes=routes, secrets=secrets,
        address=SEED, family="evm", account_networks=["ethereum-mainnet"],
    )
    backend.add(action_is("txlist"), [
        es_ok([es_native(LEFT, SEED, 1, STAMP, tx(1))] * 1000),
        raw({"status": "0", "message": "NOTOK", "result": "Query Timeout occured"}),
    ])
    backend.add(action_is("tokentx"), es_empty())
    backend.add(action_is("txlistinternal"), es_empty())
    code, body = invoke(capsys, inbound_argv(
        tmp_path / "monitor.json", "--yes", "--seed", SEED, "--network", "ethereum-mainnet",
    ), backend)
    assert code == 0, body
    assert body["reads"][0]["read_status"] == "gap"
    assert body["counterparties"] is None
    assert any(call_query(call).get("page") == ["2"] for call in backend.calls)
    assert not any(call_query(call).get("page") == ["3"] for call in backend.calls)


def test_etherscan_key_from_the_environment_is_redacted(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    sources = {
        "blockscout-arb": _source("blockscout", "BLOCKSCOUT_ARB_URL", "blockscout-public"),
        "etherscan": _source("etherscan_v2", "ETHERSCAN_API_BASE", "etherscan-account", "ETHERSCAN_API_KEY"),
    }
    routes = [{
        "network": "arbitrum-one",
        "capability": "history.native_external",
        "source_ids": ["blockscout-arb", "etherscan"],
    }]
    backend = install(
        tmp_path, monkeypatch, networks=["arbitrum-one"], sources=sources, routes=routes,
        secrets={
            "BLOCKSCOUT_ARB_URL": "https://arb.blockscout.example.invalid",
            "ETHERSCAN_API_BASE": "https://etherscan.example.invalid",
        },
        address=SEED, family="evm", account_networks=["arbitrum-one"],
    )
    monkeypatch.setenv("ETHERSCAN_API_KEY", ENV_KEY)
    backend.add(tail_is("token-transfers"), challenge())
    backend.add(tail_is("transactions"), challenge())
    backend.add(tail_is("internal-transactions"), challenge())
    backend.add(action_is("txlist"), es_ok([es_native(LEFT, SEED, 8, STAMP, tx(21))]))
    backend.add(action_is("tokentx"), es_empty())
    backend.add(action_is("txlistinternal"), es_empty())
    code, body = invoke(capsys, inbound_argv(
        tmp_path / "monitor.json", "--yes", "--seed", SEED, "--network", "arbitrum-one",
    ), backend)
    assert code == 0, body
    assert [row["address"] for row in body["counterparties"]] == [LEFT]
    assert any(ENV_KEY in call.url for call in backend.calls)
    assert ENV_KEY not in json.dumps(body)
    assert ENV_KEY not in Path(body["result_path"]).read_text(encoding="utf-8")


def test_budget_exhaustion_keeps_earlier_tails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    sources, routes, secrets = blockscout_profile(
        "ethereum-mainnet", "blockscout-eth", "BLOCKSCOUT_ETH_URL", "https://eth.blockscout.example.invalid",
    )
    backend = install(
        tmp_path, monkeypatch, networks=["ethereum-mainnet"], sources=sources, routes=routes, secrets=secrets,
        address=SEED, family="evm", account_networks=["ethereum-mainnet"], daily=1,
    )
    fill_tails(backend, [token_item(LEFT, SEED, 4, STAMP, tx(1))], [native_item(OTHER, SEED, 9, STAMP, tx(2))])
    code, body = invoke(capsys, inbound_argv(
        tmp_path / "monitor.json", "--yes", "--seed", SEED, "--network", "ethereum-mainnet",
    ), backend)
    assert code == 0, body
    assert body["status"] == "partial"
    assert body["reads"][0]["read_status"] == "gap"
    assert body["reads"][0]["reason"] == "daily_cap"
    assert [row["address"] for row in body["counterparties"]] == [LEFT]
    assert Path(body["result_path"]).is_file()
    assert len(backend.calls) == 1


def test_redirect_host_is_a_gap_and_other_networks_remain(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    eth_sources, eth_routes, eth_secrets = blockscout_profile(
        "ethereum-mainnet", "blockscout-eth", "BLOCKSCOUT_ETH_URL", "https://eth.blockscout.example.invalid",
    )
    op_sources, op_routes, op_secrets = blockscout_profile(
        "optimism-mainnet", "blockscout-op", "BLOCKSCOUT_OP_URL", "https://op.blockscout.example.invalid",
    )
    backend = install(
        tmp_path, monkeypatch, networks=["ethereum-mainnet", "optimism-mainnet"],
        sources={**eth_sources, **op_sources}, routes=[*eth_routes, *op_routes], secrets={**eth_secrets, **op_secrets},
        address=SEED, family="evm", account_networks=["ethereum-mainnet"],
    )
    backend.add(
        lambda call: urlsplit(call.url).hostname == "op.blockscout.example.invalid",
        RawResponse(301, {"location": "https://explorer.optimism.example.invalid/api"}, b""),
    )
    fill_tails(backend, [token_item(LEFT, SEED, 4, STAMP, tx(1))], [])
    code, body = invoke(capsys, inbound_argv(
        tmp_path / "monitor.json", "--yes", "--seed", SEED, "--network", "ethereum-mainnet", "--network", "optimism-mainnet",
    ), backend)
    assert code == 0, body
    assert body["status"] == "partial"
    by_network = {row["network"]: row for row in body["reads"]}
    assert by_network["ethereum-mainnet"]["read_status"] == "complete"
    assert by_network["optimism-mainnet"]["read_status"] == "gap"
    assert by_network["optimism-mainnet"]["reason"] == "redirect_host"
    assert [row["address"] for row in body["counterparties"]] == [LEFT]
    assert all(urlsplit(call.url).hostname != "explorer.optimism.example.invalid" for call in backend.calls)


def test_other_security_errors_still_fail_the_command(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    sources, routes, secrets = blockscout_profile(
        "ethereum-mainnet", "blockscout-eth", "BLOCKSCOUT_ETH_URL", "https://eth.blockscout.example.invalid",
    )
    backend = install(
        tmp_path, monkeypatch, networks=["ethereum-mainnet"], sources=sources, routes=routes, secrets=secrets,
        address=SEED, family="evm", account_networks=["ethereum-mainnet"],
    )
    backend.add(lambda _call: True, SecurityError("adapter_mismatch", reason="adapter_mismatch"))
    code, body = invoke(capsys, inbound_argv(
        tmp_path / "monitor.json", "--yes", "--seed", SEED, "--network", "ethereum-mainnet",
    ), backend)
    assert code == 4
    assert body["error_type"] == "SecurityError"


def test_gap_part_sets_the_read_reason(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    sources, routes, secrets = blockscout_profile(
        "ethereum-mainnet", "blockscout-eth", "BLOCKSCOUT_ETH_URL", "https://eth.blockscout.example.invalid",
    )
    backend = install(
        tmp_path, monkeypatch, networks=["ethereum-mainnet"], sources=sources, routes=routes, secrets=secrets,
        address=SEED, family="evm", account_networks=["ethereum-mainnet"],
    )
    backend.add(tail_is("token-transfers"), scout(
        [token_item(LEFT, SEED, 4, STAMP, tx(1))],
        {"block_number": 4, "index": 0},
    ))
    backend.add(tail_is("transactions"), challenge())
    backend.add(tail_is("internal-transactions"), empty_scout())
    code, body = invoke(capsys, inbound_argv(
        tmp_path / "monitor.json", "--yes", "--max-pages", "1", "--seed", SEED, "--network", "ethereum-mainnet",
    ), backend)
    assert code == 0, body
    row = body["reads"][0]
    assert row["reason"] == "bot_challenge"
    by_part = {part["part"]: part for part in row["parts"]}
    assert by_part["token-transfers"] == {"part": "token-transfers", "read_status": "partial", "reason": "page_cap"}
    assert by_part["transactions"] == {"part": "transactions", "read_status": "gap", "reason": "bot_challenge"}
    assert [item["address"] for item in body["counterparties"]] == [LEFT]


def test_zero_value_and_contract_creation_are_not_edges(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    sources, routes, secrets = blockscout_profile(
        "ethereum-mainnet", "blockscout-eth", "BLOCKSCOUT_ETH_URL", "https://eth.blockscout.example.invalid",
    )
    backend = install(
        tmp_path, monkeypatch, networks=["ethereum-mainnet"], sources=sources, routes=routes, secrets=secrets,
        address=SEED, family="evm", account_networks=["ethereum-mainnet"],
    )
    shared = tx(10)
    creation = native_item(PAYER, LEFT, 3, STAMP, tx(8))
    creation["to"] = None
    nft = token_item(PAYER, LEFT, 1, STAMP, tx(12))
    nft["token"] = {"symbol": "N", "type": "ERC-721"}
    fill_tails(backend, [
        token_item(PAYER, SEED, 5, STAMP, shared),
        token_item(PAYER, LEFT, 5, STAMP, tx(11)),
        token_item(PAYER, TOKEN, 0, STAMP, tx(13)),
        nft,
    ], [native_item(PAYER, TOKEN, 0, STAMP, shared), creation])
    code, body = invoke(capsys, expand_argv(
        tmp_path / "monitor.json", "--yes", "--payer", PAYER, "--network", "ethereum-mainnet", "--seed", SEED,
    ), backend)
    assert code == 0, body
    assert body["status"] == "complete"
    assert body["reads"][0]["read_status"] == "complete"
    assert body["reads"][0]["ignored_zero_value"] == 2
    assert [row["address"] for row in body["co_recipients"]] == [LEFT]
    assert TOKEN not in [row["address"] for row in body["co_recipients"]]


def test_missing_amount_stays_null(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    sources, routes, secrets = blockscout_profile(
        "ethereum-mainnet", "blockscout-eth", "BLOCKSCOUT_ETH_URL", "https://eth.blockscout.example.invalid",
    )
    backend = install(
        tmp_path, monkeypatch, networks=["ethereum-mainnet"], sources=sources, routes=routes, secrets=secrets,
        address=SEED, family="evm", account_networks=["ethereum-mainnet"],
    )
    item = native_item(LEFT, SEED, 1, STAMP, tx(1))
    item["value"] = None
    fill_tails(backend, [], [item])
    code, body = invoke(capsys, inbound_argv(
        tmp_path / "monitor.json", "--yes", "--seed", SEED, "--network", "ethereum-mainnet",
    ), backend)
    assert code == 0, body
    assert body["status"] == "complete"
    assert body["counterparties"][0]["assets"][0]["raw_amount"] is None
    assert body["counterparties"][0]["transfers"][0]["raw_amount"] is None


def test_missing_fungible_contract_is_partial(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    sources, routes, secrets = blockscout_profile(
        "ethereum-mainnet", "blockscout-eth", "BLOCKSCOUT_ETH_URL", "https://eth.blockscout.example.invalid",
    )
    backend = install(
        tmp_path, monkeypatch, networks=["ethereum-mainnet"], sources=sources, routes=routes, secrets=secrets,
        address=SEED, family="evm", account_networks=["ethereum-mainnet"],
    )
    bad = token_item(LEFT, SEED, 4, STAMP, tx(1))
    bad["token"] = {"decimals": "6", "symbol": "SYN", "type": "ERC-20"}
    fill_tails(backend, [bad, token_item(OTHER, SEED, 5, STAMP, tx(2))], [])
    code, body = invoke(capsys, inbound_argv(
        tmp_path / "monitor.json", "--yes", "--seed", SEED, "--network", "ethereum-mainnet",
    ), backend)
    assert code == 0, body
    assert body["reads"][0]["read_status"] == "partial"
    assert body["reads"][0]["reason"] == "unreadable_row"
    assert [row["address"] for row in body["counterparties"]] == [OTHER]
    sources, routes, secrets = etherscan_profile()
    backend = install(
        tmp_path, monkeypatch, networks=["ethereum-mainnet"], sources=sources, routes=routes, secrets=secrets,
        address=SEED, family="evm", account_networks=["ethereum-mainnet"],
    )
    missing = es_token(LEFT, SEED, 4, STAMP, tx(3), contract="")
    backend.add(action_is("txlist"), es_empty())
    backend.add(action_is("tokentx"), es_ok([missing]))
    backend.add(action_is("txlistinternal"), es_empty())
    code, body = invoke(capsys, inbound_argv(
        tmp_path / "monitor.json", "--yes", "--seed", SEED, "--network", "ethereum-mainnet",
    ), backend)
    assert code == 0, body
    assert body["reads"][0]["read_status"] == "partial"
    assert body["reads"][0]["reason"] == "unreadable_row"
    assert body["counterparties"] is None


def test_pending_rows_are_skipped_only_when_asked() -> None:
    pending = native_item(LEFT, SEED, 1, STAMP, tx(1))
    pending["block_number"] = None
    pending["result"] = "pending"
    confirmed = native_item(OTHER, SEED, 2, STAMP, tx(2))
    plain, _transport = _scout_client([{"items": [pending], "next_page_params": None}])
    refused = plain.read_v2(SEED, "transactions")
    assert refused.reason == "missing_block"
    assert refused.items == ()
    skipped, _transport = _scout_client([{"items": [pending, confirmed], "next_page_params": None}])
    done = skipped.read_v2(SEED, "transactions", skip_pending=True)
    assert done.category == "ok"
    assert [item["hash"] for item in done.items] == [tx(2)]


def test_relations_skip_pending_rows(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    sources, routes, secrets = blockscout_profile(
        "ethereum-mainnet", "blockscout-eth", "BLOCKSCOUT_ETH_URL", "https://eth.blockscout.example.invalid",
    )
    backend = install(
        tmp_path, monkeypatch, networks=["ethereum-mainnet"], sources=sources, routes=routes, secrets=secrets,
        address=SEED, family="evm", account_networks=["ethereum-mainnet"],
    )
    pending = native_item(LEFT, SEED, 1, STAMP, tx(1))
    pending["block_number"] = None
    pending["result"] = "pending"
    fill_tails(backend, [], [pending, native_item(OTHER, SEED, 2, STAMP, tx(2))])
    code, body = invoke(capsys, inbound_argv(
        tmp_path / "monitor.json", "--yes", "--seed", SEED, "--network", "ethereum-mainnet",
    ), backend)
    assert code == 0, body
    assert body["status"] == "complete"
    assert [row["address"] for row in body["counterparties"]] == [OTHER]


def test_read_v2_keep_partial_item_cap_and_missing_timestamp() -> None:
    first = native_item(LEFT, SEED, 1, STAMP, tx(1))
    second = native_item(OTHER, SEED, 1, STAMP, tx(2))
    capped, _transport = _scout_client([{"items": [first, second], "next_page_params": None}])
    partial = capped.read_v2(SEED, "transactions", item_cap=1, keep_partial=True)
    assert len(partial.items) == 1
    assert partial.category == "partial"
    assert partial.reason == "item_cap"
    blank = native_item(LEFT, SEED, 1, STAMP, tx(3))
    blank.pop("timestamp")
    missing, _transport = _scout_client([{"items": [blank], "next_page_params": None}])
    read = missing.read_v2(SEED, "transactions", stop_timestamp=STAMP)
    assert read.reason == "missing_timestamp"
    assert read.items == ()


def test_recipient_count_includes_the_seed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    sources, routes, secrets = blockscout_profile(
        "ethereum-mainnet", "blockscout-eth", "BLOCKSCOUT_ETH_URL", "https://eth.blockscout.example.invalid",
    )
    backend = install(
        tmp_path, monkeypatch, networks=["ethereum-mainnet"], sources=sources, routes=routes, secrets=secrets,
        address=SEED, family="evm", account_networks=["ethereum-mainnet"],
    )
    rows = [token_item(PAYER, SEED, 1, STAMP, tx(1))]
    rows.extend(token_item(PAYER, evm(20 + index), 1, STAMP, tx(100 + index)) for index in range(200))
    fill_tails(backend, rows, [])
    code, body = invoke(capsys, expand_argv(
        tmp_path / "monitor.json", "--yes", "--payer", PAYER, "--network", "ethereum-mainnet", "--seed", SEED,
    ), backend)
    assert code == 0, body
    assert body["recipient_count"] == 201
    assert body["hub"] is True
    assert "recipient_count" in body["hub_reasons"]
    assert "co_recipients" not in body


def test_incomplete_payer_read_does_not_assert_hub(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    sources, routes, secrets = blockscout_profile(
        "ethereum-mainnet", "blockscout-eth", "BLOCKSCOUT_ETH_URL", "https://eth.blockscout.example.invalid",
    )
    backend = install(
        tmp_path, monkeypatch, networks=["ethereum-mainnet"], sources=sources, routes=routes, secrets=secrets,
        address=SEED, family="evm", account_networks=["ethereum-mainnet"],
    )
    backend.add(tail_is("token-transfers"), scout([
        token_item(PAYER, LEFT, 1, STAMP, tx(1)),
        token_item(PAYER, RIGHT, 1, STAMP, tx(2)),
    ]))
    backend.add(tail_is("transactions"), challenge())
    backend.add(tail_is("internal-transactions"), empty_scout())
    code, body = invoke(capsys, expand_argv(
        tmp_path / "monitor.json", "--yes", "--payer", PAYER, "--network", "ethereum-mainnet", "--seed", SEED,
    ), backend)
    assert code == 0, body
    assert body["hub"] is None
    assert body["recipient_count_complete"] is False
    assert {row["address"] for row in body["co_recipients"]} == {LEFT, RIGHT}


def test_tron_request_cap_keeps_rows(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    sources = {"tron-grid": _source("tron_grid", "TRONGRID_API_URL", "tron-public")}
    routes = [{"network": "tron-mainnet", "capability": "history.fungible", "source_ids": ["tron-grid"]}]
    backend = install(
        tmp_path, monkeypatch, networks=["tron-mainnet"], sources=sources, routes=routes,
        secrets={"TRONGRID_API_URL": "https://tron.example.invalid"},
        address=TRON_PAYER, family="tron", account_networks=["tron-mainnet"],
    )
    pages = [
        tron_page([tron_row(TRON_PAYER, TRON_DEST, "1", f"{index:064x}")], f"fp{index:02d}")
        for index in range(60)
    ]
    backend.add(lambda call: "transactions/trc20" in call.url, pages)
    code, body = invoke(capsys, expand_argv(
        tmp_path / "monitor.json", "--yes", "--max-pages", "50", "--payer", TRON_PAYER,
        "--network", "tron-mainnet", "--seed", TRON_SEED,
    ), backend)
    assert code == 0, body
    assert body["status"] == "partial"
    assert body["reads"][0]["read_status"] == "partial"
    assert body["reads"][0]["reason"] == "request_cap"
    assert body["co_recipients"][0]["address"] == TRON_DEST
    assert body["co_recipients"][0]["count"] == 48
    assert len(backend.calls) == 48


def test_omitted_seed_uses_the_account_family(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    sources, routes, secrets = blockscout_profile(
        "ethereum-mainnet", "blockscout-eth", "BLOCKSCOUT_ETH_URL", "https://eth.blockscout.example.invalid",
    )
    sui = parse_address("sui", "0x1").canonical
    backend = install(
        tmp_path, monkeypatch, networks=["ethereum-mainnet"], sources=sources, routes=routes, secrets=secrets,
        address=SEED, family="evm", account_networks=["ethereum-mainnet"],
        extra_accounts=[{
            "wallet_id": "22222222-2222-4222-8222-222222222222",
            "label": "move-desk",
            "family": "sui",
            "address": "0x1",
            "networks": ["sui-mainnet"],
            "relationship": "watch_only",
        }],
    )
    code, body = invoke(capsys, inbound_argv(tmp_path / "monitor.json", "--dry-run"), backend)
    assert code == 0, body
    assert backend.calls == []
    assert body["status"] == "dry_run"
    queried = [address for row in body["plan"]["queries"] for address in row["addresses"]]
    assert SEED in queried
    assert sui not in queried
    unmatched = [row for row in body["plan"]["unqueried"] if row["address"] == sui]
    assert unmatched
    assert unmatched[0]["read_status"] == "unsupported"
    assert unmatched[0]["reason"] == "unsupported"


def test_network_flag_keeps_other_families_unqueried(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    sources, routes, secrets = blockscout_profile(
        "ethereum-mainnet", "blockscout-eth", "BLOCKSCOUT_ETH_URL", "https://eth.blockscout.example.invalid",
    )
    backend = install(
        tmp_path, monkeypatch, networks=["ethereum-mainnet"], sources=sources, routes=routes, secrets=secrets,
        address=SEED, family="evm", account_networks=["ethereum-mainnet"],
    )
    code, body = invoke(capsys, inbound_argv(
        tmp_path / "monitor.json", "--dry-run", "--seed", SEED, "--seed", TRON_SEED, "--network", "ethereum-mainnet",
    ), backend)
    assert code == 0, body
    assert backend.calls == []
    assert {row["network"] for row in body["plan"]["queries"]} == {"ethereum-mainnet"}
    assert all(TRON_SEED not in row["addresses"] for row in body["plan"]["queries"])
    unmatched = [row for row in body["plan"]["unqueried"] if row["address"] == TRON_SEED]
    assert unmatched
    assert unmatched[0]["read_status"] == "unsupported"
    assert unmatched[0]["reason"] == "network_family_mismatch"


def test_network_family_errors_only_when_nothing_matches(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    sources, routes, secrets = blockscout_profile(
        "ethereum-mainnet", "blockscout-eth", "BLOCKSCOUT_ETH_URL", "https://eth.blockscout.example.invalid",
    )
    backend = install(
        tmp_path, monkeypatch, networks=["ethereum-mainnet", "tron-mainnet"], sources=sources, routes=routes, secrets=secrets,
        address=SEED, family="evm", account_networks=["ethereum-mainnet"],
    )
    code, body = invoke(capsys, inbound_argv(
        tmp_path / "monitor.json", "--dry-run", "--seed", SEED, "--network", "tron-mainnet",
    ), backend)
    assert code == 2
    assert body["error_type"] == "ConfigError"
    assert "network_family" in body["error"]
    assert backend.calls == []
    code, body = invoke(capsys, inbound_argv(
        tmp_path / "monitor.json", "--dry-run", "--seed", SEED, "--network", "base-mainnet",
    ), backend)
    assert code == 2
    assert "network_disabled" in body["error"]
    assert backend.calls == []


def test_max_nodes_rejects_too_many_queries_and_truncates_output(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    sources, routes, secrets = blockscout_profile(
        "ethereum-mainnet", "blockscout-eth", "BLOCKSCOUT_ETH_URL", "https://eth.blockscout.example.invalid",
    )
    backend = install(
        tmp_path, monkeypatch, networks=["ethereum-mainnet"], sources=sources, routes=routes, secrets=secrets,
        address=SEED, family="evm", account_networks=["ethereum-mainnet"],
    )
    code, body = invoke(capsys, inbound_argv(
        tmp_path / "monitor.json", "--yes", "--max-nodes", "1", "--seed", SEED, "--seed", OTHER, "--network", "ethereum-mainnet",
    ), backend)
    assert code == 2
    assert body["error_type"] == "ConfigError"
    assert "max_nodes" in body["error"]
    assert backend.calls == []
    rows = [token_item(LEFT, SEED, 1, STAMP, tx(index)) for index in range(3)]
    rows.extend(token_item(OTHER, SEED, 1, STAMP, tx(10 + index)) for index in range(2))
    rows.append(token_item(RIGHT, SEED, 1, STAMP, tx(20)))
    fill_tails(backend, rows, [])
    code, body = invoke(capsys, inbound_argv(
        tmp_path / "monitor.json", "--yes", "--max-nodes", "2", "--seed", SEED, "--network", "ethereum-mainnet",
    ), backend)
    assert code == 0, body
    assert body["status"] == "partial"
    assert body["reads"][0]["read_status"] == "complete"
    assert [row["address"] for row in body["counterparties"]] == [LEFT, OTHER]
    assert body["truncated"] == {"reason": "node_cap", "total": 3}


def test_hub_count_uses_the_full_recipient_total(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    sources, routes, secrets = blockscout_profile(
        "ethereum-mainnet", "blockscout-eth", "BLOCKSCOUT_ETH_URL", "https://eth.blockscout.example.invalid",
    )
    backend = install(
        tmp_path, monkeypatch, networks=["ethereum-mainnet"], sources=sources, routes=routes, secrets=secrets,
        address=SEED, family="evm", account_networks=["ethereum-mainnet"],
    )
    fill_tails(backend, [token_item(PAYER, evm(20 + index), 1, STAMP, tx(100 + index)) for index in range(201)], [])
    code, body = invoke(capsys, expand_argv(
        tmp_path / "monitor.json", "--yes", "--expand-hub", "--max-nodes", "2", "--payer", PAYER,
        "--network", "ethereum-mainnet", "--seed", SEED,
    ), backend)
    assert code == 0, body
    assert body["hub"] is True
    assert body["recipient_count"] == 201
    assert "recipient_count" in body["hub_reasons"]
    assert len(body["co_recipients"]) == 2
    assert body["truncated"] == {"reason": "node_cap", "total": 201}
    assert body["status"] == "partial"
    assert body["noise"] == "high"


def test_local_label_marks_a_hub_without_a_request(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    sources = {"nodereal-bsc": _source("nodereal_transfers", "NODEREAL_BSC_URL", "nodereal-bsc")}
    routes = [{"network": "bsc-mainnet", "capability": "history.native_external", "source_ids": ["nodereal-bsc"]}]
    backend = install(
        tmp_path, monkeypatch, networks=["bsc-mainnet"], sources=sources, routes=routes,
        secrets={"NODEREAL_BSC_URL": "https://bsc.example.invalid"},
        address=SEED, family="evm", account_networks=["bsc-mainnet"],
    )
    code, body = invoke(capsys, expand_argv(
        tmp_path / "monitor.json", "--yes", "--payer", LISTA, "--network", "bsc-mainnet", "--seed", SEED,
    ), backend)
    assert code == 0, body
    assert backend.calls == []
    assert body["hub"] is True
    assert "label" in body["hub_reasons"]
    assert "co_recipients" not in body


def test_label_hit_on_ethereum_is_a_hub(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    sources, routes, secrets = blockscout_profile(
        "ethereum-mainnet", "blockscout-eth", "BLOCKSCOUT_ETH_URL", "https://eth.blockscout.example.invalid",
    )
    backend = install(
        tmp_path, monkeypatch, networks=["ethereum-mainnet"], sources=sources, routes=routes, secrets=secrets,
        address=SEED, family="evm", account_networks=["ethereum-mainnet"],
    )

    def fake(network: str, address: str):
        if network == "ethereum-mainnet" and address.lower() == PAYER.lower():
            return {"label": "Desk Router", "label_source": "static_local_registry"}
        return None

    monkeypatch.setattr("guardian.relations.report.lookup", fake)
    fill_tails(backend, [token_item(PAYER, LEFT, 2, STAMP, tx(1))], [])
    code, body = invoke(capsys, expand_argv(
        tmp_path / "monitor.json", "--yes", "--payer", PAYER, "--network", "ethereum-mainnet", "--seed", SEED,
    ), backend)
    assert code == 0, body
    assert body["hub"] is True
    assert "label" in body["hub_reasons"]
    assert "co_recipients" not in body


def test_expand_without_yes_or_with_dry_run_sends_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    sources, routes, secrets = blockscout_profile(
        "ethereum-mainnet", "blockscout-eth", "BLOCKSCOUT_ETH_URL", "https://eth.blockscout.example.invalid",
    )
    backend = install(
        tmp_path, monkeypatch, networks=["ethereum-mainnet"], sources=sources, routes=routes, secrets=secrets,
        address=SEED, family="evm", account_networks=["ethereum-mainnet"],
    )
    code, body = invoke(capsys, expand_argv(
        tmp_path / "monitor.json", "--payer", PAYER, "--network", "ethereum-mainnet", "--seed", SEED,
    ), backend)
    assert code == 2
    assert body["status"] == "consent_required"
    assert backend.calls == []
    code, body = invoke(capsys, expand_argv(
        tmp_path / "monitor.json", "--dry-run", "--payer", PAYER, "--network", "ethereum-mainnet", "--seed", SEED,
    ), backend)
    assert code == 0
    assert body["status"] == "dry_run"
    assert backend.calls == []


def test_etherscan_expand_filters_direction_from(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    sources, routes, secrets = etherscan_profile()
    backend = install(
        tmp_path, monkeypatch, networks=["ethereum-mainnet"], sources=sources, routes=routes, secrets=secrets,
        address=SEED, family="evm", account_networks=["ethereum-mainnet"],
    )
    backend.add(action_is("txlist"), es_ok([
        es_native(PAYER, LEFT, 8, STAMP, tx(1)),
        es_native(OTHER, PAYER, 9, STAMP, tx(2)),
        es_native(PAYER, SEED, 1, STAMP, tx(3)),
    ]))
    backend.add(action_is("tokentx"), es_empty())
    backend.add(action_is("txlistinternal"), es_empty())
    code, body = invoke(capsys, expand_argv(
        tmp_path / "monitor.json", "--yes", "--payer", PAYER, "--network", "ethereum-mainnet", "--seed", SEED,
    ), backend)
    assert code == 0, body
    assert [row["address"] for row in body["co_recipients"]] == [LEFT]
    assert all(call_query(call).get("sort") == ["desc"] for call in backend.calls if "action=" in call.url)


def test_etherscan_window_drops_older_rows(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    sources, routes, secrets = etherscan_profile()
    backend = install(
        tmp_path, monkeypatch, networks=["ethereum-mainnet"], sources=sources, routes=routes, secrets=secrets,
        address=SEED, family="evm", account_networks=["ethereum-mainnet"],
    )
    backend.add(action_is("txlist"), es_ok([
        es_native(LEFT, SEED, 1, STAMP, tx(1)),
        es_native(OTHER, SEED, 1, 1, tx(2)),
    ]))
    backend.add(action_is("tokentx"), es_empty())
    backend.add(action_is("txlistinternal"), es_empty())
    code, body = invoke(capsys, inbound_argv(
        tmp_path / "monitor.json", "--yes", "--seed", SEED, "--network", "ethereum-mainnet",
    ), backend)
    assert code == 0, body
    assert body["status"] == "complete"
    assert [row["address"] for row in body["counterparties"]] == [LEFT]


def test_bounds_reject_zero_pages_and_zero_days(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    sources, routes, secrets = blockscout_profile(
        "ethereum-mainnet", "blockscout-eth", "BLOCKSCOUT_ETH_URL", "https://eth.blockscout.example.invalid",
    )
    backend = install(
        tmp_path, monkeypatch, networks=["ethereum-mainnet"], sources=sources, routes=routes, secrets=secrets,
        address=SEED, family="evm", account_networks=["ethereum-mainnet"],
    )
    code, body = invoke(capsys, inbound_argv(
        tmp_path / "monitor.json", "--yes", "--max-pages", "0", "--seed", SEED, "--network", "ethereum-mainnet",
    ), backend)
    assert code == 2
    assert body["error_type"] == "ConfigError"
    assert backend.calls == []
    code, body = invoke(capsys, inbound_argv(
        tmp_path / "monitor.json", "--yes", "--since-days", "0", "--seed", SEED, "--network", "ethereum-mainnet",
    ), backend)
    assert code == 2
    assert body["error_type"] == "ConfigError"
    assert backend.calls == []


def test_endpoint_text_does_not_hide_security_errors() -> None:
    from guardian.configuration import ConfigError
    from guardian.relations.select import endpoint_text

    class Boom:
        def get(self, _source_id: str) -> None:
            raise SecurityError("redirect_host", reason="redirect_host")

    with pytest.raises(SecurityError):
        endpoint_text(Boom(), "blockscout")

    class Missing:
        def get(self, _source_id: str) -> None:
            raise KeyError("blockscout")

    assert endpoint_text(Missing(), "blockscout") == ""

    class Bad:
        def get(self, _source_id: str) -> None:
            raise ConfigError("endpoint_missing")

    assert endpoint_text(Bad(), "blockscout") == ""


def test_tron_shaped_text_falls_back_when_tron_parse_fails() -> None:
    from guardian.configuration import ConfigError
    from guardian.relations.command import _classify

    with pytest.raises(ConfigError) as caught:
        _classify("T" + "1" * 33)
    assert str(caught.value).startswith("bad_solana")


class _WindowTransport:
    def __init__(self, page: dict) -> None:
        self.page = page
        self.tx_pages = 0

    def retry_attempts(self, _source_id: str) -> int:
        return 0

    def request(self, _source_id: str, operation):
        action = operation.query["action"]
        if action == "txlist":
            self.tx_pages += 1
            return _Result(self.page, "ok")
        return _Result({"status": "0", "message": "No transactions found", "result": []}, "empty")


def call_query(call) -> dict:
    return parse_qs(urlsplit(call.url).query)


def tron_row(sender: str, recipient: str, value: str, digest: str) -> dict:
    return {
        "block_timestamp": STAMP * 1000,
        "from": sender,
        "to": recipient,
        "token_info": {"address": TRON_TOKEN, "decimals": 6, "symbol": "SYN"},
        "transaction_id": digest,
        "type": "Transfer",
        "value": value,
    }


def tron_page(rows: list, fingerprint: str | None) -> RawResponse:
    meta = {"fingerprint": fingerprint} if fingerprint else {}
    return raw({"data": rows, "meta": meta, "success": True})


class _Pages:
    def __init__(self, pages: list) -> None:
        self.pages = list(pages)
        self.calls = []

    def retry_attempts(self, _source_id: str) -> int:
        return 0

    def request(self, _source_id: str, operation):
        self.calls.append(operation)
        page = self.pages.pop(0)
        return _Result(page)


class _Result:
    def __init__(self, body, category: str = "ok") -> None:
        self.body = body
        self.status = 200
        self.category = category
        self.ok = category in {"empty", "ok", "partial"}
        self.reason = "" if category in {"ok", "empty"} else category
        self.detail = ""
        self.retryable = False
        self.blocked = False


def _scout_client(pages: list) -> tuple[BlockscoutClient, _Pages]:
    transport = _Pages(pages)
    return BlockscoutClient(transport, "blockscout", clock=lambda: CLOCK, sleep=lambda _seconds: None, random=lambda: 0.0), transport


def _tron_types():
    import guardian.chains.tron.abi as tron_abi
    from guardian.sources.tron_grid import PAGE_CAP, TronGridClient

    if not tron_abi.READ_SELECTORS:
        raise RuntimeError("selectors")
    return PAGE_CAP, TronGridClient


def _tron_history(pages: list, page_cap: int | None):
    _page_cap, client_cls = _tron_types()
    del _page_cap
    transport = _Pages(pages)
    client = client_cls(transport, "tron-grid", clock=lambda: CLOCK, sleep=lambda _seconds: None, started_at=CLOCK, deadline=180)
    if page_cap is None:
        walk = client.history("/v1/accounts/T/transactions/trc20", {"limit": "1"}, None)
    else:
        walk = client.history("/v1/accounts/T/transactions/trc20", {"limit": "1"}, None, page_cap=page_cap)
    return walk, transport.calls
