"""Offline acceptance for MCM-W1. Synthetic addresses only. No network."""

from __future__ import annotations

import ast
import base64
import json
import os
import socket
import sqlite3
import stat
from pathlib import Path

import pytest
from eth_utils import to_checksum_address

from guardian.configuration import (
    ConfigError,
    MaskedSecret,
    load_accounts,
    load_public_config,
    resolve_private_inputs,
    safe_export,
)
from guardian.identities import (
    IdentityError,
    WalletIdentity,
    _b58check_encode,
    _crc16_xmodem,
    index_wallets,
    parse_address,
)
from guardian.jsonschema_lite import SchemaError, ValidationError, validate
from guardian.legacy_state import import_legacy_state, is_legacy_alerted
from guardian.monitoring import SnapshotCycle, event_key
from guardian.storage import SCHEMA, Database

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src" / "guardian"
WALLET_A = "11111111-1111-4111-8111-111111111111"
WALLET_B = "22222222-2222-4222-8222-222222222222"
WALLET_C = "33333333-3333-4333-8333-333333333333"
ADDR_A = "0x0000000000000000000000000000000000000001"
ADDR_B = "0x0000000000000000000000000000000000000002"
SOL_ZERO = "11111111111111111111111111111111"
SOL_TOKEN = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
P2WPKH = "bc1qw508d6qejxtdg4y5r3zarvary0c5xw7kv8f3t4"
P2WSH = "bc1qrp33g0q5c5txsp9arysrx4k6zdkfs4nce4xj0gdcccefvpysxf3qccfmv3"
P2TR = "bc1p0xlxvlhemja6c4dqv22uapctqupfhlxm9h8z3k2e72q4k9hcz7vqzk5jj0"
P2TR_BECH32 = "bc1p0xlxvlhemja6c4dqv22uapctqupfhlxm9h8z3k2e72q4k9hcz7vqh2y7hd"
P2WPKH_BAD = "bc1qw508d6qejxtdg4y5r3zarvary0c5xw7kv8f3t5"
P2WPKH_TESTNET = "tb1qw508d6qejxtdg4y5r3zarvary0c5xw7kxpjzsx"
P2PKH = "1A1zP1eP5QGefi2DMPTfTL5SLmv7DivfNa"
P2SH = "31h1vYVSYuKP6AhS86fbRdMw9XHieotbST"
P2PKH_TESTNET = "mfWxJ45yp2SFn7UciZyNpvDKrzbhyfKrY8"
TRON = "T9yD14Nj9j7xAB4dbGeiX9h8unkKHxuWwb"
TRON_HEX = "410000000000000000000000000000000000000000"
COSMOS_ZERO = "cosmos1qqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqnrql8a"
OSMO_ZERO = "osmo1qqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqmcn030"
EIP55 = "0x5aAeb6053F3E94C9b9A09f33669435E7Ef1BeAed"
SECRET = "synthetic-secret-value-9f3a7c"
ENDPOINT = "https://rpc.example.invalid/synthetic-endpoint-9f3a7c"


@pytest.fixture(autouse=True)
def _block_network(monkeypatch):
    def blocked(*_args, **_kwargs):
        raise AssertionError("network is disabled")

    monkeypatch.setattr(socket, "socket", blocked)
    monkeypatch.setattr(socket, "create_connection", blocked)


def _checksum(text: str) -> str:
    return to_checksum_address(text)


def _accounts_doc(accounts: list[dict], tenant: str = "local") -> dict:
    return {"schema_version": "guardian.accounts.v1", "tenant_id": tenant, "accounts": accounts}


def _account(wallet_id: str, label: str, family: str, address: str, networks: list[str]) -> dict:
    return {
        "wallet_id": wallet_id,
        "label": label,
        "family": family,
        "address": address,
        "networks": networks,
        "relationship": "watch_only",
    }


def _write_json(path: Path, document: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document), encoding="utf-8")


def _own(environ: dict[str, str]) -> dict[str, str]:
    if os.name == "posix":
        os.chmod(environ["MONITOR_ACCOUNTS_FILE"], 0o600)
        os.chmod(environ["MONITOR_SECRET_PROFILE"], 0o600)
    return environ


