"""News GETs stop when their own deadline is spent. They do not use the chain clock."""

from __future__ import annotations

from pathlib import Path

import pytest

from guardian.news.fetch import fetch_allowlisted_news
from guardian.sources.fixtures import RawResponse
from guardian.storage import Database

_EMPTY = b'<?xml version="1.0"?><rss version="2.0"><channel></channel></rss>'


class _Clock:
    def __init__(self) -> None:
        self.now = 1_700_000_000.0

    def __call__(self) -> float:
        return self.now


class _Backend:
    def __init__(self, clock: _Clock, *, sleep: float) -> None:
        self.clock = clock
        self.sleep = sleep
        self.gets = 0

    def send(self, _call: object) -> RawResponse:
        self.gets += 1
        self.clock.now += self.sleep
        return RawResponse(200, {"content-type": "application/rss+xml"}, _EMPTY)


def _fetch(tmp_path: Path, backend: _Backend, clock: _Clock, deadline: float | None):
    database = Database(tmp_path / "state" / "monitor.sqlite")
    try:
        return fetch_allowlisted_news(
            database,
            "local",
            backend,
            resolve=lambda _host: ["1.1.1.1"],
            now="2026-10-14T00:00:00Z",
            clock=clock,
            deadline=deadline,
        )
    finally:
        database.close()


def test_news_deadline_is_checked_between_gets(tmp_path: Path) -> None:
    """Each GET spends 20s. A 25s budget allows two GETs and then records deadline."""
    clock = _Clock()
    backend = _Backend(clock, sleep=20)
    report = _fetch(tmp_path, backend, clock, 25)
    assert backend.gets == 2
    reasons = [feed.reason for feed in report.feeds]
    assert reasons[:2] == ["", ""]
    assert reasons[2:]
    assert set(reasons[2:]) == {"deadline"}
    assert all(feed.status == "fetch_error" for feed in report.feeds[2:])


def test_news_without_a_deadline_still_gets_every_feed(tmp_path: Path) -> None:
    clock = _Clock()
    backend = _Backend(clock, sleep=20)
    report = _fetch(tmp_path, backend, clock, None)
    assert backend.gets == len(report.feeds)
    assert backend.gets >= 6
    assert all(feed.reason != "deadline" for feed in report.feeds)


def test_setup_errors_leave_fetch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """State load sits outside the per-feed handler, so the caller must catch it."""
    def boom(*_args: object, **_kwargs: object) -> dict:
        raise RuntimeError("news state unreadable")

    monkeypatch.setattr("guardian.news.fetch._load_state", boom)
    clock = _Clock()
    backend = _Backend(clock, sleep=0)
    with pytest.raises(RuntimeError, match="news state unreadable"):
        _fetch(tmp_path, backend, clock, 180)
    assert backend.gets == 0
