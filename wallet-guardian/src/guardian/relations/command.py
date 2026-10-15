"""`guardian relations` inbound and expand."""

from __future__ import annotations

import json
import os
import re
import time
from collections.abc import Callable, Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from guardian.configuration import ConfigError, load_public_config, resolve_private_inputs
from guardian.credentials import resolve_named
from guardian.identities import parse_address
from guardian.locations import secure_write_text
from guardian.network_registry import load_registry
from guardian.pipeline import _secrets, _transport, default_backend
from guardian.relations.fetch import Outcome, read_blockscout, read_tron
from guardian.relations.report import expand_report, inbound_report, overall
from guardian.relations.select import Job, build_jobs, plan_for
from guardian.resources import resource_path
from guardian.security.redact import redact
from guardian.sources.transport import build_endpoint_registry

_CREDENTIAL_ENV = re.compile(r"^[A-Z][A-Z0-9_]{0,95}$")

_MAX_PAGES = 50


def execute(
    args: Any,
    *,
    backend: Any = None,
    clock: Callable[[], float] | None = None,
) -> tuple[dict, int]:
    command = getattr(args, "relations_command", None)
    if command not in {"inbound", "expand"}:
        raise ConfigError("unknown command")
    since_days = _bounded_int(getattr(args, "since_days", 90), "since_days", 1, 3650)
    max_pages = _bounded_int(getattr(args, "max_pages", 10), "max_pages", 1, _MAX_PAGES)
    max_nodes = _bounded_int(getattr(args, "max_nodes", 500), "max_nodes", 1, 5000)
    hub_threshold = _bounded_int(getattr(args, "hub_threshold", 200), "hub_threshold", 0, 1_000_000)
    now = time.time if clock is None else clock
    moment = float(now())
    public = load_public_config(args.mcm_config)
    private = resolve_private_inputs(public, os.environ)
    registry = load_registry(resource_path("config", "chains"))
    secrets = _secrets(public)
    endpoints = build_endpoint_registry(public.document, secrets=secrets, environ=os.environ)
    try:
        seeds = _seeds(args, private, command)
        payer = _payer(args, command)
        jobs = build_jobs(
            command=command,
            seeds=seeds,
            payer=payer,
            explicit_networks=getattr(args, "network", None) if command == "inbound" else None,
            expand_network=getattr(args, "network", None) if command == "expand" else None,
            enabled=list(public.document["enabled_networks"]),
            document=public.document,
            status_map=private.source_status,
            endpoints=endpoints,
            registry=registry,
        )
    except ValueError as exc:
        raise ConfigError(str(exc) or "bad_address") from None
    if not jobs:
        raise ConfigError("seed_required")
    if _queried_nodes(jobs) > max_nodes:
        raise ConfigError("max_nodes")
    plan = plan_for(jobs)
    dry_run = bool(getattr(args, "dry_run", False))
    accepted = bool(getattr(args, "yes", False))
    if dry_run or not accepted:
        payload = _envelope(
            command, "dry_run" if dry_run else "consent_required", since_days, max_pages, moment, plan, [],
        )
        payload = _store(private.state_dir, command, payload, moment, secrets, public.document)
        return payload, 0 if dry_run else 2
    transport = _transport(public, default_backend() if backend is None else backend, now)
    reads, edges, payer_contract, source_complete = _perform(
        jobs, transport, public.document, secrets, registry, max_pages, _stop(moment, since_days), now,
    )
    if command == "inbound":
        body = inbound_report(edges, reads, max_nodes=max_nodes)
    else:
        family, canonical = payer or ("", "")
        body = expand_report(
            edges,
            reads,
            payer=canonical,
            network=str(getattr(args, "network", "")),
            seeds=[item[1] for item in seeds],
            family=family,
            hub_threshold=hub_threshold,
            expand_hub=bool(getattr(args, "expand_hub", False)),
            payer_is_contract=payer_contract,
            max_nodes=max_nodes,
            source_complete=source_complete,
        )
    payload = _envelope(command, overall(reads), since_days, max_pages, moment, plan, reads)
    payload.update(body)
    if payload.get("truncated") and payload.get("status") == "complete":
        payload["status"] = "partial"
    payload = _store(private.state_dir, command, payload, moment, secrets, public.document)
    _drop(secrets)
    return payload, 0


def _perform(jobs, transport, document, secrets, registry, page_cap, stop_timestamp, clock):
    reads: list[dict] = []
    edges = []
    payer_contract: bool | None = None
    adopted: list[bool] = []
    sources = document["sources"]
    for job in jobs:
        for choice in job.choices:
            if choice.role == "skip":
                outcome = Outcome(choice.read_status or "gap", choice.reason, [])
                reads.append(_read_row(job, choice, outcome))
                adopted.append(outcome.status == "complete")
                continue
            outcome = _run_choice(
                job, choice, transport, sources, secrets, registry, page_cap, stop_timestamp, clock,
            )
            if outcome.payer_is_contract is True:
                payer_contract = True
            reads.append(_read_row(job, choice, outcome))
            edges.extend(outcome.edges)
            adopted.append(outcome.status == "complete")
    return reads, edges, payer_contract, bool(adopted) and all(adopted)


