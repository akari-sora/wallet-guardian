"""Synthetic Lista chain for the installed-wheel test.

This module does not import guardian or pytest. The child process puts this
directory on sys.path and asks it for JSON-RPC results.
"""

from __future__ import annotations

from typing import Any

from eth_abi import encode
from eth_hash.auto import keccak

PAY = 8421285170186643000
SLIS = 8107363680000000000
CLAIM = "0x" + "11" * 32
REQ = "0x" + "22" * 32
OWNER = "0x" + "0" * 39 + "1"
SUBMITTER = "0x" + "0" * 36 + "0b07"
SLOT = "0x360894a13ba1a3210667c828492db98dca3e2076cc3735a920a3ca505d382bbc"
UPGRADED = "0xbc7cd75a20ee27fd9adebab32041f755214dbc6bffa90cc0225b39da2e5c2d3b"
QUEUE = "0x6c930228"
STATUS = "0x026e3e7b"
ROLE = "0x91d14854"
CLAIM_FOR = "0x9a53d5af"


def _hash(number: int) -> str:
    return "0x" + f"{number:064x}"


def _word(value: int) -> str:
    return f"{value:064x}"


def _pad(address: str) -> str:
    return "0x" + address.lower()[2:].rjust(64, "0")


def _spec_code(label: str) -> str:
    return "0x" + (bytes.fromhex("60806040") + label.encode()).hex()