def _imported_roots(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            found.add(node.module.split(".")[0])
    return found


def _public_copy(tmp_path: Path, mutate=None) -> Path:
    document = json.loads((ROOT / "examples" / "monitor.json").read_text(encoding="utf-8"))
    if mutate is not None:
        mutate(document)
    path = tmp_path / "monitor.json"
    _write_json(path, document)
    return path


def _private_files(tmp_path: Path, accounts: dict, profile: dict) -> dict[str, str]:
    accounts_path = tmp_path / "accounts.json"
    secret_path = tmp_path / "secrets.json"
    state_dir = tmp_path / "state"
    _write_json(accounts_path, accounts)
    _write_json(secret_path, profile)
    return {
        "MONITOR_ACCOUNTS_FILE": str(accounts_path),
        "MONITOR_SECRET_PROFILE": str(secret_path),
        "MONITOR_STATE_DIR": str(state_dir),
    }


def _ton_friendly(flags: int, workchain: int, account: bytes, *, url_safe: bool = False) -> str:
    body = bytes([flags, workchain & 0xFF]) + account
    raw = body + _crc16_xmodem(body).to_bytes(2, "big")
    text = base64.b64encode(raw).decode("ascii")
    if url_safe:
        text = text.replace("+", "-").replace("/", "_")
    return text


def _claim(position: str, address: str = ADDR_A) -> dict:
    return {
        "position_id": position,
        "wallet_label": "A",
        "wallet_address": address,
        "chain_id": 56,
        "raw_amount": "5",
        "status": "CLAIMABLE",
        "quality": "COMPLETE",
        "fresh": True,
        "paused": False,
    }


def test_mcm_w1_01_public_example_has_no_secrets():
    text = (ROOT / "examples" / "monitor.json").read_text(encoding="utf-8")
    lowered = text.lower()
    assert "0x" not in lowered
    assert "glpat-" not in lowered
    assert "apikey=" not in lowered
    assert "private_key" not in lowered
    assert "mnemonic" not in lowered
    empty = json.loads((ROOT / "examples" / "accounts.empty.template.json").read_text(encoding="utf-8"))
    assert empty == {"schema_version": "guardian.accounts.v1", "tenant_id": "local", "accounts": []}
    config = load_public_config(ROOT / "examples" / "monitor.json")
    assert config.document["schema_version"] == "guardian.monitor.v1"
    assert config.document["mode"] == "read_only"
    assert config.document["execution"] == {
        "sign": False,
        "broadcast": False,
        "auto_purchase": False,
        "auto_disable_schedule": False,
    }
    assert "ethereum-mainnet" in config.document["enabled_networks"]
    assert config.document["sources"]["etherscan"]["adapter"] == "etherscan_v2"
    assert config.document["sources"]["etherscan"]["credential_env"] == "ETHERSCAN_API_KEY"
    accounts = load_accounts(ROOT / "examples" / "accounts.empty.template.json")
    assert accounts.accounts == ()
    banned = {"httpx", "urllib", "socket", "requests"}
    for name in ("configuration.py", "identities.py", "jsonschema_lite.py", "legacy_state.py", "storage.py"):
        assert _imported_roots(SRC / name).isdisjoint(banned)


def test_mcm_w1_01_rejects_secret_fields_and_values(tmp_path):
    for field in ("address", "api_key", "private_key", "state", "not_a_real_field"):
        path = _public_copy(tmp_path / field, lambda document, field=field: document.__setitem__(field, "synthetic"))
        with pytest.raises(ConfigError, match=field):
            load_public_config(path)

    samples = {
        "url": "https://rpc.example/v1/?apikey=abcdef",
        "token": "glpat-aaaaaaaaaaaa",
        "evm": ADDR_A,
        "hex": "11" * 20,
    }
    for name, value in samples.items():
        path = _public_copy(
            tmp_path / name,
            lambda document, value=value: document["routing"][0].__setitem__("capability", value),
        )
        with pytest.raises(ConfigError, match="forbidden value"):
            load_public_config(path)


def test_mcm_w1_01_private_paths_must_live_outside_the_repo(tmp_path):
    outside = _private_files(tmp_path / "ok", _accounts_doc([]), {})
    inside_accounts = dict(outside)
    inside_accounts["MONITOR_ACCOUNTS_FILE"] = str(ROOT / "examples" / "accounts.empty.template.json")
    config = load_public_config(ROOT / "examples" / "monitor.json")
    with pytest.raises(ConfigError, match="accounts path is inside the git work tree"):
        resolve_private_inputs(config, inside_accounts)
    inside_state = dict(outside)
    inside_state["MONITOR_STATE_DIR"] = str(ROOT / "data" / "mcm-w1-state-must-not-exist")
    with pytest.raises(ConfigError, match="state path is inside the git work tree"):
        resolve_private_inputs(config, inside_state)
    assert not (ROOT / "data" / "mcm-w1-state-must-not-exist").exists()
    inside_secret = dict(outside)
    inside_secret["MONITOR_SECRET_PROFILE"] = str(ROOT / "examples" / "monitor.json")
    with pytest.raises(ConfigError, match="secret path is inside the git work tree"):
        resolve_private_inputs(config, inside_secret)
    resolved = resolve_private_inputs(config, _own(outside))
    assert resolved.accounts.accounts == ()
    assert resolved.state_dir.is_dir()
    assert resolved.source_status["etherscan"].blocked_reason == "credential_missing"
    assert resolved.source_status["rpc-ethereum-mainnet"].availability == "not_required"
    if os.name != "posix":
        assert "windows_permission_bits_not_enforced" in resolved.notes


def test_mcm_w1_01_posix_file_mode_refuses_0644_accepts_0600(tmp_path, monkeypatch):
    environ = _private_files(tmp_path, _accounts_doc([]), {"ETHERSCAN_API_KEY": SECRET})
    config = load_public_config(ROOT / "examples" / "monitor.json")

    def bits(_path, mode):
        return mode

    monkeypatch.setattr(
        "guardian.configuration.file_permission_bits",
        lambda _path: 0o644,
    )
    with pytest.raises(ConfigError, match="group or world readable"):
        resolve_private_inputs(config, environ)
    monkeypatch.setattr(
        "guardian.configuration.file_permission_bits",
        lambda _path: 0o600,
    )
    accepted = resolve_private_inputs(config, environ)
    assert accepted.source_status["etherscan"].credential_present is True
    assert SECRET not in repr(accepted)

    if os.name != "posix":
        return
    monkeypatch.undo()
    accounts_path = Path(environ["MONITOR_ACCOUNTS_FILE"])
    secret_path = Path(environ["MONITOR_SECRET_PROFILE"])
    os.chmod(accounts_path, 0o644)
    os.chmod(secret_path, 0o600)
    with pytest.raises(ConfigError, match="group or world readable"):
        resolve_private_inputs(config, environ)
    os.chmod(accounts_path, 0o600)
    os.chmod(secret_path, 0o600)
    assert stat.S_IMODE(accounts_path.stat().st_mode) == 0o600
    live = resolve_private_inputs(config, environ)
    assert live.source_status["etherscan"].availability == "available"
    assert stat.S_IMODE(live.state_dir.stat().st_mode) & 0o077 == 0


def test_mcm_w1_01_secret_values_stay_out_of_repr_and_errors(tmp_path):
    wrapped = MaskedSecret(SECRET)
    assert SECRET not in repr(wrapped)
    assert SECRET not in str(wrapped)
    assert SECRET not in format(wrapped)
    accounts = _accounts_doc([])
    bad_secret = tmp_path / "bad-secrets.json"
    bad_secret.write_text("{not-json " + SECRET, encoding="utf-8")
    accounts_path = tmp_path / "accounts.json"
    _write_json(accounts_path, accounts)
    if os.name == "posix":
        os.chmod(accounts_path, 0o600)
        os.chmod(bad_secret, 0o600)
    environ = {
        "MONITOR_ACCOUNTS_FILE": str(accounts_path),
        "MONITOR_SECRET_PROFILE": str(bad_secret),
        "MONITOR_STATE_DIR": str(tmp_path / "state"),
    }
    config = load_public_config(ROOT / "examples" / "monitor.json")
    with pytest.raises(ConfigError) as caught:
        resolve_private_inputs(config, environ)
    assert SECRET not in str(caught.value)
    assert "not valid json" in str(caught.value)
    good = _own(_private_files(tmp_path / "good", accounts, {"ETHERSCAN_API_KEY": SECRET, "RPC_ETHEREUM_MAINNET_URL": ENDPOINT}))
    resolved = resolve_private_inputs(config, good)
    blob = repr(resolved) + str(resolved) + repr(resolved.source_status)
    assert SECRET not in blob
    assert ENDPOINT not in blob
    assert resolved.source_status["etherscan"].availability == "available"
    assert resolved.source_status["goplus"].blocked_reason == "credential_missing"


def test_mcm_w1_01_validator_fail_closed():
    example = json.loads((ROOT / "examples" / "monitor.json").read_text(encoding="utf-8"))
    schema = json.loads((ROOT / "schemas" / "monitor.schema.json").read_text(encoding="utf-8"))
    validate(example, schema)
    validate(0.2, {"type": "number", "exclusiveMinimum": 0, "maximum": 1000})
    with pytest.raises(ValidationError):
        validate(0, {"type": "number", "exclusiveMinimum": 0})
    with pytest.raises(ValidationError):
        validate(True, {"type": "integer"})
    with pytest.raises(SchemaError, match="unsupported keyword"):
        validate({}, {"type": "object", "not": {}})
    with pytest.raises(SchemaError, match="unsupported format"):
        validate("x", {"type": "string", "format": "email"})
    referenced = {
        "$defs": {"name": {"type": "string", "minLength": 2}},
        "type": "object",
        "properties": {"name": {"$ref": "#/$defs/name"}},
        "required": ["name"],
        "additionalProperties": False,
    }
    validate({"name": "ab"}, referenced)
    with pytest.raises(ValidationError):
        validate({"name": "a"}, referenced)
    coverage = json.loads((ROOT / "schemas" / "coverage.schema.json").read_text(encoding="utf-8"))
    row = {
        "schema_version": "guardian.coverage.v1",
        "wallet_id": "synthetic",
        "network": "ethereum-mainnet",
        "chain_uid": "eip155:1",
        "capability": "balances.native",
        "adapter_version": "none",
        "support": "unsupported",
        "probe": "untested",
        "quality": "unknown",
        "scope": {
            "declared": "none",
            "from": None,
            "to": None,
            "pagination_complete": False,
            "gap_detection_complete": False,
            "gaps": [],
            "cursor": None,
        },
        "checkpoint": {"kind": "block", "number": None, "hash": None, "status": "unknown"},
        "last_success_at": None,
        "valid_until": "2026-10-08T00:00:00Z",
        "sources": [],
        "trust": "unverified",
        "price_quality": "unknown",
        "reason_codes": ["not_run"],
        "evidence_refs": [],
    }
    validate(row, coverage)
    row["support"] = "complete"
    with pytest.raises(ValidationError):
        validate(row, coverage)


def test_mcm_w1_02_evm_checksum():
    parsed = parse_address("evm", EIP55)
    assert parsed.canonical == EIP55
    assert parsed.identity_key == EIP55.lower()
    assert parsed.display == EIP55
    lowered = parse_address("evm", EIP55.lower())
    assert lowered.canonical == EIP55
    assert lowered.identity_key == parsed.identity_key
    upper = parse_address("evm", EIP55.upper().replace("0X", "0x"))
    assert upper.identity_key == parsed.identity_key
    synthetic = parse_address("evm", ADDR_A)
    assert synthetic.identity_key == ADDR_A
    assert synthetic.canonical == _checksum(ADDR_A)
    bad = "0x5aaeb6053F3E94C9b9A09f33669435E7Ef1BeAed"
    with pytest.raises(IdentityError) as caught:
        parse_address("evm", bad)
    assert caught.value.reason == "bad_evm_checksum"
    with pytest.raises(IdentityError) as caught:
        parse_address("evm", "0x" + "ab" * 32)
    assert caught.value.reason == "bad_evm_format"


def test_mcm_w1_02_solana_base58():
    zero = parse_address("solana", SOL_ZERO)
    assert zero.identity_key == SOL_ZERO
    assert len(zero.canonical) == len(SOL_ZERO)
    token = parse_address("solana", SOL_TOKEN)
    assert token.identity_key == SOL_TOKEN
    assert token.identity_key != zero.identity_key
    with pytest.raises(IdentityError) as caught:
        parse_address("solana", SOL_TOKEN + "1")
    assert caught.value.reason == "bad_solana_length"
    with pytest.raises(IdentityError) as caught:
        parse_address("solana", "0OIl")
    assert caught.value.reason == "bad_solana_encoding"
    with pytest.raises(IdentityError) as caught:
        parse_address("solana", ADDR_A)
    assert caught.value.reason == "bad_solana_encoding"


def test_mcm_w1_02_bitcoin_mainnet():
    first = parse_address("bitcoin", P2WPKH)
    second = parse_address("bitcoin", P2WPKH.upper())
    assert first.identity_key == second.identity_key == P2WPKH
    assert parse_address("bitcoin", P2WSH).identity_key == P2WSH
    taproot = parse_address("bitcoin", P2TR)
    assert taproot.identity_key == P2TR
    assert taproot.identity_key != first.identity_key
    p2pkh = parse_address("bitcoin", P2PKH)
    assert p2pkh.canonical == P2PKH
    p2sh = parse_address("bitcoin", P2SH)
    assert p2sh.canonical == P2SH
    assert p2sh.identity_key != p2pkh.identity_key
    with pytest.raises(IdentityError) as caught:
        parse_address("bitcoin", P2WPKH_BAD)
    assert caught.value.reason == "bad_bitcoin_checksum"
    with pytest.raises(IdentityError) as caught:
        parse_address("bitcoin", P2PKH[:-1] + "b")
    assert caught.value.reason == "bad_bitcoin_checksum"
    with pytest.raises(IdentityError) as caught:
        parse_address("bitcoin", P2TR_BECH32)
    assert caught.value.reason == "bad_bitcoin_witness"
    with pytest.raises(IdentityError) as caught:
        parse_address("bitcoin", P2WPKH_TESTNET)
    assert caught.value.reason == "bad_bitcoin_hrp"
    with pytest.raises(IdentityError) as caught:
        parse_address("bitcoin", P2PKH_TESTNET)
    assert caught.value.reason == "bad_bitcoin_format"
    wrong_length = _b58check_encode(bytes([0x00]) + bytes(19))
    assert wrong_length.startswith(("1", "3"))
    with pytest.raises(IdentityError) as caught:
        parse_address("bitcoin", wrong_length)
    assert caught.value.reason == "bad_bitcoin_version"


def test_mcm_w1_02_tron_base58_and_hex():
    base58 = parse_address("tron", TRON)
    hex_form = parse_address("tron", TRON_HEX)
    prefixed = parse_address("tron", "0x" + TRON_HEX)
    assert base58.identity_key == hex_form.identity_key == prefixed.identity_key == TRON_HEX.lower()
    assert base58.canonical == hex_form.canonical == TRON
    with pytest.raises(IdentityError) as caught:
        parse_address("tron", TRON[:-1] + "a")
    assert caught.value.reason == "bad_tron_checksum"
    with pytest.raises(IdentityError) as caught:
        parse_address("tron", ADDR_A)
    assert caught.value.reason == "bad_tron_format"


def test_mcm_w1_02_ton_forms_share_one_identity(tmp_path):
    assert _crc16_xmodem(b"123456789") == 0x31C3
    account = bytes([10]) * 32
    raw = "0:" + account.hex()
    bounceable = _ton_friendly(0x11, 0, account)
    quiet = _ton_friendly(0x51, 0, account)
    url_safe = _ton_friendly(0x11, 0, account, url_safe=True)
    assert "/" in bounceable or "+" in bounceable
    assert url_safe != bounceable
    forms = [raw, bounceable, quiet, url_safe]
    keys = {parse_address("ton", form).identity_key for form in forms}
    assert keys == {raw}
    assert all(parse_address("ton", form).canonical == raw for form in forms)
    master = bytes([19]) * 32
    master_raw = "-1:" + master.hex()
    master_friendly = _ton_friendly(0x11, -1, master)
    assert parse_address("ton", master_friendly).identity_key == master_raw
    broken = bounceable[:-1] + ("A" if bounceable[-1] != "A" else "B")
    with pytest.raises(IdentityError) as caught:
        parse_address("ton", broken)
    assert caught.value.reason in {"bad_ton_crc", "bad_ton_format"}
    document = _accounts_doc([
        _account(WALLET_A, "ton-a", "ton", bounceable, ["ton-mainnet"]),
        _account(WALLET_B, "ton-b", "ton", quiet, ["ton-mainnet"]),
    ])
    path = tmp_path / "accounts.json"
    _write_json(path, document)
    with pytest.raises(ConfigError, match="duplicate_identity"):
        load_accounts(path)


def test_mcm_w1_02_move_padding_does_not_infer_evm():
    evm = parse_address("evm", ADDR_A)
    sui = parse_address("sui", ADDR_A)
    aptos = parse_address("aptos", ADDR_A)
    assert evm.family == "evm"
    assert sui.family == "sui"
    assert aptos.family == "aptos"
    assert evm.identity_key == ADDR_A
    assert sui.identity_key == "0x" + "0" * 24 + ADDR_A[2:]
    assert sui.identity_key != evm.identity_key
    assert aptos.identity_key == sui.identity_key
    assert parse_address("aptos", "0x1").canonical == "0x" + "0" * 63 + "1"
    assert parse_address("sui", "0xAb").canonical == "0x" + "0" * 62 + "ab"
    with pytest.raises(IdentityError) as caught:
        parse_address("evm", sui.canonical)
    assert caught.value.reason == "bad_evm_format"
    with pytest.raises(IdentityError) as caught:
        parse_address("sui", "not-an-address")
    assert caught.value.reason == "bad_move_hex"


def test_mcm_w1_02_cosmos_hrp_per_network():
    hub = parse_address("cosmos", COSMOS_ZERO, "cosmos-hub")
    osmosis = parse_address("cosmos", OSMO_ZERO, "osmosis-mainnet")
    assert hub.identity_key == COSMOS_ZERO
    assert osmosis.identity_key == OSMO_ZERO
    assert hub.identity_key != osmosis.identity_key
    with pytest.raises(IdentityError) as caught:
        parse_address("cosmos", OSMO_ZERO, "cosmos-hub")
    assert caught.value.reason == "bad_cosmos_hrp"
    with pytest.raises(IdentityError) as caught:
        parse_address("cosmos", COSMOS_ZERO, "osmosis-mainnet")
    assert caught.value.reason == "bad_cosmos_hrp"
    with pytest.raises(IdentityError) as caught:
        parse_address("cosmos", COSMOS_ZERO)
    assert caught.value.reason == "cosmos_network_required"
    with pytest.raises(IdentityError) as caught:
        parse_address("cosmos", COSMOS_ZERO[:-1] + "q", "cosmos-hub")
    assert caught.value.reason == "bad_cosmos_checksum"


def test_mcm_w1_02_cross_family_rejected():
    cases = [
        ("solana", ADDR_A, None, "bad_solana_encoding"),
        ("evm", SOL_ZERO, None, "bad_evm_format"),
        ("evm", TRON, None, "bad_evm_format"),
        ("bitcoin", TRON, None, "bad_bitcoin_format"),
        ("tron", P2PKH, None, "bad_tron_format"),
        ("cosmos", COSMOS_ZERO, "ethereum-mainnet", "unknown_cosmos_network"),
        ("cardano", ADDR_A, None, "unknown_family"),
    ]
    for family, text, network, reason in cases:
        with pytest.raises(IdentityError) as caught:
            parse_address(family, text, network)
        assert caught.value.reason == reason


def test_mcm_w1_02_same_label_does_not_overwrite(tmp_path):
    left = parse_address("evm", ADDR_A)
    right = parse_address("evm", ADDR_B)
    wallets = index_wallets([
        WalletIdentity(WALLET_A, "same-label", left),
        WalletIdentity(WALLET_B, "same-label", right),
    ])
    assert set(wallets) == {WALLET_A, WALLET_B}
    assert wallets[WALLET_A].label == wallets[WALLET_B].label == "same-label"
    assert wallets[WALLET_A].identity_key != wallets[WALLET_B].identity_key
    with pytest.raises(IdentityError) as caught:
        index_wallets([
            WalletIdentity(WALLET_A, "one", left),
            WalletIdentity(WALLET_B, "two", left),
        ])
    assert caught.value.reason == "duplicate_identity"
    path = tmp_path / "accounts.json"
    _write_json(path, _accounts_doc([
        _account(WALLET_A, "same-label", "evm", _checksum(ADDR_A), ["ethereum-mainnet"]),
        _account(WALLET_B, "same-label", "evm", _checksum(ADDR_B), ["bsc-mainnet"]),
    ]))
    loaded = load_accounts(path)
    assert [row.wallet_id for row in loaded.accounts] == [WALLET_A, WALLET_B]
    assert {row.label for row in loaded.accounts} == {"same-label"}
    _write_json(path, _accounts_doc([
        _account(WALLET_A, "one", "evm", _checksum(ADDR_A), ["ethereum-mainnet"]),
        _account(WALLET_B, "two", "evm", ADDR_A, ["ethereum-mainnet"]),
    ]))
    with pytest.raises(ConfigError, match="duplicate_identity"):
        load_accounts(path)


def test_mcm_w1_03_state_migration_preserves_rows_and_reopen_is_idempotent(tmp_path):
    path = tmp_path / "v1.sqlite"
    payload = '{"position_id":"oldlabel:bnb:native","raw_amount":"5"}'
    alert = '{"dedup_key":"kept-alert"}'
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA)
    conn.execute("PRAGMA user_version=1")
    conn.execute("INSERT INTO snapshots(generation, tenant_id, created_at) VALUES (1, 'local', '2020-01-01T00:00:00Z')")
    conn.execute(
        "INSERT INTO snapshot_items(generation, tenant_id, position_id, payload) VALUES (1, 'local', 'oldlabel:bnb:native', ?)",
        (payload,),
    )
    conn.execute("INSERT INTO alerts(tenant_id, dedup_key, payload) VALUES ('local', 'kept-alert', ?)", (alert,))
    conn.execute("INSERT INTO alert_dedup(tenant_id, dedup_key) VALUES ('local', 'kept-alert')")
    conn.execute("INSERT INTO outbox(tenant_id, status, payload) VALUES ('local', 'pending', ?)", (alert,))
    conn.execute("INSERT INTO cursors(scope, tenant_id, value) VALUES ('kept', 'local', '{\"n\":1}')")
    conn.commit()
    before = _dump(path)
    conn.close()
    db = Database(path)
    try:
        assert db.conn.execute("PRAGMA user_version").fetchone()[0] == 1
        assert "mcm_schema_migrations" in db.table_names()
        assert "wallet_identities" in db.table_names()
        recorded = db.conn.execute(
            "SELECT migration_id, sha256, applied_at FROM mcm_schema_migrations"
        ).fetchall()
        assert [row["migration_id"] for row in recorded] == ["0001_mcm_identity"]
        applied_at = recorded[0]["applied_at"]
    finally:
        db.close()
    assert _dump(path) == before
    db = Database(path)
    try:
        again = db.conn.execute(
            "SELECT migration_id, sha256, applied_at FROM mcm_schema_migrations"
        ).fetchone()
        assert again["applied_at"] == applied_at
        assert db.conn.execute("SELECT COUNT(*) FROM mcm_schema_migrations").fetchone()[0] == 1
    finally:
        db.close()
    conn = sqlite3.connect(path)
    conn.execute("UPDATE mcm_schema_migrations SET sha256=?", ("0" * 64,))
    conn.commit()
    conn.close()
    with pytest.raises(ConfigError, match="sha256 changed"):
        Database(path)
    conn = sqlite3.connect(path)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 1
    assert conn.execute("SELECT payload FROM snapshot_items").fetchone()[0] == payload
    assert conn.execute("SELECT status FROM outbox").fetchone()[0] == "pending"
    conn.close()


