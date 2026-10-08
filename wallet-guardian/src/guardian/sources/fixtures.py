"""Recorded responses. An unmatched request fails; it never opens a socket."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from guardian.errors import GuardianError
from guardian.security.redact import redact


class FixtureMiss(GuardianError):
    def __init__(self) -> None:
        self.reason = "fixture_miss"
        super().__init__("fixture_miss")


@dataclass(frozen=True)
class RawResponse:
    status: int
    headers: Mapping[str, str]
    body: bytes


@dataclass(frozen=True)
class FixtureFailure:
    """A scripted transport failure. ``timeout`` and ``connection_error`` are retryable."""

    reason: str


@dataclass(frozen=True)
class PreparedCall:
    source_id: str
    method: str
    url: str
    body: bytes
    protocol: str
    connect_ip: str | None = None

    def __repr__(self) -> str:
        return f"PreparedCall(method={self.method!r}, url={redact(self.url)!r}, protocol={self.protocol!r})"


def canonical_body(value: Any) -> bytes:
    if value is None:
        return b""
    if isinstance(value, (bytes, bytearray)):
        return bytes(value)
    return json.dumps(value, ensure_ascii=True, separators=(",", ":"), sort_keys=True).encode("utf-8")


def request_key(method: str, url: str, body: bytes) -> str:
    text = body.decode("utf-8")
    return json.dumps({"body": text, "method": method, "url": url}, separators=(",", ":"), sort_keys=True)


class FixtureBackend:
    """Map canonical request keys to responses. A miss raises FixtureMiss."""

    def __init__(self, mapping: dict[str, RawResponse | FixtureFailure | list[RawResponse | FixtureFailure]]) -> None:
        self.mapping = mapping
        self.calls: list[PreparedCall] = []

    def send(self, call: PreparedCall) -> RawResponse:
        self.calls.append(call)
        key = request_key(call.method, call.url, call.body)
        if key not in self.mapping:
            raise FixtureMiss()
        item = self.mapping[key]
        if isinstance(item, list):
            if not item:
                raise FixtureMiss()
            chosen = item.pop(0)
        else:
            chosen = item
        if isinstance(chosen, FixtureFailure):
            if chosen.reason == "timeout":
                raise TimeoutError("timeout")
            if chosen.reason == "connection_error":
                raise ConnectionError("connection_error")
            raise FixtureMiss()
        return chosen
