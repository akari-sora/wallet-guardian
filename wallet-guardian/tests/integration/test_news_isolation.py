"""WP-2. A slow or broken news fetch cannot drop on-chain scans or on-chain alerts.

Addresses are the synthetic desk addresses. No private key is used.
"""

from __future__ import annotations

import importlib.util
import json
import socket
from pathlib import Path
from urllib.parse import urlsplit

import httpx
import pytest

from guardian.cli import main
from guardian.news.catalog import ALLOWLISTED_FEED_URLS
from guardian.sources.fixtures import RawResponse

_SPEC = importlib.util.spec_from_file_location(
    "news_isolation_support",
    Path(__file__).with_name("test_mcm_live_entrypoint.py"),
)
_LIVE = importlib.util.module_from_spec(_SPEC)
assert _SPEC is not None and _SPEC.loader is not None
_SPEC.loader.exec_module(_LIVE)

_NEWS_HOSTS = {(urlsplit(url).hostname or "").lower() for url in ALLOWLISTED_FEED_URLS}
_NEWS_KINDS = frozenset({"exposure", "exposure_unknown", "exposure_update"})
_EMPTY = b'<?xml version="1.0"?><rss version="2.0"><channel></channel></rss>'
_CHAIN_DEADLINE = 90


class _Clock:
    def __init__(self) -> None:
        self.now = 1_700_000_000.0

    def __call__(self) -> float:
        return self.now


class _SlowNews(_LIVE.ChainBackend):
    """Chain answers stay instant. Each news GET moves the injected clock by 20s."""

    def __init__(self, clock: _Clock) -> None:
        super().__init__()
        self.clock = clock
        self.news_gets = 0

    def send(self, call: object) -> RawResponse:
        method = str(getattr(call, "method", "") or "")
        host = (urlsplit(str(getattr(call, "url", ""))).hostname or "").lower()
        if method == "GET" and host in _NEWS_HOSTS:
            self.news_gets += 1
            self.clock.now += 20
            return RawResponse(200, {"content-type": "application/rss+xml"}, _EMPTY)
        return super().send(call)


@pytest.fixture(autouse=True)
def _block_network(monkeypatch: pytest.MonkeyPatch) -> None:
    def blocked(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("network is disabled")

    monkeypatch.setattr(socket, "socket", blocked)
    monkeypatch.setattr(socket, "create_connection", blocked)
    monkeypatch.setattr(socket, "getaddrinfo", blocked)
    monkeypatch.setattr(httpx, "Client", blocked)
    monkeypatch.setattr("guardian.pipeline._resolve", lambda _host: ["1.1.1.1"])


def _eight_pairs(original):
    def wrapped(sources):
        found = list(original(sources))
        seed = list(found)
        index = 0
        while seed and len(found) < 8:
            found.append(seed[index % len(seed)])
            index += 1
        return found

    return wrapped


def _enable(desk, *, fetch: bool) -> None:
    document = json.loads(desk.config.read_text(encoding="utf-8"))
    document["polling"]["round_deadline_seconds"] = _CHAIN_DEADLINE
    if fetch:
        document["news"] = {"fetch": True}
    desk.config.write_text(json.dumps(document), encoding="utf-8")


def _run(capsys, desk, clock) -> tuple[int, dict]:
    code = main(
        ["run-once", "--mcm-config", str(desk.config), "--no-send"],
        backend=desk.backend,
        clock=clock,
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload.get("status") != "error", payload
    assert payload.get("status") != "failed", payload
    assert code != 1, payload
    return code, payload


def _onchain_outbox(desk) -> list[dict]:
    return [row for row in _LIVE._outbox(desk) if row["payload"].get("kind") not in _NEWS_KINDS]


def _kinds(rows: list[dict]) -> list[str]:
    return sorted(str(row["payload"].get("kind")) for row in rows)


def _onchain_reasons(report: dict) -> list[str]:
    found = []
    for row in report["coverage"]:
        if row.get("capability") == "exposure_news":
            continue
        found.extend(row.get("reason_codes") or [])
    return found


def _news_rows(report: dict) -> list[dict]:
    return [row for row in report["coverage"] if row.get("capability") == "exposure_news"]


def _desk(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    tmp_path.mkdir(parents=True, exist_ok=True)
    return _LIVE._desk(tmp_path, monkeypatch)


def _two_rounds(capsys, desk, clock) -> dict:
    _run(capsys, desk, clock)
    desk.backend.show_material()
    _code, second = _run(capsys, desk, clock)
    return second


def test_slow_news_gets_do_not_deadline_the_chain(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    """Eight news GETs of 20s each run after the chain commit."""
    import guardian.news.fetch as news_fetch

    monkeypatch.setattr(news_fetch, "_pairs", _eight_pairs(news_fetch._pairs))
    quiet = _desk(tmp_path / "quiet", monkeypatch)
    _enable(quiet, fetch=False)
    quiet_clock = _Clock()
    quiet_second = _two_rounds(capsys, quiet, quiet_clock)
    quiet_outbox = _onchain_outbox(quiet)

    clock = _Clock()
    slow = _desk(tmp_path / "slow", monkeypatch)
    _enable(slow, fetch=True)
    slow.backend = _SlowNews(clock)
    second = _two_rounds(capsys, slow, clock)

    assert isinstance(slow.backend, _SlowNews)
    assert slow.backend.news_gets == 16
    assert "deadline" not in _onchain_reasons(second)
    assert "deadline" not in _onchain_reasons(quiet_second)
    assert _kinds(_onchain_outbox(slow)) == _kinds(quiet_outbox)
    assert len(quiet_outbox) > 1


def test_parse_feed_runtime_error_keeps_chain_alerts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    quiet = _desk(tmp_path / "quiet", monkeypatch)
    _enable(quiet, fetch=False)
    _two_rounds(capsys, quiet, _Clock())

    def boom(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("feed parser failed")

    monkeypatch.setattr("guardian.news.fetch.parse_feed", boom)
    broken = _desk(tmp_path / "broken", monkeypatch)
    _enable(broken, fetch=True)
    second = _two_rounds(capsys, broken, _Clock())
    assert _kinds(_onchain_outbox(broken)) == _kinds(_onchain_outbox(quiet))
    rows = _news_rows(second)
    assert len(rows) == 1
    assert "news_fetch_failed" in rows[0]["reason_codes"]
    assert rows[0]["quality"] != "complete_in_declared_scope"
    assert {row.get("reason") for row in second["news_health"]} == {"news_fetch_failed"}


def test_link_exposure_exception_keeps_chain_alerts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    quiet = _desk(tmp_path / "quiet", monkeypatch)
    _enable(quiet, fetch=False)
    _two_rounds(capsys, quiet, _Clock())

    def boom(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("exposure link failed")

    monkeypatch.setattr("guardian.pipeline.link_exposure", boom)
    broken = _desk(tmp_path / "broken", monkeypatch)
    _enable(broken, fetch=True)
    second = _two_rounds(capsys, broken, _Clock())
    assert _kinds(_onchain_outbox(broken)) == _kinds(_onchain_outbox(quiet))
    rows = _news_rows(second)
    assert len(rows) == 1
    assert "news_postprocess_failed" in rows[0]["reason_codes"]
    assert rows[0]["quality"] != "complete_in_declared_scope"
    assert {row.get("reason") for row in second["news_health"]} == {"news_postprocess_failed"}