def test_mcm_w1_03_label_rename_keeps_wallet_id(tmp_path):
    db = Database(tmp_path / "names.sqlite")
    try:
        identity = parse_address("evm", ADDR_A).identity_key
        other = parse_address("evm", ADDR_B).identity_key
        db.upsert_wallet_identity("local", WALLET_A, "evm", identity, "oldlabel")
        created = db.conn.execute("SELECT created_at FROM wallet_identities WHERE wallet_id=?", (WALLET_A,)).fetchone()[0]
        db.upsert_wallet_identity("local", WALLET_A, "evm", identity, "newlabel")
        assert db.conn.execute("SELECT created_at FROM wallet_identities WHERE wallet_id=?", (WALLET_A,)).fetchone()[0] == created
        assert db.resolve_label("local", "oldlabel") == WALLET_A
        assert db.resolve_label("local", "newlabel") == WALLET_A
        position_id = "oldlabel:bnb:native"
        assert db.resolve_label("local", position_id.split(":", 1)[0]) == WALLET_A
        with pytest.raises(IdentityError) as caught:
            db.upsert_wallet_identity("local", WALLET_A, "evm", other, "newlabel")
        assert caught.value.reason == "identity_rebind"
        assert db.conn.execute(
            "SELECT identity_key FROM wallet_identities WHERE wallet_id=?",
            (WALLET_A,),
        ).fetchone()[0] == identity
        db.upsert_wallet_identity("local", WALLET_B, "evm", other, "shared")
        db.upsert_wallet_identity("local", WALLET_A, "evm", identity, "shared")
        rows = db.conn.execute("SELECT wallet_id, label FROM wallet_identities ORDER BY wallet_id").fetchall()
        assert [(row["wallet_id"], row["label"]) for row in rows] == [(WALLET_A, "shared"), (WALLET_B, "shared")]
        with pytest.raises(IdentityError) as ambiguous:
            db.resolve_label("local", "shared")
        assert ambiguous.value.reason == "ambiguous_label"
    finally:
        db.close()


