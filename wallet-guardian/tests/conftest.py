"""Tests are hermetic with respect to ambient credentials.

The shared credential resolver reads the process environment after the private
profile, so a developer's real keys must not leak into fixture runs.
"""

from __future__ import annotations

import os

import pytest

_SUFFIXES = ("_API_KEY", "_ACCESS_KEY", "_BOT_TOKEN", "_AUTH_TOKEN")


@pytest.fixture(autouse=True)
def _no_ambient_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in list(os.environ):
        if name.endswith(_SUFFIXES):
            monkeypatch.delenv(name, raising=False)