class ListaChain:
    """One coherent claim. ``quote_extra`` makes the status amount disagree with the log."""

    def __init__(self, manifest: dict[str, Any], *, quote_extra: int = 0) -> None:
        self.account = OWNER
        self.quote_extra = quote_extra
        proxy = manifest["proxy"]
        self.proxy = str(proxy["address"]).lower()
        stored = next(row for row in manifest["implementations"] if row["calculation"] == "stored_bnb")
        self.implementation = str(stored["implementation"]).lower()
        self.impl_code = _spec_code("lista-explainer-stored-bnb-v1")
        self.proxy_code = _spec_code("lista-explainer-erc1967-proxy-v1")
        events = {row["signature"]: str(row["topic"]).lower() for row in manifest["abi"]["events"]}
        self.claim_topic = events["ClaimWithdrawal(address,uint256,uint256)"]
        self.request_topic = events["RequestWithdraw(address,uint256)"]
        self.old = 100
        self.new = 110
        self.request_block = 105
        self.logs = [self._request(), self._claim()]

    def answer(self, method: str, params: list[Any]) -> Any | None:
        if method == "eth_chainId":
            return "0x38"
        if method == "eth_getBlockByNumber":
            return self._header(params[0] if params else None)
        if method == "eth_getLogs":
            return self._logs(params[0] if params else {})
        if method == "eth_getTransactionByHash":
            return self._tx() if params and str(params[0]).lower() == CLAIM else None
        if method == "eth_getTransactionReceipt":
            return self._receipt() if params and str(params[0]).lower() == CLAIM else None
        if method == "eth_getStorageAt":
            return self._storage(params)
        if method == "eth_getCode":
            return self._code(params)
        if method == "eth_call":
            return self._call(params)
        return None

    def _header(self, tag: Any) -> dict[str, Any] | None:
        if not isinstance(tag, str) or not tag.startswith("0x"):
            return None
        number = int(tag, 16)
        return {
            "hash": _hash(number),
            "number": hex(number),
            "parentHash": _hash(max(number - 1, 0)),
            "timestamp": hex(1_700_000_000),
        }

    def _request(self) -> dict[str, Any]:
        return {
            "address": self.proxy,
            "blockHash": _hash(self.request_block),
            "blockNumber": hex(self.request_block),
            "data": "0x" + _word(SLIS),
            "logIndex": hex(4),
            "removed": False,
            "topics": [self.request_topic, _pad(OWNER)],
            "transactionHash": REQ,
            "transactionIndex": hex(0),
        }

    def _claim(self) -> dict[str, Any]:
        return {
            "address": self.proxy,
            "blockHash": _hash(self.new),
            "blockNumber": hex(self.new),
            "data": "0x" + _word(0) + _word(PAY),
            "logIndex": hex(7),
            "removed": False,
            "topics": [self.claim_topic, _pad(OWNER)],
            "transactionHash": CLAIM,
            "transactionIndex": hex(1),
        }

    def _logs(self, filt: Any) -> list[dict[str, Any]] | None:
        if not isinstance(filt, dict):
            return []
        topics = filt.get("topics") if isinstance(filt.get("topics"), list) else []
        topic = str(topics[0]).lower() if topics else ""
        if topic == UPGRADED:
            return []
        if "fromBlock" not in filt or "toBlock" not in filt:
            return []
        start = int(str(filt["fromBlock"]), 16)
        end = int(str(filt["toBlock"]), 16)
        found = []
        for row in self.logs:
            if int(row["blockNumber"], 16) < start or int(row["blockNumber"], 16) > end:
                continue
            if str(row["topics"][0]).lower() != topic:
                continue
            if len(topics) > 1 and str(row["topics"][1]).lower() != str(topics[1]).lower():
                continue
            found.append(row)
        return found

    def _tx(self) -> dict[str, Any]:
        payload = encode(["address", "uint256"], [OWNER, 0])
        return {
            "blockHash": _hash(self.new),
            "blockNumber": hex(self.new),
            "from": SUBMITTER,
            "hash": CLAIM,
            "input": CLAIM_FOR + payload.hex(),
            "to": self.proxy,
            "transactionIndex": hex(1),
        }

    def _receipt(self) -> dict[str, Any]:
        return {
            "blockHash": _hash(self.new),
            "blockNumber": hex(self.new),
            "logs": [self._claim()],
            "status": "0x1",
            "transactionHash": CLAIM,
            "transactionIndex": hex(1),
        }

    def _storage(self, params: list[Any]) -> str | None:
        if len(params) < 3 or str(params[1]).lower() != SLOT:
            return None
        return _pad(self.implementation)

    def _code(self, params: list[Any]) -> str | None:
        if len(params) < 2:
            return None
        address = str(params[0]).lower()
        if address == self.implementation:
            return self.impl_code
        if address == self.proxy:
            return self.proxy_code
        return "0x"

    def _call(self, params: list[Any]) -> str | None:
        if len(params) < 2 or not isinstance(params[0], dict):
            return None
        data = str(params[0].get("data") or "")
        selector = data[:10].lower()
        block = params[1].get("blockHash") if isinstance(params[1], dict) else ""
        digest = str(block).lower()
        if selector == QUEUE:
            if digest == _hash(self.new - 1):
                rows = [(42, SLIS, 1)]
            else:
                rows = []
            return "0x" + encode(["(uint256,uint256,uint256)[]"], [rows]).hex()
        if selector == STATUS:
            return "0x" + encode(["bool", "uint256"], [True, PAY + self.quote_extra]).hex()
        if selector == ROLE:
            return "0x" + encode(["bool"], [True]).hex()
        return None


def code_matches(manifest: dict[str, Any]) -> bool:
    """The bytecode this fixture returns is the manifest's specification pin."""
    proxy = manifest["proxy"]
    stored = next(row for row in manifest["implementations"] if row["calculation"] == "stored_bnb")
    impl = _spec_code("lista-explainer-stored-bnb-v1")
    proxy_code = _spec_code("lista-explainer-erc1967-proxy-v1")
    impl_hash = "0x" + keccak(bytes.fromhex(impl[2:])).hex()
    proxy_hash = "0x" + keccak(bytes.fromhex(proxy_code[2:])).hex()
    return impl_hash == str(stored["code_hash"]).lower() and proxy_hash == str(proxy["code_hash"]).lower()