def test_mcm_w1_03_legacy_import_does_not_resend_or_invent_history(tmp_path):
    tx_known = "0x" + "00" * 31 + "ab"
    tx_other = "0x" + "11" * 32
    legacy = {
        "_note": "synthetic fixture",
        "last_run_taipei": "2024-03-01T00:00:00+08:00",
        "sources": {"eth": "synthetic description"},
        "nonces": {ADDR_A: {"bsc": 5, "base": 1}, ADDR_B: {"op": 3}},
        "latest_seen": {
            "A_eth_external": "2024-03-01",
            "Z_eth_external": "2024-03-02",
            "A_doge_external": "2024-03-03",
            "A_eth_bad": "not-a-date",
        },
        "alerted": [
            f"eth:A:{tx_known} synthetic note",
            f"doge:A:{tx_known} nope",
            f"eth:Z:{tx_other} nope",
            "not-an-alert",
        ],
        "ignored_spam": ["spam"],
        "news_checked": ["lista"],
        "runs": [{"at": "2024-03-01", "result": "ok", "gaps": ["history"]}],
        "bsc_last_block": 12345,
    }
    original = json.dumps(legacy, sort_keys=True)
    path = tmp_path / "legacy.json"
    path.write_text(json.dumps(legacy), encoding="utf-8")
    before_file = path.read_bytes()
    accounts_path = tmp_path / "accounts.json"
    _write_json(accounts_path, _accounts_doc([
        _account(WALLET_A, "synthetic-label-alpha", "evm", _checksum(ADDR_A), ["ethereum-mainnet", "bsc-mainnet"]),
    ]))
    accounts = load_accounts(accounts_path)
    db = Database(tmp_path / "state.sqlite")
    try:
        with db.conn:
            db.conn.execute(
                "INSERT INTO snapshots(generation, tenant_id, created_at) VALUES (1, 'local', 'old')",
            )
            db.conn.execute(
                "INSERT INTO alerts(tenant_id, dedup_key, payload) VALUES ('local', 'already', '{}')",
            )
            db.conn.execute(
                "INSERT INTO outbox(tenant_id, status, payload) VALUES ('local', 'pending', '{}')",
            )
        report = import_legacy_state(db, "local", legacy, tag_map={"A": WALLET_A}, accounts=accounts)
        assert report["noop"] is False
        assert report["snapshots_created"] == 0
        assert report["alerts_created"] == 0
        assert report["outbox_created"] == 0
        assert report["events_created"] == 0
        assert "runs" in report["recognized_unimported"]
        reasons = {row["reason"] for row in report["unmapped"]}
        assert {"unknown_chain", "unknown_wallet_tag", "unknown_address", "unparseable", "bad_date"} <= reasons
        assert db.conn.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0] == 1
        assert db.conn.execute("SELECT COUNT(*) FROM snapshot_items").fetchone()[0] == 0
        assert db.conn.execute("SELECT COUNT(*) FROM alerts").fetchone()[0] == 1
        assert db.conn.execute("SELECT COUNT(*) FROM outbox").fetchone()[0] == 1
        assert db.conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 0
        assert db.conn.execute("SELECT COUNT(*) FROM alert_dedup WHERE dedup_key LIKE 'legacy:%'").fetchone()[0] == 4
        assert db.conn.execute("SELECT COUNT(*) FROM legacy_alert_suppressions").fetchone()[0] == 4
        assert is_legacy_alerted(db, "local", "ethereum-mainnet", tx_known) is True
        assert is_legacy_alerted(db, "local", "ethereum-mainnet", tx_known.upper()) is True
        assert is_legacy_alerted(db, "local", "doge-mainnet", tx_known) is False
        seen = db.conn.execute(
            "SELECT value FROM cursors WHERE tenant_id='local' AND scope='legacy:latest_seen:A_eth_external'"
        ).fetchone()
        body = json.loads(seen["value"])
        assert body["provenance"] == "legacy_import"
        assert body["precision"] == "date"
        assert body["value"] == "2024-03-01"
        assert body["wallet_id"] == WALLET_A
        assert db.conn.execute(
            "SELECT COUNT(*) FROM cursors WHERE scope LIKE 'legacy:latest_seen:Z_%' OR scope LIKE 'legacy:latest_seen:A_doge_%' OR scope='legacy:latest_seen:A_eth_bad'"
        ).fetchone()[0] == 0
        nonce = json.loads(db.conn.execute(
            "SELECT value FROM cursors WHERE scope=?",
            (f"legacy:nonce:{WALLET_A}:bsc",),
        ).fetchone()["value"])
        assert nonce["precision"] == "nonce" and nonce["value"] == 5 and nonce["value"] != 0
        assert db.conn.execute(
            "SELECT value FROM cursors WHERE scope=?",
            (f"legacy:nonce:{WALLET_A}:base",),
        ).fetchone() is not None
        assert db.conn.execute(
            "SELECT COUNT(*) FROM cursors WHERE scope LIKE 'legacy:nonce:%:op'"
        ).fetchone()[0] == 0
        block = json.loads(db.conn.execute(
            "SELECT value FROM cursors WHERE scope='legacy:bsc_last_block'"
        ).fetchone()["value"])
        assert block["precision"] == "block" and block["value"] == 12345
        again = import_legacy_state(db, "local", legacy, tag_map={"A": WALLET_A}, accounts=accounts)
        assert again["noop"] is True
        assert db.conn.execute("SELECT COUNT(*) FROM legacy_imports").fetchone()[0] == 1
        assert db.conn.execute("SELECT COUNT(*) FROM alert_dedup WHERE dedup_key LIKE 'legacy:%'").fetchone()[0] == 4
        assert db.conn.execute("SELECT COUNT(*) FROM outbox").fetchone()[0] == 1
    finally:
        db.close()
    assert path.read_bytes() == before_file
    assert json.dumps(legacy, sort_keys=True) == original


