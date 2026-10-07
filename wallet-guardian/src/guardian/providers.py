"""Outbound read-only clients. Forbidden RPC methods are rejected before the socket is used."""

from __future__ import annotations

import hashlib
from typing import Any
import httpx
from eth_abi import decode, encode
from eth_utils import keccak

from guardian.errors import RpcError, SecurityError
from guardian.security.redact import redact
from guardian.security.rpc_guard import assert_agent_method
from guardian.security.ssrf import next_redirect, validate_url

IMPLEMENTATION_SLOT = "0x360894a13ba1a3210667c828492db98dca3e2076cc3735a920a3ca505d382bbc"
LISTA_PROXY = "0x1adB950d8bB3dA4bE104211D5AB038628e477fE6"
SLISBNB = "0xB0b84D294e0C75A6abe60171b70edEb2EFd14A1B"


def selector(signature: str) -> bytes:
    return keccak(text=signature)[:4]


def encode_call(signature: str, types: list[str], args: list[Any]) -> str:
    body = encode(types, args) if types else b""
    return "0x" + (selector(signature) + body).hex()


class RpcClient:
    def __init__(self, url: str, *, client: httpx.Client | None = None, timeout: float = 20) -> None:
        self.url = url
        self.timeout = timeout
        self._client = client
        self.calls: list[str] = []

    def call(self, method: str, params: list[Any], *, allow_estimate: bool = False) -> Any:
        assert_agent_method(method, allow_estimate=allow_estimate)
        self.calls.append(method)
        payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
        try:
            if self._client is not None:
                response = self._client.post(self.url, json=payload)
            else:
                with httpx.Client(timeout=self.timeout, follow_redirects=False) as client:
                    response = client.post(self.url, json=payload)
        except httpx.HTTPError as exc:
            raise RpcError(redact(str(exc))) from exc
        if response.status_code != 200:
            raise RpcError(f"rpc http {response.status_code}")
        try:
            body = response.json()
        except ValueError as exc:
            raise RpcError("rpc response was not json") from exc
        if isinstance(body, dict) and body.get("error"):
            raise RpcError(redact(str(body["error"]))[:500])
        if not isinstance(body, dict) or "result" not in body:
            raise RpcError("rpc response missing result")
        return body["result"]


def _hex_bytes(value: str) -> bytes:
    return bytes.fromhex(value[2:] if value.startswith("0x") else value)


def address_from_word(word: str) -> str:
    from eth_utils import to_checksum_address

    raw = word[2:] if word.startswith("0x") else word
    raw = raw.rjust(64, "0")
    return to_checksum_address("0x" + raw[-40:])


def code_sha256(code: str) -> str:
    payload = _hex_bytes(code) if code and code != "0x" else b""
    return hashlib.sha256(payload).hexdigest()


def read_lista_state(rpc: RpcClient, wallet: str) -> dict[str, Any]:
    chain_id = int(rpc.call("eth_chainId", []), 16)
    if chain_id != 56:
        raise RpcError(f"chain id mismatch: {chain_id}")
    block_hex = rpc.call("eth_blockNumber", [])
    block = rpc.call("eth_getBlockByNumber", [block_hex, False])
    block_number = int(block_hex, 16)
    implementation = address_from_word(
        rpc.call("eth_getStorageAt", [LISTA_PROXY, IMPLEMENTATION_SLOT, block_hex])
    )
    proxy_code = rpc.call("eth_getCode", [LISTA_PROXY, block_hex])
    impl_code = rpc.call("eth_getCode", [implementation, block_hex])
    paused = bool(decode(["bool"], _hex_bytes(rpc.call("eth_call", [{"to": LISTA_PROXY, "data": encode_call("paused()", [], [])}, block_hex])))[0])
    request_word = rpc.call(
        "eth_call",
        [
            {
                "to": LISTA_PROXY,
                "data": encode_call("getUserWithdrawalRequests(address)", ["address"], [wallet]),
            },
            block_hex,
        ],
    )
    decoded = decode(["(uint256,uint256,uint256)[]"], _hex_bytes(request_word))[0]
    requests = []
    for index, row in enumerate(decoded):
        status_word = rpc.call(
            "eth_call",
            [
                {
                    "to": LISTA_PROXY,
                    "data": encode_call(
                        "getUserRequestStatus(address,uint256)",
                        ["address", "uint256"],
                        [wallet, index],
                    ),
                },
                block_hex,
            ],
        )
        is_claimable, bnb_amount = decode(["bool", "uint256"], _hex_bytes(status_word))
        quote = decode(
            ["uint256"],
            _hex_bytes(
                rpc.call(
                    "eth_call",
                    [
                        {
                            "to": LISTA_PROXY,
                            "data": encode_call("convertSnBnbToBnb(uint256)", ["uint256"], [row[1]]),
                        },
                        block_hex,
                    ],
                )
            ),
        )[0]
        requests.append(
            {
                "idx": index,
                "uuid": str(row[0]),
                "amount_in_slis_bnb": str(row[1]),
                "start_time": int(row[2]),
                "is_claimable": bool(is_claimable),
                "bnb_amount": str(bnb_amount),
                "quoted_bnb_from_exchange_rate": str(quote),
                "exchange_rate_is_claim_proof": False,
            }
        )
    slis_balance = decode(
        ["uint256"],
        _hex_bytes(
            rpc.call(
                "eth_call",
                [{"to": SLISBNB, "data": encode_call("balanceOf(address)", ["address"], [wallet])}, block_hex],
            )
        ),
    )[0]
    native = int(rpc.call("eth_getBalance", [wallet, block_hex]), 16)
    return {
        "chain_id": chain_id,
        "block_number": block_number,
        "block_hash": block.get("hash"),
        "block_timestamp": int(block.get("timestamp", "0x0"), 16),
        "implementation": implementation,
        "proxy_code_sha256": code_sha256(proxy_code),
        "implementation_code_sha256": code_sha256(impl_code),
        "paused": paused,
        "requests": requests,
        "slisbnb_raw": str(slis_balance),
        "native_wei": str(native),
        "finality": "provisional",
    }


def safe_get_text(url: str, *, resolve=None, timeout: float = 30) -> tuple[int, str]:
    current = validate_url(url, resolve=resolve)
    hops = 0
    with httpx.Client(timeout=timeout, follow_redirects=False) as client:
        while True:
            response = client.get(current)
            if response.status_code in {301, 302, 303, 307, 308}:
                hops += 1
                if hops > 3:
                    raise SecurityError("too many redirects")
                current = next_redirect(current, response.headers.get("location", ""), resolve=resolve)
                continue
            return response.status_code, response.text


def safe_post_json(url: str, payload: dict[str, Any], *, timeout: float = 20) -> tuple[int, Any]:
    validate_url(url)
    with httpx.Client(timeout=timeout, follow_redirects=False) as client:
        response = client.post(url, json=payload)
    try:
        body = response.json()
    except ValueError:
        body = {"non_json": True}
    return response.status_code, body
