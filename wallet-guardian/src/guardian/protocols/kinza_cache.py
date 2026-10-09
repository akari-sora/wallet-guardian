"""Intra-round cache for Kinza reserve metadata.

Identity and the reserve list are read again for every wallet. If either
result changes, cached metadata is dropped. balanceOf and getUserAccountData
are never served from cache. The Kinza plugin itself is unchanged.
"""

from __future__ import annotations

from typing import Any

from eth_utils import function_signature_to_4byte_selector


def _selector(signature: str) -> str:
    return "0x" + function_signature_to_4byte_selector(signature).hex()


_REVALIDATE = frozenset({
    _selector("ADDRESSES_PROVIDER()"),
    _selector("getPool()"),
    _selector("getReservesList()"),
})
_METADATA = frozenset({
    _selector("POOL()"),
    _selector("UNDERLYING_ASSET_ADDRESS()"),
    _selector("decimals()"),
    _selector("getReserveData(address)"),
})
_BALANCE = _selector("balanceOf(address)")
_ACCOUNT = _selector("getUserAccountData(address)")


class KinzaMetadataCache:
    def __init__(self) -> None:
        self.fingerprints: dict[tuple[str, str], str] = {}
        self.metadata: dict[tuple[str, str], Any] = {}
        self.wallets: set[str] = set()
        self.balance_reads = 0
        self.account_reads = 0
        self.revalidations = 0
        self.metadata_rpc = 0
        self.metadata_shared_hits = 0
        self.rpc_calls = 0

    def wrap(self, transport: Any) -> Any:
        return _CachingTransport(transport, self)

    def snapshot(self) -> dict[str, Any]:
        return {
            "account_reads": self.account_reads,
            "balance_reads": self.balance_reads,
            "metadata_rpc": self.metadata_rpc,
            "metadata_shared_hits": self.metadata_shared_hits,
            "revalidations": self.revalidations,
            "rpc_calls": self.rpc_calls,
            "wallets_read": len(self.wallets),
        }


class _CachingTransport:
    def __init__(self, inner: Any, cache: KinzaMetadataCache) -> None:
        self._inner = inner
        self._cache = cache

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    def request(self, source_id: str, operation: Any, **kwargs: Any) -> Any:
        kind, target, data = _classify(operation)
        cache = self._cache
        if kind == "balance":
            cache.balance_reads += 1
            _remember_wallet(cache, data)
            cache.rpc_calls += 1
            return self._inner.request(source_id, operation, **kwargs)
        if kind == "account":
            cache.account_reads += 1
            _remember_wallet(cache, data)
            cache.rpc_calls += 1
            return self._inner.request(source_id, operation, **kwargs)
        if kind == "revalidate":
            cache.revalidations += 1
            cache.rpc_calls += 1
            result = self._inner.request(source_id, operation, **kwargs)
            blob = _blob(result)
            key = (source_id, target, data[:10].lower())
            previous = cache.fingerprints.get(key)
            if previous is not None and previous != blob:
                cache.metadata.clear()
            cache.fingerprints[key] = blob
            return result
        if kind == "metadata":
            key = (source_id, target, data.lower())
            cached = cache.metadata.get(key)
            if cached is not None:
                cache.metadata_shared_hits += 1
                return cached
            cache.rpc_calls += 1
            result = self._inner.request(source_id, operation, **kwargs)
            if getattr(result, "ok", False):
                cache.metadata[key] = result
                cache.metadata_rpc += 1
            return result
        cache.rpc_calls += 1
        return self._inner.request(source_id, operation, **kwargs)


def _classify(operation: Any) -> tuple[str, str, str]:
    if getattr(operation, "method", None) != "eth_call":
        return "other", "", ""
    params = getattr(operation, "params", None)
    if not isinstance(params, list) or not params or not isinstance(params[0], dict):
        return "other", "", ""
    target = str(params[0].get("to") or "").lower()
    data = str(params[0].get("data") or "")
    if not data.startswith("0x") or len(data) < 10:
        return "other", target, data
    selector = data[:10].lower()
    if selector == _BALANCE:
        return "balance", target, data
    if selector == _ACCOUNT:
        return "account", target, data
    if selector in _REVALIDATE:
        return "revalidate", target, data
    if selector in _METADATA:
        return "metadata", target, data
    return "other", target, data


def _remember_wallet(cache: KinzaMetadataCache, data: str) -> None:
    raw = data[10:]
    if len(raw) < 64:
        return
    cache.wallets.add("0x" + raw[:64][-40:].lower())


def _blob(result: Any) -> str:
    body = getattr(result, "body", None)
    if isinstance(body, dict):
        value = body.get("result")
        if isinstance(value, str):
            return value.lower()
    return ""
