"""Outbound read-only clients. Forbidden RPC methods are rejected before the socket is used.

GET, POST, and JSON-RPC connect to the validated address. The Host header, TLS
SNI, and certificate check keep the original hostname.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from typing import Any
from urllib.parse import urljoin
import httpcore
import httpx
from eth_abi import decode, encode
from eth_utils import keccak

from guardian.errors import RpcError, SecurityError
from guardian.security.redact import redact
from guardian.security.rpc_guard import assert_agent_method
from guardian.sources.fixtures import PreparedCall
from guardian.sources.resolver import assert_direct_transport, direct_client_kwargs
from guardian.sources.transport import _guard_url, pinned_request

_REDIRECTS = frozenset({301, 302, 303, 307, 308})
_PROBE_DEADLINE = threading.local()


class ProbeDeadline:
    """Remaining monotonic budget for one probe, shared by every request in it.

    Cancelling closes open clients and any pinned stream already handed to this
    deadline. A stream still inside HTTPCore's connect is not on the client yet.
    """

    def __init__(self, seconds: float, *, clock=None) -> None:
        self._clock = clock or time.monotonic
        self._deadline = self._clock() + float(seconds)
        self._cancel = threading.Event()
        self._lock = threading.Lock()
        self._clients: list[Any] = []

    def remaining(self) -> float:
        return self._deadline - self._clock()

    def expired(self) -> bool:
        return self._cancel.is_set() or self.remaining() <= 0

    def timeout_for(self, requested: float) -> float:
        """The per-request timeout: the smaller of ``requested`` and the time left."""
        if self.expired():
            raise TimeoutError("probe_deadline")
        left = self.remaining()
        if left <= 0:
            raise TimeoutError("probe_deadline")
        return min(float(requested), left)

    def watch(self, client: Any) -> None:
        """Close ``client`` when this deadline expires, including if it already has."""
        with self._lock:
            self._clients.append(client)
            cancelled = self._cancel.is_set()
        if cancelled:
            _close_quietly(client)
            raise TimeoutError("probe_deadline")

    def cancel(self) -> None:
        """Mark the deadline spent and close every client still in flight."""
        self._cancel.set()
        with self._lock:
            clients = list(self._clients)
        for client in clients:
            _close_quietly(client)


def install_probe_deadline(deadline: ProbeDeadline | None) -> None:
    """Install ``deadline`` on this thread. ``None`` clears it."""
    _PROBE_DEADLINE.deadline = deadline


def _current_deadline() -> ProbeDeadline | None:
    return getattr(_PROBE_DEADLINE, "deadline", None)


def _budget_timeout(timeout: float) -> float:
    deadline = _current_deadline()
    if deadline is None:
        return timeout
    return deadline.timeout_for(timeout)


def _watch_client(client: Any) -> None:
    deadline = _current_deadline()
    if deadline is not None:
        deadline.watch(client)


def _close_quietly(client: Any) -> None:
    close = getattr(client, "close", None)
    if not callable(close):
        return
    try:
        close()
    except Exception:
        return


def _phase_timeout(deadline: ProbeDeadline, requested: float | None) -> float:
    """The time this connect phase may still use, from the budget that is left."""
    if requested is None:
        if deadline.expired():
            raise TimeoutError("probe_deadline")
        left = deadline.remaining()
        if left <= 0:
            raise TimeoutError("probe_deadline")
        return left
    return deadline.timeout_for(requested)


class _DeadlineStream(httpcore.NetworkStream):
    """Pinned socket that keeps the probe deadline across TCP, TLS, and the write.

    ``client.close()`` does not reach the socket while HTTPCore is inside
    ``_connect()`` and has not assigned the connection. After each phase, and
    again before request bytes, an expired or cancelled deadline closes the
    stream that just became available and raises.
    """

    def __init__(self, inner: Any, deadline: ProbeDeadline) -> None:
        self._inner = inner
        self._deadline = deadline
        self._closed = False

    def read(self, max_bytes: int, timeout: float | None = None) -> bytes:
        phase = self._budget(timeout)
        return self._inner.read(max_bytes, phase)

    def write(self, buffer: bytes, timeout: float | None = None) -> None:
        phase = self._budget(timeout)
        self._inner.write(buffer, phase)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        _close_quietly(self._inner)

    def start_tls(
        self,
        ssl_context: Any,
        server_hostname: str | None = None,
        timeout: float | None = None,
    ) -> httpcore.NetworkStream:
        try:
            phase = _phase_timeout(self._deadline, timeout)
        except TimeoutError:
            self.close()
            raise
        try:
            upgraded = self._inner.start_tls(ssl_context, server_hostname, phase)
        except Exception:
            self.close()
            raise
        return _adopt_stream(upgraded, self._deadline)

    def get_extra_info(self, info: str) -> Any:
        return self._inner.get_extra_info(info)

    def _budget(self, timeout: float | None) -> float:
        try:
            phase = _phase_timeout(self._deadline, timeout)
        except TimeoutError:
            self.close()
            raise
        return phase


def _adopt_stream(stream: Any, deadline: ProbeDeadline) -> _DeadlineStream:
    """Own ``stream``. Close it and raise when the deadline is already spent."""
    wrapped = _DeadlineStream(stream, deadline)
    try:
        if deadline.expired():
            raise TimeoutError("probe_deadline")
        deadline.watch(wrapped)
    except TimeoutError:
        wrapped.close()
        raise
    return wrapped


class _DeadlineBackend(httpcore.NetworkBackend):
    """Connect backend that recomputes the probe budget between TCP and TLS."""

    def __init__(self, deadline: ProbeDeadline, inner: httpcore.NetworkBackend) -> None:
        self._deadline = deadline
        self._inner = inner

    def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Any = None,
    ) -> httpcore.NetworkStream:
        phase = _phase_timeout(self._deadline, timeout)
        stream = self._inner.connect_tcp(
            host,
            port,
            timeout=phase,
            local_address=local_address,
            socket_options=socket_options,
        )
        return _adopt_stream(stream, self._deadline)

    def connect_unix_socket(
        self,
        path: str,
        timeout: float | None = None,
        socket_options: Any = None,
    ) -> httpcore.NetworkStream:
        phase = _phase_timeout(self._deadline, timeout)
        stream = self._inner.connect_unix_socket(path, timeout=phase, socket_options=socket_options)
        return _adopt_stream(stream, self._deadline)


def _arm_deadline_transport(client: Any) -> None:
    """Carry the current probe deadline on the client's real connection pool.

    A stand-in client used by tests has no pool, and this leaves that stand-in
    in place. The production pool is HTTPCore 1.0.9's ``_network_backend``.
    """
    deadline = _current_deadline()
    if deadline is None:
        return
    pool = getattr(getattr(client, "_transport", None), "_pool", None)
    backend = getattr(pool, "_network_backend", None)
    if pool is None or backend is None or isinstance(backend, _DeadlineBackend):
        return
    pool._network_backend = _DeadlineBackend(deadline, backend)


def _pinned_exchange(
    method: str,
    url: str,
    body: bytes,
    *,
    resolve,
    timeout: float,
    drop_accept: bool = False,
) -> tuple[int, dict[str, str], str]:
    """One request to the validated IP, with the original hostname on Host and SNI.

    The deadline is checked before DNS and again before the socket. Connect and
    TLS each use the time still left. After TCP, after TLS, and before any
    request bytes, an expired or cancelled deadline closes that stream and
    raises. Closing the client alone does not do this while the handshake is
    still inside HTTPCore's ``_connect()``.
    """
    timeout = _budget_timeout(timeout)
    assert_direct_transport(os.environ)
    connect_ip = _guard_url(url, allow_private=False, resolve=resolve)
    timeout = _budget_timeout(timeout)
    call = PreparedCall("legacy-probe", method, url, body, "rest", connect_ip=connect_ip)
    target, headers, extensions = pinned_request(call)
    if drop_accept:
        headers.pop("accept", None)
    client = httpx.Client(**direct_client_kwargs(timeout=timeout))
    _arm_deadline_transport(client)
    try:
        _watch_client(client)
        response = client.request(
            method,
            target,
            content=body or None,
            headers=headers,
            extensions=extensions,
        )
        copied = {str(key): str(value) for key, value in response.headers.items()}
        return response.status_code, copied, response.text
    finally:
        _close_quietly(client)


IMPLEMENTATION_SLOT = "0x360894a13ba1a3210667c828492db98dca3e2076cc3735a920a3ca505d382bbc"
LISTA_PROXY = "0x1adB950d8bB3dA4bE104211D5AB038628e477fE6"
SLISBNB = "0xB0b84D294e0C75A6abe60171b70edEb2EFd14A1B"


def selector(signature: str) -> bytes:
    return keccak(text=signature)[:4]


def encode_call(signature: str, types: list[str], args: list[Any]) -> str:
    body = encode(types, args) if types else b""
    return "0x" + (selector(signature) + body).hex()


class RpcClient:
    def __init__(
        self,
        url: str,
        *,
        client: httpx.Client | None = None,
        timeout: float = 20,
        resolve=None,
    ) -> None:
        self.url = url
        self.timeout = timeout
        self._client = client
        self.resolve = resolve
        self.calls: list[str] = []

    def call(self, method: str, params: list[Any], *, allow_estimate: bool = False) -> Any:
        assert_agent_method(method, allow_estimate=allow_estimate)
        _budget_timeout(self.timeout)
        self.calls.append(method)
        payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
        try:
            status, body = self._invoke(payload)
        except TimeoutError:
            raise
        except httpx.HTTPError as exc:
            raise RpcError(redact(str(exc))) from exc
        if status != 200:
            raise RpcError(f"rpc http {status}")
        if isinstance(body, dict) and body.get("error"):
            raise RpcError(redact(str(body["error"]))[:500])
        if not isinstance(body, dict) or "result" not in body:
            raise RpcError("rpc response missing result")
        return body["result"]

    def _invoke(self, payload: dict[str, Any]) -> tuple[int, Any]:
        if self._client is not None:
            _watch_client(self._client)
            response = self._client.post(self.url, json=payload)
            try:
                body = response.json()
            except ValueError as exc:
                raise RpcError("rpc response was not json") from exc
            return response.status_code, body
        raw = json.dumps(payload).encode("utf-8")
        status, _headers, text = _pinned_exchange(
            "POST",
            self.url,
            raw,
            resolve=self.resolve,
            timeout=self.timeout,
        )
        try:
            body = json.loads(text) if text else None
        except ValueError as exc:
            raise RpcError("rpc response was not json") from exc
        return status, body


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


def _location(headers: dict[str, str]) -> str:
    for key, value in headers.items():
        if key.lower() == "location":
            return value
    return ""


def safe_get_text(url: str, *, resolve=None, timeout: float = 30) -> tuple[int, str]:
    current = url
    hops = 0
    while True:
        status, headers, text = _pinned_exchange(
            "GET",
            current,
            b"",
            resolve=resolve,
            timeout=timeout,
            drop_accept=True,
        )
        if status in _REDIRECTS:
            hops += 1
            if hops > 3:
                raise SecurityError("too many redirects")
            current = urljoin(current, _location(headers))
            continue
        return status, text


def safe_post_json(
    url: str,
    payload: dict[str, Any],
    *,
    timeout: float = 20,
    resolve=None,
) -> tuple[int, Any]:
    raw = json.dumps(payload).encode("utf-8")
    status, _headers, text = _pinned_exchange("POST", url, raw, resolve=resolve, timeout=timeout)
    try:
        body = json.loads(text) if text else {"non_json": True}
    except ValueError:
        body = {"non_json": True}
    return status, body
