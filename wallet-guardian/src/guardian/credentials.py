"""One credential resolver for doctor, coverage, and the read adapters.

A nonempty private-profile value wins. Otherwise a nonempty process-environment
value is used. The choice is made once. An HTTP 401 does not select the other
source. Provenance names the variable and the source. It does not carry the
value.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

PROFILE_PATH_ENV = "MONITOR_SECRET_PROFILE"


def _nonempty(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    return text


@dataclass(frozen=True)
class CredentialResolution:
    """Selected source for one variable. ``reveal`` is for the request, not for diagnostics."""

    variable: str
    selected_from: str
    present: bool
    shadowed: bool
    _value: str

    def reveal(self) -> str:
        return self._value

    def provenance(self) -> dict[str, Any]:
        return {
            "variable": self.variable,
            "selected_from": self.selected_from,
            "present": self.present,
            "shadowed": self.shadowed,
        }

    def __repr__(self) -> str:
        return (
            f"CredentialResolution(variable={self.variable!r}, selected_from={self.selected_from!r}, "
            f"present={self.present!r}, shadowed={self.shadowed!r})"
        )


def resolve_named(
    name: str,
    profile: Mapping[str, str] | None,
    environ: Mapping[str, str] | None,
) -> CredentialResolution:
    """Profile, then environment. Empty and whitespace-only values are absent.

    ``shadowed`` is true when the selected source hid another nonempty value.
    A later 401 must keep this object. Do not call this again to try the other source.
    """
    profile_map = profile or {}
    env_map = environ or {}
    profile_value = _nonempty(profile_map.get(name))
    env_value = _nonempty(env_map.get(name))
    if profile_value is not None:
        return CredentialResolution(name, "profile", True, env_value is not None, profile_value)
    if env_value is not None:
        return CredentialResolution(name, "env", True, False, env_value)
    return CredentialResolution(name, "absent", False, False, "")


def profile_from_environ(
    environ: Mapping[str, str],
    profile_env: str = PROFILE_PATH_ENV,
) -> dict[str, str]:
    """Load the secret profile when its path variable is set and the file exists.

    A missing path or a missing file is an empty profile, not an error. The
    caller that requires the file uses ``load_profile_required``.
    """
    raw = environ.get(profile_env)
    if not isinstance(raw, str) or not raw.strip():
        return {}
    path = Path(raw).expanduser()
    if not path.is_file():
        return {}
    from guardian.configuration import _load_secret_profile

    return _load_secret_profile(path)


def load_profile_required(environ: Mapping[str, str], profile_env: str) -> dict[str, str]:
    """Load the profile the public config named. A missing path or file is a config error."""
    from guardian.configuration import _env_path, _load_secret_profile

    return _load_secret_profile(_env_path(environ, profile_env))


def reveal_for_profile_env(name: str, environ: Mapping[str, str], profile_env: str) -> str:
    """Value the adapters send. Absent is an empty string so the caller can refuse the read."""
    profile = load_profile_required(environ, profile_env)
    found = resolve_named(name, profile, environ)
    return found.reveal() if found.present else ""
