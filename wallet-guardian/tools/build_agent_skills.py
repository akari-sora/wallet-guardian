"""Generate the agent command catalog and the flattened Grok Bot skill.

The classification source is ``src/guardian/_resources/agent-capabilities.json``.
``--check`` compares generated files to the tree and exits non-zero on drift.
This tool does not install anything and does not open a network connection.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

_LINK = re.compile(r"\[([^\]]+)\]\((references/[^)]+)\)")
_GROK_NOTE = """## Grok Bot host

This file inlines the reference pages because a Grok Bot skill upload keeps only this text. The cloud computer for that account must already have the `guardian` command installed. Bots on the same account share that computer and its data. This host integration was not exercised (未實測).
"""


def project_root() -> Path:
    return Path(__file__).resolve().parents[1]


def repo_root() -> Path:
    return project_root().parent


def catalog_path() -> Path:
    return project_root() / "src" / "guardian" / "_resources" / "agent-capabilities.json"


def skill_dir() -> Path:
    return repo_root() / "skills" / "wallet-guardian"


def commands_path() -> Path:
    return skill_dir() / "references" / "commands.md"


def safety_path() -> Path:
    return skill_dir() / "references" / "safety.md"


def skill_path() -> Path:
    return skill_dir() / "SKILL.md"


def grok_path() -> Path:
    return repo_root() / "skills" / "dist" / "grok-bot" / "SKILL.md"


def load_catalog(path: Path | None = None) -> dict:
    payload = json.loads((path or catalog_path()).read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("catalog is not an object")
    return payload


def _cell(value: object) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    text = str(value).replace("|", "/")
    return text


def render_commands(catalog: dict) -> str:
    """Render the command reference from the catalog only."""
    lines = [
        "# guardian command classes",
        "",
        "Generated from wallet-guardian/src/guardian/_resources/agent-capabilities.json. Do not edit by hand.",
        "",
        f"Package version in this catalog: `{catalog['version']}`.",
        "",
        "Run `guardian capabilities` to print this catalog from the installed command. An agent must not treat a gap as safe, as zero, or as complete.",
        "",
        "## Classes",
        "",
    ]
    classes = catalog["classes"]
    for name in (
        "A_read_only_offline",
        "B_network_requires_owner_ok",
        "C_writes_local_state_owner_decides",
        "D_never_from_agent",
    ):
        lines.append(f"- `{name}`: {classes[name]}")
    lines.extend(["", "## Exit codes", ""])
    lines.append("| code | meaning |")
    lines.append("|---|---|")
    for row in catalog["exit_codes"]:
        lines.append(f"| {row['code']} | {_cell(row['meaning'])} |")
    lines.extend(["", "## Gap rules", ""])
    for rule in catalog["gap_rules"]:
        lines.append(f"- {rule}")
    lines.extend(["", "## Classification notes", ""])
    for note in catalog["classification_notes"]:
        lines.append(f"- `{note['id']}`: {note['note']}")
    lines.extend(["", "## Commands", ""])
    for command in catalog["commands"]:
        joined = " ".join(command["path"])
        lines.append(f"### `guardian {joined}`")
        lines.append("")
        lines.append(command["help"])
        lines.append("")
        lines.append("| variant | class | network | writes_local_state | requires_no_send | requires_yes | when |")
        lines.append("|---|---|---|---|---|---|---|")
        for variant in command["variants"]:
            lines.append(
                "| `{id}` | `{klass}` | {network} | {writes} | {no_send} | {yes} | {when} |".format(
                    id=variant["id"],
                    klass=variant["class"],
                    network=_cell(variant["network"]),
                    writes=_cell(variant["writes_local_state"]),
                    no_send=_cell(variant["requires_no_send"]),
                    yes=_cell(variant["requires_yes"]),
                    when=_cell(variant["when"]),
                )
            )
        lines.append("")
        for variant in command["variants"]:
            lines.append(f"- `{variant['id']}`: {variant['notes']}")
        lines.append("")
    text = "\n".join(lines).rstrip() + "\n"
    if "](" in text:
        raise ValueError("commands.md must not contain a markdown link")
    return text


def _frontmatter_version(text: str) -> str:
    if not text.startswith("---\n"):
        raise ValueError("skill frontmatter missing")
    end = text.find("\n---\n", 4)
    if end < 0:
        raise ValueError("skill frontmatter missing")
    match = re.search(r'(?m)^  version:\s*"([^"]+)"\s*$', text[4:end])
    if match is None:
        raise ValueError("skill metadata.version missing")
    return match.group(1)


def render_grok(skill: str, references: dict[str, str], catalog_version: str) -> str:
    """Inline reference pages. The result has no relative links."""
    if _frontmatter_version(skill) != catalog_version:
        raise ValueError("skill metadata.version does not match the catalog")

    def replace(match: re.Match[str]) -> str:
        label = match.group(1)
        target = match.group(2)
        if target not in references:
            raise ValueError(f"unknown skill reference {target}")
        body = references[target].strip()
        return f"{label}\n\n{body}\n"

    flattened = _LINK.sub(replace, skill).rstrip() + "\n\n" + _GROK_NOTE.strip() + "\n"
    if "](" in flattened:
        raise ValueError("grok skill still has a markdown link")
    if "references/" in flattened:
        raise ValueError("grok skill still names a references path")
    return flattened


def generated_files(root: Path | None = None) -> dict[Path, str]:
    base = project_root() if root is None else Path(root)
    catalog = load_catalog(base / "src" / "guardian" / "_resources" / "agent-capabilities.json")
    skill_root = base.parent / "skills" / "wallet-guardian"
    skill = (skill_root / "SKILL.md").read_text(encoding="utf-8")
    safety = (skill_root / "references" / "safety.md").read_text(encoding="utf-8")
    commands = render_commands(catalog)
    grok = render_grok(
        skill,
        {"references/safety.md": safety, "references/commands.md": commands},
        str(catalog["version"]),
    )
    return {
        skill_root / "references" / "commands.md": commands,
        base.parent / "skills" / "dist" / "grok-bot" / "SKILL.md": grok,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build agent skill files from the capability catalog")
    parser.add_argument("--check", action="store_true", help="exit non-zero if generated files differ")
    parser.add_argument("--root", default=None, help="wallet-guardian project root")
    args = parser.parse_args(argv)
    root = Path(args.root) if args.root else project_root()
    try:
        files = generated_files(root)
    except (OSError, ValueError, json.JSONDecodeError, KeyError) as exc:
        sys.stderr.write(str(exc) + "\n")
        return 1
    drifted: list[str] = []
    for path, text in files.items():
        current = path.read_text(encoding="utf-8") if path.is_file() else None
        if current != text:
            drifted.append(str(path))
            if not args.check:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(text, encoding="utf-8", newline="\n")
    if drifted and args.check:
        sys.stderr.write("skill drift:\n" + "\n".join(drifted) + "\n")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
