"""Page walkers. A capped or failed walk is never pagination_complete."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from guardian.errors import SecurityError
from guardian.sources.budget import BootstrapStop, BudgetExhausted


@dataclass(frozen=True)
class PageWalk:
    items: tuple[Any, ...]
    pagination_complete: bool
    pages_fetched: int
    stop_reason: str

    def __post_init__(self) -> None:
        if self.stop_reason == "exhausted" and not self.pagination_complete:
            raise ValueError("exhausted walk must be complete")
        if self.stop_reason != "exhausted" and self.pagination_complete:
            raise ValueError("capped walk is not complete")


def walk_page_offset(
    fetch: Callable[[int], Any],
    *,
    page_cap: int,
    item_cap: int,
    offset: int,
) -> PageWalk:
    """Etherscan-style page/offset walker. ``fetch`` receives a 1-based page index."""
    if page_cap < 1 or item_cap < 1 or offset < 1:
        return _stop((), 0, "error")
    items: list[Any] = []
    pages = 0
    page = 1
    while True:
        if pages >= page_cap:
            return _stop(items, pages, "page_cap")
        try:
            batch = fetch(page)
        except BootstrapStop:
            raise
        except BudgetExhausted:
            return _stop(items, pages, "budget")
        except SecurityError:
            raise
        except Exception:
            return _stop(items, pages, "error")
        pages += 1
        if not isinstance(batch, list):
            return _stop(items, pages, "error")
        items.extend(batch)
        if len(items) >= item_cap and len(batch) >= offset:
            return _stop(items[:item_cap], pages, "item_cap")
        if len(batch) < offset:
            if len(items) > item_cap:
                return _stop(items[:item_cap], pages, "item_cap")
            return _stop(items, pages, "exhausted")
        page += 1


def walk_page_key(
    fetch: Callable[[str | None], Any],
    *,
    page_cap: int,
    item_cap: int,
    items_key: str = "transfers",
    token_key: str = "pageKey",
) -> PageWalk:
    """NodeReal-style pageKey walker. A missing items field is an error, not an empty wallet."""
    if page_cap < 1 or item_cap < 1:
        return _stop((), 0, "error")
    items: list[Any] = []
    pages = 0
    token: str | None = None
    while True:
        if pages >= page_cap:
            return _stop(items, pages, "page_cap")
        try:
            payload = fetch(token)
        except BootstrapStop:
            raise
        except BudgetExhausted:
            return _stop(items, pages, "budget")
        except SecurityError:
            raise
        except Exception:
            return _stop(items, pages, "error")
        pages += 1
        if not isinstance(payload, dict) or not isinstance(payload.get(items_key), list):
            return _stop(items, pages, "error")
        batch = payload[items_key]
        items.extend(batch)
        raw_token = payload.get(token_key)
        if raw_token is None:
            if len(items) > item_cap:
                return _stop(items[:item_cap], pages, "item_cap")
            return _stop(items, pages, "exhausted")
        if not isinstance(raw_token, str) or not raw_token:
            return _stop(items, pages, "error")
        if len(items) >= item_cap:
            return _stop(items[:item_cap], pages, "item_cap")
        token = raw_token


def _stop(items: list[Any] | tuple[Any, ...], pages: int, reason: str) -> PageWalk:
    complete = reason == "exhausted"
    return PageWalk(tuple(items), complete, pages, reason)
