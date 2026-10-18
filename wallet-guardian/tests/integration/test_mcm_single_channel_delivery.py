"""MCM-SHARE-01 S7. Offline Telegram handoff. No live Bot API call."""

from __future__ import annotations

import json
import os
import socket
from pathlib import Path
from urllib.parse import urlsplit

import httpx
import pytest
from eth_utils import to_checksum_address

from guardian.cli import main
from guardian.configuration import MaskedSecret, PublicConfig
from guardian.delivery import deliver, delivery_receipts, load_handoff, resolve_unknown
from guardian.errors import ConfigError, SecurityError
from guardian.identities import _b58encode
from guardian.senders.telegram import TelegramSender, WrittenTransportError, guard_telegram_send
from guardian.sources.fixtures import RawResponse
from guardian.storage import Database

ROOT = Path(__file__).resolve().parents[2]
EXAMPLE = ROOT / "examples" / "monitor.json"
TOKEN = "123456789:SYNTHETICBOTTOKEN"
TENANT = "local"
OWNER = "owner-a"
CHAT = "10001"
EVM_ID = "11111111-1111-4111-8111-111111111111"
SOL_ID = "22222222-2222-4222-8222-222222222222"
GENESIS = "5eykt4UsFv8P8NJdTREpY1vzqKqZKvdpKuc147dw2N9d"
CHAIN_HEX = {
    "ethereum.example.invalid": "0x1",
    "bsc.example.invalid": "0x38",
    "arbitrum.example.invalid": "0xa4b1",
    "base.example.invalid": "0x2105",
    "optimism.example.invalid": "0xa",
    "polygon.example.invalid": "0x89",
    "avalanche.example.invalid": "0xa86a",
}


