"""Offline first-run diagnostics. No live lookup, no credential values.

Requested resolver mode, effective resolver mode, connectivity, and egress
qualification are separate facts. DoH enabled is not a qualified path.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any
from urllib.parse import urlsplit

from guardian.credentials import CredentialResolution, profile_from_environ, resolve_named
from guardian.errors import ConfigError, GuardianError, SecurityError

DOH_NOTE = "DoH enabled is not a qualified path"
_MODE_ENV = "GUARDIAN_RESOLVER_MODE"
_ENDPOINTS_ENV = "GUARDIAN_DOH_ENDPOINTS"
_BOOTSTRAP_ENV = "GUARDIAN_DOH_BOOTSTRAP"


def profile_env_name(document: Mapping[str, Any] | None) -> str:
    if isinstance(document, Mapping):
        private = document.get("private_inputs")
        if isinstance(private, Mapping):
            name = private.get("secret_profile_file_env")
            if isinstance(name, str) and name.strip():
                return name.strip()
    return "MONITOR_SECRET_PROFILE"


def credential_names(document: Mapping[str, Any] | None, fallback: Sequence[str] = ()) -> list[str]:
    found: list[str] = []
    sources = document.get("sources") if isinstance(document, Mapping) else None
    if isinstance(sources, Mapping):
        for source in sources.values():
            if not isinstance(source, Mapping):
                continue
            name = source.get("credential_env")
            if isinstance(name, str) and name and name not in found:
                found.append(name)
    if not found:
        found.extend(name for name in fallback if name not in found)
    return sorted(found)


def diagnostic_fields(
    document: Mapping[str, Any] | None,
    environ: Mapping[str, str],
    *,
    fallback_names: Sequence[str] = (),
    resolutions: Sequence[CredentialResolution] | None = None,
) -> dict[str, Any]:
    """Provenance, resolver mode, and the explicit non-qualification of DoH.

    ``resolutions`` is the one context the caller already selected. When it is
    passed, this function does not resolve those variables again.
    """
    names = credential_names(document, fallback_names)
    profile = profile_from_environ(environ, profile_env_name(document))
    if resolutions is None:
        resolved = [resolve_named(name, profile, environ) for name in names]
    else:
        resolved = list(resolutions)
    fields = {
        "credentials": [item.provenance() for item in resolved],
        "resolver": resolver_view(document, environ),
        "connectivity": {"probed": False, "bounded": True, "timeout_seconds": None},
        "egress_qualification": {
            "qualified": False,
            "reason": "not_qualified",
            "note": DOH_NOTE,
        },
    }
    refuse_secret_leaks(fields, _secret_texts(profile, environ, names))
    return fields


def resolver_view(document: Mapping[str, Any] | None, environ: Mapping[str, str]) -> dict[str, Any]:
    """Requested mode and effective mode. Endpoint values are hostnames only."""
    mode_name, endpoints_name, _bootstrap_name = _resolver_env_names(document)
    requested = _requested_mode(environ.get(mode_name))
    try:
        from guardian.sources.resolver import load_resolver_settings

        settings = load_resolver_settings(document, environ)
    except (ConfigError, SecurityError) as exc:
        return {
            "requested_mode": requested,
            "effective_mode": None,
            "endpoints": _hostnames(environ.get(endpoints_name)),
            "error": _safe_reason(exc),
        }
    return {
        "requested_mode": requested,
        "effective_mode": settings.mode,
        "endpoints": [endpoint.host for endpoint in settings.endpoints],
        "error": None,
    }


def refuse_secret_leaks(payload: Any, secrets: Sequence[str]) -> None:
    """Refuse a diagnostic document that contains a credential or a keyed URL."""
    blob = json.dumps(payload, ensure_ascii=True, default=str)
    if "apikey=" in blob.lower():
        raise GuardianError("diagnostic output refused")
    for secret in secrets:
        if not isinstance(secret, str):
            continue
        text = secret.strip()
        if len(text) >= 8 and text in blob:
            raise GuardianError("diagnostic output refused")


def _secret_texts(
    profile: Mapping[str, str],
    environ: Mapping[str, str],
    names: Sequence[str],
) -> list[str]:
    found: list[str] = []
    for name in names:
        for pool in (profile, environ):
            value = pool.get(name)
            if isinstance(value, str) and value.strip():
                found.append(value)
    return found


def _requested_mode(raw: object) -> str:
    """``system``, ``doh``, or ``invalid``. The raw setting is never returned."""
    if not isinstance(raw, str) or not raw.strip():
        return "system"
    lowered = raw.strip().lower()
    if lowered in {"system", "doh"}:
        return lowered
    return "invalid"


def _resolver_env_names(document: Mapping[str, Any] | None) -> tuple[str, str, str]:
    mode_name = _MODE_ENV
    endpoints_name = _ENDPOINTS_ENV
    bootstrap_name = _BOOTSTRAP_ENV
    if isinstance(document, Mapping):
        block = document.get("resolver")
        if isinstance(block, Mapping):
            mode_name = _env_label(block.get("mode_env"), mode_name)
            endpoints_name = _env_label(block.get("endpoints_env"), endpoints_name)
            bootstrap_name = _env_label(block.get("bootstrap_env"), bootstrap_name)
    return mode_name, endpoints_name, bootstrap_name


def _env_label(value: object, default: str) -> str:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return default


def _hostnames(raw: object) -> list[str]:
    """Hostnames from a comma-separated URL list. Raw URLs and userinfo are dropped."""
    if not isinstance(raw, str) or not raw.strip():
        return []
    found: list[str] = []
    for item in raw.split(","):
        host = urlsplit(item.strip()).hostname
        if host and host not in found:
            found.append(host)
    return found


def _safe_reason(exc: BaseException) -> str:
    text = str(exc)
    lowered = text.lower()
    if "://" in text or "apikey" in lowered or "api_key" in lowered or len(text) > 160:
        return "resolver_settings_unusable"
    return text or "resolver_settings_unusable"


def resolutions_for(
    document: Mapping[str, Any] | None,
    environ: Mapping[str, str],
    *,
    fallback_names: Sequence[str] = (),
) -> list[CredentialResolution]:
    names = credential_names(document, fallback_names)
    profile = profile_from_environ(environ, profile_env_name(document))
    return [resolve_named(name, profile, environ) for name in names]
