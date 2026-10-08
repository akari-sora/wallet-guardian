"""Shared offline harness for MCM-W2. Synthetic hosts only."""

from __future__ import annotations

import json
from pathlib import Path

from guardian.sources.budget import BudgetBook
from guardian.sources.fixtures import FixtureBackend, RawResponse, request_key
from guardian.sources.transport import ReadOnlyTransport, build_endpoint_registry

ROOT = Path(__file__).resolve().parents[3]
SYNTH = "0x0000000000000000000000000000000000000001"
PUBLIC_IP = "1.1.1.1"


def clock_pair(start: float = 1_700_000_000.0):
    now = [start]
    slept: list[float] = []

    def clock() -> float:
        return now[0]

    def sleep(seconds: float) -> None:
        slept.append(seconds)
        now[0] += seconds

    return now, slept, clock, sleep


def public_resolver():
    answers = [PUBLIC_IP]

    def resolve(_host: str) -> list[str]:
        return list(answers)

    return answers, resolve


def source(adapter: str, env_name: str, group: str) -> dict:
    return {
        "adapter": adapter,
        "budget_group": group,
        "enabled": True,
        "endpoint_env": env_name,
    }


def budget(max_rps: float = 1000, **extra) -> dict:
    payload = {
        "max_concurrency": 4,
        "max_requests_per_day": 1000,
        "max_rps": max_rps,
        "retry_attempts": extra.pop("retry_attempts", 2),
    }
    payload.update(extra)
    return payload


def harness(sources: dict, budgets: dict, secrets: dict, *, local_profiles=None, resolve=None, max_response_bytes: int = 1_048_576, retry_clock=None):
    _now, _slept, clock, sleep = clock_pair() if retry_clock is None else retry_clock
    if resolve is None:
        _answers, resolve = public_resolver()
    document = {"budgets": budgets, "sources": sources}
    registry = build_endpoint_registry(document, secrets=secrets, local_profiles=local_profiles)
    book = BudgetBook.from_document(budgets, clock=clock, sleep=sleep)
    backend = FixtureBackend({})
    transport = ReadOnlyTransport(
        registry,
        book,
        backend,
        resolve=resolve,
        max_response_bytes=max_response_bytes,
    )
    return transport, backend, clock, sleep


def install(transport, source_id: str, operation, response: RawResponse) -> None:
    call = transport.encode(source_id, operation)
    transport.backend.mapping[request_key(call.method, call.url, call.body)] = response


def json_body(payload, status: int = 200, headers: dict | None = None) -> RawResponse:
    return RawResponse(status, headers or {}, json.dumps(payload).encode("utf-8"))


def text_body(text: str, status: int = 200, headers: dict | None = None) -> RawResponse:
    return RawResponse(status, headers or {}, text.encode("utf-8"))