@pytest.fixture(autouse=True)
def _block_network(monkeypatch: pytest.MonkeyPatch) -> None:
    def blocked(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("network is disabled")

    monkeypatch.setattr(socket, "socket", blocked)
    monkeypatch.setattr(socket, "create_connection", blocked)
    monkeypatch.setattr(socket, "getaddrinfo", blocked)
    monkeypatch.setattr(httpx, "Client", blocked)


class Clock:
    def __init__(self, now: int = 1_700_000_000) -> None:
        self.now = now

    def __call__(self) -> float:
        return float(self.now)


class OkBackend:
    def __init__(self) -> None:
        self.calls: list[object] = []

    def send(self, call: object) -> RawResponse:
        self.calls.append(call)
        body = json.dumps({"ok": True, "result": {"message_id": 1000 + len(self.calls)}}).encode()
        return RawResponse(200, {}, body)


class ScriptBackend:
    def __init__(self, script: list[object]) -> None:
        self.script = list(script)
        self.calls: list[object] = []

    def send(self, call: object) -> RawResponse:
        self.calls.append(call)
        item = self.script.pop(0)
        if isinstance(item, BaseException):
            raise item
        assert isinstance(item, RawResponse)
        return item


class Boom:
    tracks_inflight = True

    def __init__(self) -> None:
        self.calls = 0

    def send(self, message: dict) -> None:
        self.calls += 1
        raise AssertionError("double send")


class LocalCrash(BaseException):
    pass


def test_mcm_share_s7_01_explicit_opt_in(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    """Default outbox_only sends nothing; opt-in plus a fake transport records message_id."""
    desk = _desk(tmp_path, monkeypatch)
    sink = Boom()
    default = _cli(capsys, ["run-once", "--mcm-config", str(desk.config)], desk.backend, sink)
    assert default["delivery_status"] == "send_disabled"
    assert default["sent"] == 0
    assert default["canary"] == "NOT_RUN"
    assert sink.calls == 0
    database = Database(desk.state / "monitor.sqlite")
    pending = [row for row in database.outbox_for(TENANT) if row["status"] == "pending"]
    database.close()
    assert pending
    assert {row["status"] for row in pending} == {"pending"}

    _write_profile(tmp_path, monkeypatch, opt_in=False)
    _set_delivery(desk.config, "configured_sender")
    refused = _cli(
        capsys,
        ["run-once", "--mcm-config", str(desk.config)],
        desk.backend,
        sink,
        notification_backend=OkBackend(),
        notification_resolve=_resolve,
    )
    assert refused["delivery_status"] == "send_disabled"
    assert refused["sent"] == 0
    assert sink.calls == 0
    database = Database(desk.state / "monitor.sqlite")
    assert [row for row in database.outbox_for(TENANT) if row["status"] == "pending"]
    database.close()

    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", TOKEN)
    _write_profile(tmp_path, monkeypatch, opt_in=True)
    transport = OkBackend()
    accepted = _cli(
        capsys,
        ["run-once", "--mcm-config", str(desk.config)],
        desk.backend,
        notification_backend=transport,
        notification_resolve=_resolve,
    )
    assert accepted["delivery_status"] == "provider_accepted"
    assert accepted["sent"] >= 1
    assert accepted["canary"] == "NOT_RUN"
    assert accepted["provider_message_ids"]
    assert all(item.isdigit() for item in accepted["provider_message_ids"])
    database = Database(desk.state / "monitor.sqlite")
    try:
        rows = database.outbox_for(TENANT)
        assert [row for row in rows if row["status"] == "pending"] == []
        assert any(row["status"] == "provider_accepted" for row in rows)
        receipts = [item for item in delivery_receipts(database, TENANT) if item.get("status") == "provider_accepted"]
        assert receipts
        assert {item["message_id"] for item in receipts} == set(accepted["provider_message_ids"])
        blob = json.dumps({"accepted": accepted, "receipts": receipts}) + _sqlite_text(desk.state / "monitor.sqlite")
        _assert_token_absent(blob)
    finally:
        database.close()
    assert len(transport.calls) == accepted["sent"]
    for call in transport.calls:
        assert urlsplit(str(getattr(call, "url", ""))).hostname == "api.telegram.org"
        assert TOKEN.encode() not in getattr(call, "body", b"")
        assert b"10001" in getattr(call, "body", b"")


def test_mcm_share_s7_02_single_owner_lease(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A second owner is refused, and a slow send cannot be delivered twice."""
    database = _database(tmp_path)
    clock = Clock()
    handoff = _handoff(tmp_path, monkeypatch)
    transport = OkBackend()
    sender = _telegram(transport)
    other = Boom()

    class First:
        tracks_inflight = True

        def send(self, message: dict) -> object:
            refused = deliver(
                database, TENANT, mode="send", sender=other, handoff=handoff,
                clock=clock, owner_id="owner-b", lease_seconds=30,
            )
            self.refused = refused
            return sender.send(message)

    try:
        _alert(database, "owner-row")
        first = First()
        report = deliver(
            database, TENANT, mode="send", sender=first, handoff=handoff,
            clock=clock, owner_id=OWNER, lease_seconds=30,
        )
        assert report.status == "provider_accepted"
        assert report.sent == 1
        assert report.message_ids == ("1001",)
        assert first.refused.status == "owner_refused"
        assert first.refused.sent == 0
        assert other.calls == 0
        assert len(transport.calls) == 1
        again = deliver(
            database, TENANT, mode="send", sender=sender, handoff=handoff,
            clock=clock, owner_id="owner-b",
        )
        assert again.status == "owner_refused"
        assert again.sent == 0
        assert len(transport.calls) == 1

        _alert(database, "slow-row")
        slow_transport = OkBackend()
        inner = _telegram(slow_transport)
        boom = Boom()

        class Slow:
            tracks_inflight = True

            def send(self, message: dict) -> object:
                clock.now += 10_000
                self.nested = deliver(
                    database, TENANT, mode="send", sender=boom, handoff=handoff,
                    clock=clock, owner_id=OWNER, lease_seconds=30,
                )
                return inner.send(message)

        slow = Slow()
        overlapped = deliver(
            database, TENANT, mode="send", sender=slow, handoff=handoff,
            clock=clock, owner_id=OWNER, lease_seconds=30,
        )
        assert slow.nested.sent == 0
        assert boom.calls == 0
        assert len(slow_transport.calls) == 1
        assert overlapped.sent == 0
        slow_row = next(row for row in database.outbox_for(TENANT) if row["payload"]["dedup_key"] == "slow-row")
        assert slow_row["status"] != "pending"
        quiet = deliver(
            database, TENANT, mode="send", sender=inner, handoff=handoff,
            clock=clock, owner_id=OWNER, lease_seconds=30,
        )
        assert quiet.sent == 0
        assert len(slow_transport.calls) == 1

        low = _alert(database, "watermark-old")
        _alert(database, "watermark-new")
        marked = _handoff(tmp_path, monkeypatch, event_watermark=low)
        migrated = OkBackend()
        moved = deliver(
            database, TENANT, mode="send", sender=_telegram(migrated), handoff=marked,
            clock=clock, owner_id=OWNER,
        )
        assert moved.sent == 1
        assert len(migrated.calls) == 1
        assert b"watermark-new" in getattr(migrated.calls[0], "body", b"")
        assert b"watermark-old" not in getattr(migrated.calls[0], "body", b"")
        statuses = {row["payload"]["dedup_key"]: row["status"] for row in database.outbox_for(TENANT)}
        assert statuses["watermark-old"] == "suppressed_watermark"
        assert statuses["watermark-new"] == "provider_accepted"
    finally:
        database.close()


def test_mcm_share_s7_03_unknown_not_resent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Timeout after write and a crash after accept stay unknown across restart."""
    database = _database(tmp_path)
    clock = Clock()
    handoff = _handoff(tmp_path, monkeypatch)
    script = ScriptBackend([WrittenTransportError("timeout"), WrittenTransportError("connection_reset")])
    sender = _telegram(script)
    try:
        timeout_id = _alert(database, "timeout-row")
        reset_id = _alert(database, "reset-row")
        first = deliver(database, TENANT, mode="send", sender=sender, handoff=handoff, clock=clock, owner_id=OWNER)
        assert first.status == "unknown"
        assert first.sent == 0
        assert len(script.calls) == 2
        statuses = {row["id"]: row["status"] for row in database.outbox_for(TENANT)}
        assert statuses[timeout_id] == "unknown"
        assert statuses[reset_id] == "unknown"
        reasons = {item["outbox_id"]: item["reason"] for item in delivery_receipts(database, TENANT)}
        assert reasons[timeout_id] == "timeout_after_write"
        assert reasons[reset_id] == "connection_reset_after_write"
        restart = deliver(database, TENANT, mode="send", sender=sender, handoff=handoff, clock=clock, owner_id=OWNER)
        assert restart.sent == 0
        assert len(script.calls) == 2
        for outbox_id in (timeout_id, reset_id):
            resolved = resolve_unknown(database, TENANT, outbox_id, action="acknowledge")
            assert resolved["resent"] is False
            assert resolved["status"] == "resolved_acknowledged"
            assert resolved["canary"] == "NOT_RUN"
        with pytest.raises(ConfigError):
            resolve_unknown(database, TENANT, timeout_id, action="retry")
        after = deliver(database, TENANT, mode="send", sender=sender, handoff=handoff, clock=clock, owner_id=OWNER)
        assert after.sent == 0
        assert len(script.calls) == 2
    finally:
        database.close()

    crashed = _database(tmp_path / "crash")
    crash_transport = OkBackend()
    inner = _telegram(crash_transport)
    handoff = _handoff(tmp_path / "crash", monkeypatch)

    class CrashAfterAccept:
        tracks_inflight = True

        def send(self, message: dict) -> object:
            inner.send(message)
            raise LocalCrash("crash after accept")

    try:
        _alert(crashed, "crash-row")
        with pytest.raises(LocalCrash):
            deliver(
                crashed, TENANT, mode="send", sender=CrashAfterAccept(), handoff=handoff,
                clock=clock, owner_id=OWNER,
            )
        assert len(crash_transport.calls) == 1
        assert [row["status"] for row in crashed.outbox_for(TENANT)] == ["pending"]
        restart = deliver(
            crashed, TENANT, mode="send", sender=CrashAfterAccept(), handoff=handoff,
            clock=clock, owner_id=OWNER,
        )
        assert restart.status == "unknown"
        assert restart.sent == 0
        assert len(crash_transport.calls) == 1
        assert [row["status"] for row in crashed.outbox_for(TENANT)] == ["unknown"]
        outbox_id = int(crashed.outbox_for(TENANT)[0]["id"])
        acknowledged = resolve_unknown(crashed, TENANT, outbox_id, action="acknowledge")
        assert acknowledged["resent"] is False
        final = deliver(
            crashed, TENANT, mode="send", sender=inner, handoff=handoff, clock=clock, owner_id=OWNER,
        )
        assert final.sent == 0
        assert len(crash_transport.calls) == 1
        assert crashed.outbox_for(TENANT)[0]["status"] == "resolved_acknowledged"
    finally:
        crashed.close()


def test_mcm_share_s7_04_secret_masking_wrong_recipient(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The bot token stays out of stored output, and a missing chat is not retried."""
    with pytest.raises(SecurityError) as caught:
        guard_telegram_send(f"https://evil.example/bot{TOKEN}/sendMessage", resolve=_resolve)
    assert TOKEN not in str(caught.value)
    with pytest.raises(SecurityError) as scheme:
        guard_telegram_send(f"http://api.telegram.org/bot{TOKEN}/sendMessage", resolve=_resolve)
    assert TOKEN not in str(scheme.value)

    blocked_db = _database(tmp_path / "blocked")
    handoff = _handoff(tmp_path / "blocked", monkeypatch)
    blocked = ScriptBackend([])
    try:
        _alert(blocked_db, "private-dns")
        report = deliver(
            blocked_db, TENANT, mode="send",
            sender=_telegram(blocked, resolve=lambda _host: ["127.0.0.1"]),
            handoff=handoff, clock=Clock(), owner_id=OWNER,
        )
        assert report.sent == 0
        assert blocked.calls == []
        assert blocked_db.outbox_for(TENANT)[0]["status"] == "pending"
        _assert_token_absent(_sqlite_text(tmp_path / "blocked" / "monitor.sqlite") + json.dumps(report.as_dict()))
    finally:
        blocked_db.close()

    database = _database(tmp_path / "mask")
    handoff = _handoff(tmp_path / "mask", monkeypatch)
    clock = Clock()
    wrong = ScriptBackend([
        _response(400, {"ok": False, "error_code": 400, "description": "Bad Request: chat not found"}),
    ])
    sender = _telegram(wrong)
    try:
        missing_id = _alert(database, "missing-chat")
        failed = deliver(database, TENANT, mode="send", sender=sender, handoff=handoff, clock=clock, owner_id=OWNER)
        assert failed.status == "failed_permanent"
        assert failed.sent == 0
        assert len(wrong.calls) == 1
        assert database.outbox_for(TENANT)[0]["status"] == "failed_permanent"
        receipt = delivery_receipts(database, TENANT)[0]
        assert receipt["reason"] == "wrong_recipient"
        assert receipt["status"] == "failed_permanent"
        for _ in range(5):
            again = deliver(database, TENANT, mode="send", sender=sender, handoff=handoff, clock=clock, owner_id=OWNER)
            assert again.sent == 0
        assert len(wrong.calls) == 1

        forbidden = ScriptBackend([
            _response(403, {"ok": False, "error_code": 403, "description": "Forbidden: bot was blocked by the user"}),
        ])
        _alert(database, "forbidden-chat")
        blocked_chat = deliver(
            database, TENANT, mode="send", sender=_telegram(forbidden), handoff=handoff, clock=clock, owner_id=OWNER,
        )
        assert blocked_chat.status == "failed_permanent"
        assert len(forbidden.calls) == 1
        for _ in range(3):
            deliver(database, TENANT, mode="send", sender=_telegram(forbidden), handoff=handoff, clock=clock, owner_id=OWNER)
        assert len(forbidden.calls) == 1

        leaked = ScriptBackend([RuntimeError(f"boom {TOKEN} https://api.telegram.org/bot{TOKEN}/sendMessage")])
        _alert(database, "leaked-error")
        masked = deliver(
            database, TENANT, mode="send", sender=_telegram(leaked), handoff=handoff, clock=clock, owner_id=OWNER,
        )
        assert masked.sent == 0
        assert len(leaked.calls) == 1
        leaked_row = next(row for row in database.outbox_for(TENANT) if row["payload"]["dedup_key"] == "leaked-error")
        assert leaked_row["status"] == "unknown"
        restart = deliver(
            database, TENANT, mode="send", sender=_telegram(leaked), handoff=handoff, clock=clock, owner_id=OWNER,
        )
        assert restart.sent == 0
        assert len(leaked.calls) == 1

        rate = ScriptBackend([
            _response(
                429,
                {"ok": False, "error_code": 429, "description": "Too Many Requests", "parameters": {"retry_after": 30}},
                {"Retry-After": "30"},
            ),
            _response(200, {"ok": True, "result": {"message_id": 77}}),
        ])
        _alert(database, "rate-row")
        limited = deliver(
            database, TENANT, mode="send", sender=_telegram(rate), handoff=handoff, clock=clock, owner_id=OWNER,
        )
        assert limited.sent == 0
        assert len(rate.calls) == 1
        held = deliver(database, TENANT, mode="send", sender=_telegram(rate), handoff=handoff, clock=clock, owner_id=OWNER)
        assert held.sent == 0
        assert len(rate.calls) == 1
        clock.now += 29
        still = deliver(database, TENANT, mode="send", sender=_telegram(rate), handoff=handoff, clock=clock, owner_id=OWNER)
        assert still.sent == 0
        assert len(rate.calls) == 1
        clock.now += 1
        resumed = deliver(database, TENANT, mode="send", sender=_telegram(rate), handoff=handoff, clock=clock, owner_id=OWNER)
        assert resumed.status == "provider_accepted"
        assert resumed.message_ids == ("77",)
        assert len(rate.calls) == 2

        blob = _sqlite_text(tmp_path / "mask" / "monitor.sqlite")
        blob += json.dumps(delivery_receipts(database, TENANT))
        blob += json.dumps(failed.as_dict())
        _assert_token_absent(blob)
        assert database.conn.execute("SELECT id FROM outbox WHERE id=?", (missing_id,)).fetchone() is not None
    finally:
        database.close()


def test_mcm_share_s7_05_canary_not_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    """Canary stays NOT_RUN, and --no-send wins over an opted-in Telegram profile."""
    desk = _desk(tmp_path, monkeypatch)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", TOKEN)
    _write_profile(tmp_path, monkeypatch, opt_in=True)
    _set_delivery(desk.config, "configured_sender")
    sink = Boom()
    transport = OkBackend()
    report = _cli(
        capsys,
        ["run-once", "--mcm-config", str(desk.config), "--no-send"],
        desk.backend,
        sink,
        notification_backend=transport,
        notification_resolve=_resolve,
    )
    assert report["canary"] == "NOT_RUN"
    assert report["delivery_status"] == "no_send"
    assert report["sent"] == 0
    assert report["provider_message_ids"] == []
    assert report["live_pass"] is not True
    assert sink.calls == 0
    assert transport.calls == []
    plan = _cli(capsys, ["monitor", "plan", "--mcm-config", str(desk.config)])
    assert plan["canary"] == "NOT_RUN"
    assert plan["delivery"]["canary"] == "NOT_RUN"
    assert plan["started"] is False
    database = Database(desk.state / "monitor.sqlite")
    try:
        assert database.outbox_for(TENANT)
        assert {row["status"] for row in database.outbox_for(TENANT)} == {"pending"}
        blob = json.dumps({"plan": plan, "report": report}) + capsys.readouterr().out
        _assert_token_absent(blob + _sqlite_text(desk.state / "monitor.sqlite"))
    finally:
        database.close()


def _telegram(backend: object, *, resolve=None) -> TelegramSender:
    return TelegramSender(token=MaskedSecret(TOKEN), chat_id=CHAT, backend=backend, resolve=resolve or _resolve)


def _resolve(host: str) -> list[str]:
    if host == "api.telegram.org":
        return ["1.1.1.1"]
    return ["1.1.1.1"]


def _response(status: int, body: dict, headers: dict[str, str] | None = None) -> RawResponse:
    return RawResponse(status, headers or {}, json.dumps(body).encode())


def _database(tmp_path: Path) -> Database:
    database = Database(tmp_path / "monitor.sqlite")
    database.upsert_tenant(TENANT, TENANT)
    return database


def _alert(database: Database, key: str) -> int:
    database.add_alert(TENANT, key, {
        "dedup_key": key,
        "identity": key,
        "kind": "asset_usd",
        "network": "ethereum-mainnet",
        "wallet_id": EVM_ID,
    })
    row = database.conn.execute(
        "SELECT id FROM outbox WHERE tenant_id=? ORDER BY id DESC LIMIT 1",
        (TENANT,),
    ).fetchone()
    return int(row["id"])


def _handoff(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, **overrides: object) -> object:
    _write_profile(tmp_path, monkeypatch, **overrides)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", TOKEN)
    public = PublicConfig(
        document={"delivery": {"mode": "configured_sender", "profile_env": "MONITOR_DELIVERY_PROFILE"}},
        path=tmp_path / "monitor.json",
    )
    return load_handoff(public, os.environ)


def _write_profile(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, **overrides: object) -> None:
    body: dict[str, object] = {
        "schema_version": "guardian.delivery-profile.v1",
        "channel": "telegram",
        "opt_in": True,
        "owner_id": OWNER,
        "chat_id": CHAT,
        "token_env": "TELEGRAM_BOT_TOKEN",
        "single_sender_handoff": True,
        "legacy_shadow": False,
        "event_watermark": None,
    }
    body.update(overrides)
    path = tmp_path / "delivery.json"
    _write_private(path, json.dumps(body) + "\n")
    monkeypatch.setenv("MONITOR_DELIVERY_PROFILE", str(path))


def _set_delivery(config: Path, mode: str) -> None:
    document = json.loads(config.read_text(encoding="utf-8"))
    document["delivery"] = {"mode": mode, "profile_env": "MONITOR_DELIVERY_PROFILE"}
    config.write_text(json.dumps(document), encoding="utf-8")


def _assert_token_absent(blob: str) -> None:
    assert TOKEN not in blob
    assert "SYNTHETICBOTTOKEN" not in blob


def _sqlite_text(path: Path) -> str:
    return path.read_bytes().decode("utf-8", errors="replace")


def _cli(
    capsys: pytest.CaptureFixture[str],
    argv: list[str],
    backend: FixtureRpc | None = None,
    sender: object | None = None,
    *,
    notification_backend: object | None = None,
    notification_resolve: object | None = None,
) -> dict:
    code = main(
        argv,
        backend=backend,
        sender=sender,
        notification_backend=notification_backend,
        notification_resolve=notification_resolve,
    )
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert code == 0, payload
    return payload


class FixtureRpc:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def send(self, call: object) -> RawResponse:
        body = getattr(call, "body", b"") or b""
        payload = json.loads(body.decode("utf-8")) if body else {}
        method = payload.get("method") if isinstance(payload, dict) else None
        host = urlsplit(str(getattr(call, "url", ""))).hostname or ""
        self.calls.append(str(method))
        result = _rpc_result(str(method), host)
        encoded = json.dumps({"id": payload.get("id", 1) if isinstance(payload, dict) else 1, "jsonrpc": "2.0", "result": result}).encode()
        return RawResponse(200, {}, encoded)


def _desk(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> _Desk:
    config = tmp_path / "monitor.json"
    document = json.loads(EXAMPLE.read_text(encoding="utf-8"))
    # This delivery fixture serves RPC only. Keep history unavailable instead
    # of resolving the public keyless fallback or inventing indexer responses.
    document["sources"]["routescan"]["enabled"] = False
    config.write_text(json.dumps(document), encoding="utf-8")
    evm_address = to_checksum_address("0x" + ("0" * 39) + "1")
    sol_address = _b58encode(bytes([2]) * 32)
    accounts = {
        "schema_version": "guardian.accounts.v1",
        "tenant_id": TENANT,
        "accounts": [
            {
                "wallet_id": EVM_ID,
                "label": "evm-desk",
                "family": "evm",
                "address": evm_address,
                "networks": ["ethereum-mainnet"],
                "relationship": "owned_user_declared",
                "own_group": "desk",
            },
            {
                "wallet_id": SOL_ID,
                "label": "sol-desk",
                "family": "solana",
                "address": sol_address,
                "networks": ["solana-mainnet"],
                "relationship": "owned_user_declared",
                "own_group": "desk",
            },
        ],
    }
    secrets = {
        "COINGECKO_API_BASE": "https://prices.example.invalid/api",
        "COINGECKO_API_KEY": "synthetic-secret-value-9f3a7c",
        "ETHERSCAN_API_BASE": "https://etherscan.example.invalid/v2",
        "GOPLUS_API_BASE": "https://goplus.example.invalid/api",
        "HELIUS_PARSED_EVENTS_URL": "https://helius.example.invalid/events",
        "OFAC_SLS_FEED": "https://ofac.example.invalid/feed",
        "RPC_ARBITRUM_ONE_URL": "https://arbitrum.example.invalid/rpc",
        "RPC_AVALANCHE_C_URL": "https://avalanche.example.invalid/rpc",
        "RPC_BASE_MAINNET_URL": "https://base.example.invalid/rpc",
        "RPC_BSC_MAINNET_URL": "https://bsc.example.invalid/rpc",
        "RPC_ETHEREUM_MAINNET_URL": "https://ethereum.example.invalid/rpc",
        "RPC_OPTIMISM_MAINNET_URL": "https://optimism.example.invalid/rpc",
        "RPC_POLYGON_POS_URL": "https://polygon.example.invalid/rpc",
        "RPC_SOLANA_MAINNET_URL": "https://solana.example.invalid/rpc",
    }
    accounts_path = tmp_path / "accounts.json"
    secret_path = tmp_path / "secrets.json"
    state = tmp_path / "state"
    _write_private(accounts_path, json.dumps(accounts) + "\n")
    _write_private(secret_path, json.dumps(secrets) + "\n")
    monkeypatch.setenv("MONITOR_ACCOUNTS_FILE", str(accounts_path))
    monkeypatch.setenv("MONITOR_SECRET_PROFILE", str(secret_path))
    monkeypatch.setenv("MONITOR_STATE_DIR", str(state))
    for name in ("ETHERSCAN_API_KEY", "GOPLUS_API_KEY", "CHAINABUSE_API_KEY", "HELIUS_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    return _Desk(config, state, FixtureRpc())


class _Desk:
    def __init__(self, config: Path, state: Path, backend: FixtureRpc) -> None:
        self.config = config
        self.state = state
        self.backend = backend


def _write_private(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)


def _rpc_result(method: str, host: str) -> object:
    if method == "eth_chainId":
        return CHAIN_HEX.get(host, "0x1")
    if method == "eth_blockNumber":
        return "0x10"
    if method == "eth_getBalance":
        return "0x1"
    if method == "eth_getLogs":
        return []
    if method == "getGenesisHash":
        return GENESIS
    if method == "getBalance":
        return {"context": {"slot": 1}, "value": 1}
    if method == "getTokenAccountsByOwner":
        return {"value": []}
    if method == "getSignaturesForAddress":
        return []
    return []
