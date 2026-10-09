"""One token bucket per provider account, shared by every chain that uses it.

Low-priority work stops before the hard cap. Health and other high-priority
work can still spend the last reserved unit. Exhaustion raises; it is not a
silent skip and it is not a zero observation.
"""

from __future__ import annotations

import json
import os
import stat
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from guardian.errors import ConfigError, GuardianError

Clock = Callable[[], float]
Sleeper = Callable[[float], None]


class BudgetExhausted(GuardianError):
    """The shared account cannot accept this call. Callers should leave a gap."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


class BootstrapStop(Exception):
    """The indexer walk hit the bootstrap request cap or the round deadline.

    This is not a daily budget miss. Callers keep the last fully read prefix.
    """

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


@dataclass(frozen=True)
class BudgetSpec:
    max_rps: float
    max_concurrency: int
    max_requests_per_day: int
    retry_attempts: int
    max_credits_per_month: int | None = None

    @classmethod
    def from_document(cls, document: Mapping[str, Any]) -> BudgetSpec:
        credits = document.get("max_credits_per_month")
        return cls(
            max_rps=float(document["max_rps"]),
            max_concurrency=int(document["max_concurrency"]),
            max_requests_per_day=int(document["max_requests_per_day"]),
            retry_attempts=int(document["retry_attempts"]),
            max_credits_per_month=None if credits is None else int(credits),
        )


@dataclass
class Lease:
    group: BudgetGroup

    def release(self) -> None:
        self.group._release()


class BudgetGroup:
    """Shared counters for every source that names this budget_group."""

    def __init__(self, name: str, spec: BudgetSpec, *, clock: Clock, sleep: Sleeper) -> None:
        if spec.max_rps <= 0 or spec.max_concurrency < 1 or spec.max_requests_per_day < 1:
            raise ConfigError("budget spec is not usable")
        if spec.retry_attempts < 0:
            raise ConfigError("budget spec is not usable")
        self.name = name
        self.spec = spec
        self._clock = clock
        self._sleep = sleep
        self._capacity = max(1.0, float(spec.max_rps))
        self._tokens = self._capacity
        self._updated = clock()
        self._in_flight = 0
        self.requests_today = 0
        self.credits_month = 0
        today = _day(clock())
        self.day = today
        self.month = today[:7]

    def acquire(self, *, priority: str = "normal", credits: int = 1) -> Lease:
        if priority not in {"low", "normal", "high", "health"}:
            raise BudgetExhausted("invalid_priority")
        if not isinstance(credits, int) or credits < 1:
            raise BudgetExhausted("invalid_credits")
        self._roll()
        if self._in_flight >= self.spec.max_concurrency:
            raise BudgetExhausted("concurrency")
        if self.requests_today >= self.spec.max_requests_per_day:
            raise BudgetExhausted("daily_cap")
        if priority == "low" and self.requests_today >= self.spec.max_requests_per_day - 1:
            raise BudgetExhausted("low_priority")
        limit = self.spec.max_credits_per_month
        if limit is not None:
            if self.credits_month + credits > limit:
                raise BudgetExhausted("credit_cap")
            if priority == "low" and self.credits_month + credits > limit - 1:
                raise BudgetExhausted("low_priority")
        wait = self._reserve_token()
        self.requests_today += 1
        self.credits_month += credits
        self._in_flight += 1
        if wait > 0:
            self._sleep(wait)
            self._tokens = 0
            self._updated = self._clock()
        return Lease(self)

    def save(self, path: Path) -> None:
        path = Path(path)
        payload = {
            "budget_group": self.name,
            "credits_month": self.credits_month,
            "day": self.day,
            "month": self.month,
            "requests_today": self.requests_today,
            "version": 1,
        }
        _atomic_write(path, json.dumps(payload, indent=2, sort_keys=True) + "\n")

    @classmethod
    def load(cls, path: Path, spec: BudgetSpec, *, clock: Clock, sleep: Sleeper) -> BudgetGroup:
        path = Path(path)
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ConfigError("provider usage is not readable") from None
        if not isinstance(document, dict) or document.get("version") != 1:
            raise ConfigError("provider usage is not readable")
        name = document.get("budget_group")
        if not isinstance(name, str) or not name:
            raise ConfigError("provider usage is not readable")
        group = cls(name, spec, clock=clock, sleep=sleep)
        group.day = str(document["day"])
        group.month = str(document["month"])
        group.requests_today = int(document["requests_today"])
        group.credits_month = int(document["credits_month"])
        group._roll()
        return group

    def _reserve_token(self) -> float:
        now = self._clock()
        elapsed = max(0.0, now - self._updated)
        self._tokens = min(self._capacity, self._tokens + elapsed * float(self.spec.max_rps))
        self._updated = now
        if self._tokens >= 1:
            self._tokens -= 1
            return 0.0
        return (1 - self._tokens) / float(self.spec.max_rps)

    def _release(self) -> None:
        if self._in_flight > 0:
            self._in_flight -= 1

    def _roll(self) -> None:
        today = _day(self._clock())
        month = today[:7]
        if today != self.day:
            self.day = today
            self.requests_today = 0
        if month != self.month:
            self.month = month
            self.credits_month = 0


class BudgetBook:
    """The process-wide set of provider accounts."""

    def __init__(self, groups: dict[str, BudgetGroup]) -> None:
        self._groups = groups

    @classmethod
    def from_document(cls, document: Mapping[str, Mapping[str, Any]], *, clock: Clock, sleep: Sleeper) -> BudgetBook:
        groups = {
            name: BudgetGroup(name, BudgetSpec.from_document(spec), clock=clock, sleep=sleep)
            for name, spec in document.items()
        }
        return cls(groups)

    def group(self, name: str) -> BudgetGroup:
        try:
            return self._groups[name]
        except KeyError:
            raise ConfigError("unknown budget_group") from None

    def save_all(self, directory: Path) -> None:
        directory = Path(directory)
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        if os.name == "posix":
            os.chmod(directory, 0o700)
        for name, group in self._groups.items():
            group.save(directory / f"{name}.json")


def usage_path(state_dir: Path, budget_group: str) -> Path:
    return Path(state_dir) / "provider-usage" / f"{budget_group}.json"


def _day(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp, timezone.utc).date().isoformat()


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_name(path.name + ".tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, text.encode("utf-8"))
        os.fsync(fd)
    finally:
        os.close(fd)
    if os.name == "posix":
        os.chmod(temporary, 0o600)
    os.replace(temporary, path)
    if os.name == "posix":
        os.chmod(path, 0o600)
        mode = stat.S_IMODE(path.stat().st_mode)
        if mode & 0o077:
            raise ConfigError("provider usage is group or world readable")