def test_mcm_w1_03_missing_legacy_fields_write_nothing(tmp_path):
    db = Database(tmp_path / "empty.sqlite")
    try:
        report = import_legacy_state(
            db, "local", {"_note": "synthetic", "runs": [{"result": "ok"}]}, tag_map={}, accounts=[],
        )
        assert report["cursors_written"] == 0
        assert report["alert_dedup_written"] == 0
        assert report["snapshots_created"] == 0
        assert "runs" in report["recognized_unimported"]
        assert db.conn.execute("SELECT COUNT(*) FROM cursors").fetchone()[0] == 0
        assert db.conn.execute("SELECT COUNT(*) FROM alert_dedup").fetchone()[0] == 0
        assert db.conn.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0] == 0
        assert db.conn.execute("SELECT COUNT(*) FROM outbox").fetchone()[0] == 0
    finally:
        db.close()


def test_mcm_w1_03_failed_import_rolls_back(tmp_path):
    db = Database(tmp_path / "rollback.sqlite")
    try:
        db.conn.execute(
            "CREATE TRIGGER boom_cursor BEFORE INSERT ON cursors "
            "BEGIN SELECT RAISE(ABORT, 'boom'); END"
        )
        with pytest.raises(sqlite3.IntegrityError, match="boom"):
            import_legacy_state(
                db,
                "local",
                {"bsc_last_block": 9, "alerted": ["eth:A:" + "0x" + "ab" * 32 + " note"]},
                tag_map={"A": WALLET_A},
                accounts=[],
            )
        assert db.conn.execute("SELECT COUNT(*) FROM cursors").fetchone()[0] == 0
        assert db.conn.execute("SELECT COUNT(*) FROM alert_dedup").fetchone()[0] == 0
        assert db.conn.execute("SELECT COUNT(*) FROM legacy_imports").fetchone()[0] == 0
        db.conn.execute("DROP TRIGGER boom_cursor")
        report = import_legacy_state(
            db, "local", {"bsc_last_block": 9}, tag_map={}, accounts=[],
        )
        assert report["cursors_written"] == 1
        assert json.loads(db.conn.execute("SELECT value FROM cursors").fetchone()[0])["value"] == 9
    finally:
        db.close()


