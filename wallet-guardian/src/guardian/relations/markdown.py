"""English Markdown report beside a relations JSON result. No full URLs."""

from __future__ import annotations

from collections.abc import Mapping, Sequence

CLOSING = (
    "This report lists addresses and heuristic evidence only. "
    "Shared transactions, assets, amounts or timing do not show that two addresses "
    "are controlled by the same party."
)
_ALLOWED = set("0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz\u2026: ")


def render_markdown(
    payload: Mapping,
    *,
    seeds: Sequence[str] = (),
    max_nodes: int = 500,
) -> str:
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
    lines.extend(["", "## Disclosure plan"])
    lines.extend(_plan_lines(payload.get("plan") or {}))
    if payload.get("status") in {"dry_run", "consent_required"}:
        lines.extend(["", CLOSING, ""])
        return "\n".join(lines).replace("https://", "").replace("http://", "")
    lines.extend(["", "## Reads"])
    lines.extend(_reads_table(payload.get("reads") or []))
    title = "Co-recipients" if command == "expand" else "Counterparties"
    key = "co_recipients" if command == "expand" else "counterparties"
    rows = payload.get(key)
    if not isinstance(rows, list):
        rows = []
    lines.extend(["", f"## {title}"])
    lines.extend(_main_table(rows, expand=command == "expand"))
    if command == "expand" and rows:
        lines.extend(["", *_mermaid(payload, rows, seeds, max_nodes)])
    lines.extend(["", "## Merged addresses"])
    lines.extend(_merged_lines(payload.get("merged_addresses") or []))
    lines.extend(["", "## Gaps"])
    lines.extend(_gap_lines(payload))
    lines.extend(["", CLOSING, ""])
    return "\n".join(lines).replace("https://", "").replace("http://", "")


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
        header = "| address | network | count | assets | confidence | score | reasons | tx_hashes |"
        rule = "| --- | --- | --- | --- | --- | --- | --- | --- |"
    else:
        header = "| address | network | count | assets | reasons | tx_hashes |"
        rule = "| --- | --- | --- | --- | --- | --- |"
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
            ])
        else:
            base.extend([_cell(reasons), _cell(hashes)])
        lines.append("| " + " | ".join(base) + " |")
    return lines


def _merged_lines(rows: Sequence) -> list[str]:
    if not rows:
        return ["None"]
    lines = []
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        networks = ", ".join(str(item) for item in (row.get("networks") or []))
        text = (
            f"- {row.get('address')} on {networks}; count {row.get('count')}; "
            f"first {row.get('first_timestamp')}; last {row.get('last_timestamp')}; "
            f"tx_hash_count {row.get('tx_hash_count')}; is_contract {row.get('is_contract')}"
        )
        if "score" in row:
            text += f"; score {row.get('score')}; confidence {row.get('confidence')}"
        lines.append(text)
    return lines or ["None"]


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


def _mermaid(payload: Mapping, rows: Sequence[Mapping], seeds: Sequence[str], max_nodes: int) -> list[str]:
    payer = str(payload.get("payer") or "")
    chosen_seeds = []
    for seed in seeds:
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
        name = asset.get("symbol") or asset.get("contract") or asset.get("kind") or ""
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
    return str(value).replace("|", " ").replace("\n", " ").replace("\r", " ")


def _same(left: str, right: str) -> bool:
    if left.startswith("0x") or right.startswith("0x"):
        return left.lower() == right.lower()
    return left == right
