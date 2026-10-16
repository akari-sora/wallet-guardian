"""English Markdown report beside a relations JSON result. No full URLs."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence

from guardian.relations.evidence import asset_name

CLOSING = (
    "This report lists addresses and heuristic evidence only. "
    "Shared transactions, assets, amounts or timing do not show that two addresses "
    "are controlled by the same party."
)
_ALLOWED = set("0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz\u2026: ")
_MD_SPECIAL = set("\\`*_[]()<>!#|~")
_URL = re.compile(r"(?i)[a-z][a-z0-9+.\-]*://\S*")


def render_markdown(payload: Mapping, *, max_nodes: int = 500) -> str:
    command = str(payload.get("command") or "relations")
    lines = [
        f"# Relations {command}",
        f"Generated: {payload.get('generated_at') or ''}",
        "",
        "## Window",
        f"Since days: {payload.get('since_days')}",
        f"Status: {payload.get('status')}",
    ]
    if payload.get("payer"):
        lines.append(f"Payer: {payload.get('payer')}")
    networks = payload.get("networks")
    if isinstance(networks, list) and networks:
        lines.append("Networks: " + ", ".join(str(item) for item in networks))
    elif payload.get("network"):
        lines.append(f"Network: {payload.get('network')}")
    lines.extend(_window_hub(payload))
    lines.extend(_contract_check_lines(payload))
    lines.extend(["", "## Disclosure plan"])
    lines.extend(_plan_lines(payload.get("plan") or {}))
    if payload.get("status") in {"dry_run", "consent_required"}:
        lines.extend(["", CLOSING, ""])
        return _strip_urls("\n".join(lines))
    lines.extend(["", "## Reads"])
    lines.extend(_reads_table(payload.get("reads") or []))
    title = "Co-recipients" if command == "expand" else "Counterparties"
    key = "co_recipients" if command == "expand" else "counterparties"
    if key in payload:
        rows = payload.get(key)
        lines.extend(["", f"## {title}"])
        if isinstance(rows, list):
            lines.extend(_main_table(rows, expand=command == "expand"))
            if command == "expand" and rows:
                lines.extend(["", *_mermaid(payload, rows, max_nodes)])
        else:
            lines.append("unknown")
    lines.extend(_seed_lines(payload))
    lines.extend(_merged_section(payload))
    lines.extend(["", "## Gaps"])
    lines.extend(_gap_lines(payload))
    lines.extend(["", CLOSING, ""])
    return _strip_urls("\n".join(lines))


def _strip_urls(text: str) -> str:
    return _URL.sub("[url removed]", text)


def _plan_lines(plan: Mapping) -> list[str]:
    queries = plan.get("queries") or []
    if not queries:
        return ["No host receives an address."]
    lines: list[str] = []
    for query in queries:
        if not isinstance(query, Mapping):
            continue
        addresses = [str(item) for item in (query.get("addresses") or [])]
        host = str(query.get("host") or "")
        lines.append(f"{host} receives {len(addresses)} addresses:")
        lines.extend(f"- {address}" for address in addresses)
    return lines or ["No host receives an address."]


def _reads_table(reads: Sequence) -> list[str]:
    lines = [
        "| network | source_id | read_status | reason |",
        "| --- | --- | --- | --- |",
    ]
    for row in reads:
        if not isinstance(row, Mapping):
            continue
        lines.append("| " + " | ".join([
            _cell(row.get("network")),
            _cell(row.get("source_id")),
            _cell(row.get("read_status")),
            _cell(row.get("reason")),
        ]) + " |")
    return lines


def _main_table(rows: Sequence[Mapping], *, expand: bool) -> list[str]:
    if expand:
        header = "| address | network | count | assets | confidence | score | reasons | tx_hashes | label |"
        rule = "| --- | --- | --- | --- | --- | --- | --- | --- | --- |"
    else:
        header = "| address | network | count | assets | reasons | tx_hashes | label |"
        rule = "| --- | --- | --- | --- | --- | --- | --- |"
    lines = [header, rule]
    for row in rows:
        hashes = ", ".join(str(item) for item in list(row.get("tx_hashes") or [])[:3])
        reasons = "; ".join(str(item) for item in (row.get("reasons") or []))
        base = [
            _cell(row.get("address")),
            _cell(row.get("network")),
            _cell(row.get("count")),
            _cell(_assets_text(row.get("assets") or [])),
        ]
        if expand:
            base.extend([
                _cell(row.get("confidence")),
                _cell(row.get("score")),
                _cell(reasons),
                _cell(hashes),
                _cell(row.get("label")),
            ])
        else:
            base.extend([_cell(reasons), _cell(hashes), _cell(row.get("label"))])
        lines.append("| " + " | ".join(base) + " |")
    return lines


def _seed_lines(payload: Mapping) -> list[str]:
    if "seed_receipts" not in payload and "evidence_note" not in payload:
        return []
    lines = ["", "## Seed receipts"]
    receipts = payload.get("seed_receipts")
    if not isinstance(receipts, Mapping):
        lines.append("unknown")
    else:
        lines.append(f"Count: {receipts.get('count')}")
    note = payload.get("evidence_note")
    if isinstance(note, str) and note:
        lines.append(f"Note: {note}")
    return lines


def _merged_section(payload: Mapping) -> list[str]:
    if "merged_addresses" not in payload:
        return []
    lines = ["", "## Merged addresses"]
    rows = payload.get("merged_addresses")
    if not isinstance(rows, list):
        lines.append("unknown")
        return lines
    lines.extend(_merged_lines(rows))
    return lines


def _merged_lines(rows: Sequence) -> list[str]:
    if not rows:
        return ["No merged addresses."]
    lines = []
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        networks = ", ".join(str(item) for item in (row.get("networks") or []))
        text = (
            f"- {_cell(row.get('address'))} on {networks}; count {row.get('count')}; "
            f"first {row.get('first_timestamp')}; last {row.get('last_timestamp')}; "
            f"tx_hash_count {row.get('tx_hash_count')}; is_contract {row.get('is_contract')}"
        )
        if "score" in row:
            text += f"; score {row.get('score')}; confidence {row.get('confidence')}"
        lines.append(text)
    return lines or ["No merged addresses."]


def _gap_lines(payload: Mapping) -> list[str]:
    lines = []
    for row in payload.get("reads") or []:
        if not isinstance(row, Mapping) or row.get("read_status") == "complete":
            continue
        lines.append(
            f"- {row.get('network')} {row.get('source_id')} {row.get('read_status')} {row.get('reason')}"
        )
    plan = payload.get("plan") or {}
    for row in plan.get("unqueried") or []:
        if not isinstance(row, Mapping):
            continue
        lines.append(
            f"- {row.get('network')} {row.get('address')} {row.get('read_status')} {row.get('reason')}"
        )
    return lines or ["None"]


def _contract_check_lines(payload: Mapping) -> list[str]:
    check = payload.get("contract_check")
    if not isinstance(check, Mapping):
        return []
    status = check.get("status")
    if status in {None, "", "complete", "ok"}:
        return []
    if "co_recipients" in payload:
        return []
    return ["Contract check failed; pass --expand-hub to list them."]


def _window_hub(payload: Mapping) -> list[str]:
    if payload.get("hub") is not True or "co_recipients" in payload:
        return []
    reasons = payload.get("hub_reasons")
    if not isinstance(reasons, list):
        reasons = []
    shown = ", ".join(str(item) for item in reasons)
    return [
        f"Hub: true ({shown})",
        "Co-recipients are not listed for a hub payer; pass --expand-hub to list them.",
    ]


def _mermaid(payload: Mapping, rows: Sequence[Mapping], max_nodes: int) -> list[str]:
    payer = str(payload.get("payer") or "")
    raw_seeds = payload.get("seeds") if isinstance(payload.get("seeds"), list) else []
    chosen_seeds = []
    for seed in raw_seeds:
        seed = str(seed or "")
        if not seed or _same(seed, payer) or seed in chosen_seeds:
            continue
        chosen_seeds.append(seed)
    limit = min(20, max_nodes) if max_nodes > 0 else 0
    chosen_rows = []
    seen: set[str] = set()
    for row in rows:
        address = str(row.get("address") or "")
        key = address.lower() if address.startswith("0x") else address
        if not key or key in seen or _same(address, payer):
            continue
        seen.add(key)
        chosen_rows.append(row)
        if len(chosen_rows) >= limit:
            break
    lines = ["```mermaid", "graph LR"]
    ids: list[str] = []
    for index, seed in enumerate(chosen_seeds):
        node = f"n{index}"
        ids.append(node)
        lines.append(f'  {node}["{_label(_short(seed))}"]')
    payer_id = f"n{len(ids)}"
    lines.append(f'  {payer_id}["{_label(_short(payer))}"]')
    for node in ids:
        lines.append(f"  {node} --> {payer_id}")
    base = len(ids) + 1
    for offset, row in enumerate(chosen_rows):
        node = f"n{base + offset}"
        text = _label(f"{_short(str(row.get('address') or ''))} {row.get('confidence') or ''}".strip())
        lines.append(f'  {node}["{text}"]')
        lines.append(f"  {payer_id} --> {node}")
    lines.append("```")
    return lines


def _assets_text(assets: Sequence) -> str:
    parts = []
    for asset in assets:
        if not isinstance(asset, Mapping):
            continue
        name = asset_name(asset)
        parts.append(f"{name} raw {asset.get('raw_amount')} decimals {asset.get('decimals')}")
    return "; ".join(parts)


def _short(address: str) -> str:
    if len(address) <= 12:
        return address
    return address[:6] + "\u2026" + address[-4:]


def _label(text: str) -> str:
    return "".join(ch for ch in text if ch in _ALLOWED)


def _cell(value: object) -> str:
    if value is None:
        return ""
    text = str(value).replace("\r\n", " ").replace("\n", " ").replace("\r", " ")
    text = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    return "".join("\\" + char if char in _MD_SPECIAL else char for char in text)


def _same(left: str, right: str) -> bool:
    if left.startswith("0x") or right.startswith("0x"):
        return left.lower() == right.lower()
    return left == right