def test_mcm_w1_03_snapshot_cycle_after_migration_does_not_reemit(tmp_path):
    path = tmp_path / "cycle.sqlite"
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA)
    conn.execute("PRAGMA user_version=1")
    conn.execute("INSERT INTO snapshots(generation, tenant_id, created_at) VALUES (1, 'local', '2020-01-01T00:00:00Z')")
    conn.execute("INSERT INTO cursors(scope, tenant_id, value) VALUES ('kept', 'local', '{\"n\":1}')")
    old = _claim("A:lista:old")
    new = _claim("A:lista:new")
    old_dedup = event_key("local", old["wallet_address"], old["position_id"], "claimable")
    payload = json.dumps({"dedup_key": old_dedup})
    conn.execute("INSERT INTO alerts(tenant_id, dedup_key, payload) VALUES ('local', ?, ?)", (old_dedup, payload))
    conn.execute("INSERT INTO alert_dedup(tenant_id, dedup_key) VALUES ('local', ?)", (old_dedup,))
    conn.execute("INSERT INTO outbox(tenant_id, status, payload) VALUES ('local', 'pending', ?)", (payload,))
    conn.commit()
    conn.close()
    db = Database(path)
    try:
        assert db.conn.execute("PRAGMA user_version").fetchone()[0] == 1
        result = SnapshotCycle(db, "local").finish(
            incoming=[old, new],
            successful_scopes={("A", 56)},
            failed_scopes=[],
            created="2024-01-01T00:00:00Z",
        )
        new_dedup = event_key("local", new["wallet_address"], new["position_id"], "claimable")
        assert [alert["dedup_key"] for alert in result["notification_decision"]] == [new_dedup]
        assert old_dedup not in [alert["dedup_key"] for alert in result["notification_decision"]]
        assert result["snapshot_generation"] == 2
        assert db.conn.execute("SELECT COUNT(*) FROM outbox").fetchone()[0] == 2
        assert db.conn.execute("SELECT value FROM cursors WHERE scope='kept'").fetchone()[0] == '{"n":1}'
    finally:
        db.close()