def _run_choice(job: Job, choice, transport, sources, secrets, registry, page_cap, stop_timestamp, clock) -> Outcome:
    source = sources.get(choice.source_id) or {}
    entry = registry[job.network]
    family = entry["family"]
    if choice.adapter == "blockscout":
        return read_blockscout(
            transport,
            choice.source_id,
            credential=_credential(source, secrets),
            address=job.address,
            direction=job.direction,
            network=job.network,
            family=family,
            page_cap=page_cap,
            stop_timestamp=stop_timestamp,
            entry=entry,
            clock=clock,
        )
    if choice.adapter == "tron_grid":
        return read_tron(
            transport,
            choice.source_id,
            address=job.address,
            direction=job.direction,
            network=job.network,
            page_cap=page_cap,
            stop_timestamp=stop_timestamp,
            clock=clock,
        )
    return Outcome("gap", "endpoint_missing", [])


def _credential(source: Mapping[str, Any], secrets: Mapping[str, str]) -> str | None:
    name = source.get("credential_env")
    if not isinstance(name, str) or not name:
        return None
    try:
        return _profile_credential(name, secrets)
    except ValueError:
        return None


def _profile_credential(name: str, secrets: Mapping[str, str]) -> str:
    """Profile value first, then the process environment."""
    if not isinstance(name, str) or _CREDENTIAL_ENV.fullmatch(name) is None:
        raise ValueError("credential_env")
    found = resolve_named(name, secrets, os.environ)
    if not found.present:
        raise ValueError("credential_missing")
    return found.reveal()


def _read_row(job: Job, choice, outcome: Outcome) -> dict:
    row = {
        "address": job.address,
        "direction": job.direction,
        "ignored_zero_value": outcome.ignored_zero_value,
        "network": job.network,
        "parts": list(outcome.parts or []),
        "read_status": outcome.status,
        "reason": outcome.reason,
        "source_id": choice.source_id,
    }
    if choice.gap:
        row["gap"] = choice.gap
    return row


def _seeds(args, private, command: str) -> list[tuple[str, str]]:
    raw = getattr(args, "seed", None)
    found: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    if raw:
        items = [_classify(text) for text in raw]
    else:
        items = [(record.family, record.normalized.canonical) for record in private.accounts.accounts]
    if not items and command == "inbound":
        raise ConfigError("seed_required")
    for item in items:
        if item not in seen:
            seen.add(item)
            found.append(item)
    return found


def _payer(args, command: str) -> tuple[str, str] | None:
    if command != "expand":
        return None
    return _classify(getattr(args, "payer", ""))


def _classify(text: str) -> tuple[str, str]:
    if not isinstance(text, str) or not text.strip():
        raise ConfigError("bad_address")
    raw = text.strip()
    if raw.startswith("0x") and len(raw) == 42:
        return _parsed("evm", raw)
    if raw.startswith("T") and len(raw) == 34:
        try:
            return _parsed("tron", raw)
        except ConfigError:
            return _parsed("solana", raw)
    if raw.lower().startswith(("bc1", "tb1", "bcrt1")):
        return _parsed("bitcoin", raw)
    if raw[:1] in {"1", "3"}:
        try:
            return _parsed("bitcoin", raw)
        except ConfigError:
            return _parsed("solana", raw)
    return _parsed("solana", raw)


def _parsed(family: str, raw: str) -> tuple[str, str]:
    try:
        parsed = parse_address(family, raw)
    except ValueError as exc:
        raise ConfigError(getattr(exc, "reason", "bad_address")) from None
    return family, parsed.canonical


def _envelope(command, status, since_days, max_pages, moment, plan, reads) -> dict:
    return {
        "command": command,
        "generated_at": datetime.fromtimestamp(moment, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "max_pages": max_pages,
        "plan": plan,
        "reads": reads,
        "since_days": since_days,
        "status": status,
    }


def _store(state_dir: Path, command: str, payload: dict, moment: float, secrets: Mapping[str, str], document: Mapping[str, Any]) -> dict:
    directory = Path(state_dir) / "relations"
    stamp = datetime.fromtimestamp(moment, timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    path = directory / f"{stamp}-{command}.json"
    suffix = 2
    while path.exists():
        path = directory / f"{stamp}-{command}-{suffix}.json"
        suffix += 1
    payload["result_path"] = str(path)
    extra = _extra_secrets(secrets, document)
    text = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    cleaned = redact(text, extra)
    if cleaned != text:
        payload = json.loads(cleaned)
        text = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    secure_write_text(path, text)
    return payload


def _queried_nodes(jobs) -> int:
    seen: set[tuple[str, str]] = set()
    for job in jobs:
        if any(choice.role != "skip" for choice in job.choices):
            seen.add((job.network, job.address))
    return len(seen)


def _extra_secrets(secrets: Mapping[str, str], document: Mapping[str, Any]) -> list[str]:
    extra = [value for value in secrets.values() if isinstance(value, str) and len(value) >= 6]
    seen = set(extra)
    sources = document.get("sources") if isinstance(document, Mapping) else None
    if not isinstance(sources, Mapping):
        return extra
    for source in sources.values():
        if not isinstance(source, Mapping):
            continue
        name = source.get("credential_env")
        if not isinstance(name, str) or not name:
            continue
        try:
            value = _profile_credential(name, secrets)
        except ValueError:
            continue
        if isinstance(value, str) and len(value) >= 6 and value not in seen:
            seen.add(value)
            extra.append(value)
    return extra


def _bounded_int(value: Any, name: str, low: int, high: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < low or value > high:
        raise ConfigError(name)
    return value


def _stop(moment: float, since_days: int) -> int:
    stamp = int(moment) - since_days * 86400
    return stamp if stamp > 0 else 0


def _drop(secrets: dict) -> None:
    secrets.clear()