def test_mcm_w1_04_profile_roundtrip_and_safe_export(tmp_path):
    config = load_public_config(ROOT / "examples" / "monitor.json")
    assert config.document["plugins"] == ["lista-slisbnb", "aave-v3", "erc4626-registered"]
    source = (SRC / "configuration.py").read_text(encoding="utf-8")
    assert "importlib" not in source and "exec(" not in source and "eval(" not in source
    accounts = _accounts_doc([
        _account(WALLET_A, "synthetic-label-alpha", "evm", _checksum(ADDR_A), ["ethereum-mainnet", "bsc-mainnet"]),
        _account(WALLET_B, "synthetic-label-beta", "solana", SOL_ZERO, ["solana-mainnet"]),
    ])
    environ = _private_files(
        tmp_path,
        accounts,
        {"ETHERSCAN_API_KEY": SECRET, "RPC_ETHEREUM_MAINNET_URL": ENDPOINT},
    )
    resolved = resolve_private_inputs(config, _own(environ))
    assert [row.wallet_id for row in resolved.accounts.accounts] == [WALLET_A, WALLET_B]
    assert resolved.accounts.accounts[0].normalized.identity_key == ADDR_A
    assert resolved.source_status["etherscan"].credential_present is True
    assert resolved.source_status["goplus"].blocked_reason == "credential_missing"
    exported = safe_export(
        config,
        resolved.accounts,
        {"cursors": ADDR_A, "dedup_key": "legacy:abc", "labels": "synthetic-label-alpha", "source_presence": {"prices": True}},
    )
    assert set(exported) == {"schema_version", "enabled_networks", "wallet_counts", "sources", "budgets"}
    assert exported["schema_version"] == "guardian.monitor.v1"
    assert exported["wallet_counts"] == {
        "by_family": {"evm": 1, "solana": 1},
        "by_network": {"bsc-mainnet": 1, "ethereum-mainnet": 1, "solana-mainnet": 1},
    }
    etherscan = next(row for row in exported["sources"] if row["name"] == "etherscan")
    assert etherscan == {"name": "etherscan", "adapter": "etherscan_v2", "enabled": True, "credential_present": True}
    goplus = next(row for row in exported["sources"] if row["name"] == "goplus")
    assert goplus["credential_present"] is False and goplus["enabled"] is True
    prices = next(row for row in exported["sources"] if row["name"] == "prices")
    assert prices["credential_present"] is True
    assert exported["budgets"]["etherscan-account"]["max_rps"] == 2
    blob = json.dumps(exported)
    assert json.loads(blob) == exported
    for needle in (
        ADDR_A,
        ADDR_B,
        _checksum(ADDR_A),
        SOL_ZERO,
        "synthetic-label-alpha",
        "synthetic-label-beta",
        WALLET_A,
        WALLET_B,
        SECRET,
        ENDPOINT,
        "legacy:abc",
    ):
        assert needle not in blob
    agnostic = _public_copy(tmp_path / "agnostic", lambda document: document["routing"].append({
        "network": "solana-mainnet",
        "capability": "asset_risk",
        "source_ids": ["goplus"],
    }))
    loaded = load_public_config(agnostic)
    assert loaded.document["routing"][-1]["source_ids"] == ["goplus"]


def test_mcm_w1_04_rejects_broken_references_and_family_mismatch(tmp_path):
    def missing_source(document):
        document["routing"][0]["source_ids"] = ["missing-source"]

    def missing_budget(document):
        document["sources"]["etherscan"]["budget_group"] = "no-such-budget"

    def split_budget(document):
        document["sources"]["helius-parser"]["credential_env"] = "ETHERSCAN_API_KEY"

    def shared_budget(document):
        document["sources"]["helius-parser"]["credential_env"] = "ETHERSCAN_API_KEY"
        document["sources"]["helius-parser"]["budget_group"] = "etherscan-account"

    def wrong_family(document):
        document["routing"][0]["source_ids"] = ["rpc-solana-mainnet"]

    with pytest.raises(ConfigError, match="unknown source"):
        load_public_config(_public_copy(tmp_path / "source", missing_source))
    with pytest.raises(ConfigError, match="unknown budget_group"):
        load_public_config(_public_copy(tmp_path / "budget", missing_budget))
    with pytest.raises(ConfigError, match="shared credential_env"):
        load_public_config(_public_copy(tmp_path / "split", split_budget))
    shared = load_public_config(_public_copy(tmp_path / "shared", shared_budget))
    assert shared.document["sources"]["helius-parser"]["budget_group"] == "etherscan-account"
    with pytest.raises(ConfigError, match="adapter family mismatch"):
        load_public_config(_public_copy(tmp_path / "family", wrong_family))
    path = tmp_path / "accounts.json"
    _write_json(path, _accounts_doc([
        _account(WALLET_A, "synthetic-label-alpha", "evm", _checksum(ADDR_A), ["solana-mainnet"]),
    ]))
    with pytest.raises(ConfigError, match="network family mismatch"):
        load_accounts(path)


def _dump(path: Path) -> dict[str, list[tuple]]:
    conn = sqlite3.connect(path)
    tables = ("snapshots", "snapshot_items", "alerts", "alert_dedup", "outbox", "cursors")
    found = {}
    for table in tables:
        found[table] = [tuple(row) for row in conn.execute(f"SELECT * FROM {table} ORDER BY rowid")]
    found["user_version"] = conn.execute("PRAGMA user_version").fetchone()[0]
    conn.close()
    return found


def test_mcm_w1_03_migration_digest_ignores_crlf_checkout(tmp_path):
    """A DB migrated from a CRLF (Windows) checkout must reopen on an LF checkout."""
    from guardian import storage as storage_module

    source = storage_module.mcm_migration_directory()
    lf_dir = tmp_path / "lf"
    crlf_dir = tmp_path / "crlf"
    lf_dir.mkdir()
    crlf_dir.mkdir()
    for path in source.glob("*.sql"):
        text = path.read_bytes().decode("utf-8").replace("\r\n", "\n")
        (lf_dir / path.name).write_bytes(text.encode("utf-8"))
        (crlf_dir / path.name).write_bytes(text.replace("\n", "\r\n").encode("utf-8"))
    db_path = tmp_path / "state.sqlite"
    original = storage_module.mcm_migration_directory
    try:
        storage_module.mcm_migration_directory = lambda: crlf_dir
        Database(db_path).close()
        storage_module.mcm_migration_directory = lambda: lf_dir
        db = Database(db_path)
        try:
            rows = db.conn.execute("SELECT migration_id FROM mcm_schema_migrations").fetchall()
            assert [row[0] for row in rows] == sorted(path.stem for path in source.glob("*.sql"))
        finally:
            db.close()
    finally:
        storage_module.mcm_migration_directory = original
