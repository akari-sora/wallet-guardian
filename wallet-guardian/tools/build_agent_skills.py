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

_TRAILING_PUNCT = set(".。!！?？:：;；,")
_LINK = re.compile(
    r"\[([^\]]+)\]\((references/[^)]+)\)([.。!！?？:：;；,])?(?=\r?\n|$)"
)
_REF_LINK = re.compile(r"\[([^\]]+)\]\((references/[^)]+)\)")
_ATX = re.compile(r"^(#{1,6})([ \t]+.*)$")
_FENCE_MARK = re.compile(r"^[ ]{0,3}(`{3,}|~{3,})(.*)$")
_INDENTED_ATX = re.compile(r"^[ ]{1,3}#{1,6}(?:[ \t]|$)")
_EMPTY_ATX = re.compile(r"^#{1,6}[ \t]*$")
_SETEXT_LINE = re.compile(r"^[ ]{0,3}(?:=+|-+)[ \t]*$")
_FLEX_ATX = re.compile(r"^[ ]{0,3}(#{1,6})(?:[ \t]|$)")
_ORPHAN_PUNCT = set(".。!！?？:：;；,")
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


def _is_indented_code(line: str) -> bool:
    return line.startswith("\t") or line.startswith("    ")


def _is_fence_line(line: str) -> bool:
    return _FENCE_MARK.match(line) is not None


def _step_fence(line: str, open_fence: tuple[str, int] | None) -> tuple[str, int] | None:
    """Move one line through a CommonMark fence.

    The returned pair is the open mark and its length. Only a line of the same
    mark, at least as long, indented at most three spaces, and otherwise blank,
    closes it. A backtick opener whose info string contains a backtick is not a fence.
    """
    match = _FENCE_MARK.match(line)
    if open_fence is not None:
        marker, length = open_fence
        if (
            match is not None
            and match.group(1)[0] == marker
            and len(match.group(1)) >= length
            and match.group(2).strip(" \t") == ""
        ):
            return None
        return open_fence
    if match is None:
        return None
    marks = match.group(1)
    if marks[0] == "`" and "`" in match.group(2):
        return None
    return (marks[0], len(marks))


def _frontmatter_body_start(text: str) -> int:
    """Index just after a leading YAML frontmatter block, or 0 when absent."""
    if not text.startswith("---\n"):
        return 0
    end = text.find("\n---\n", 4)
    if end < 0:
        return 0
    return end + len("\n---\n")


def _trim_blank_edges(text: str) -> str:
    """Drop blank lines at the ends. Keep each remaining line's indentation."""
    lines = text.splitlines()
    start = 0
    end = len(lines)
    while start < end and lines[start] == "":
        start += 1
    while end > start and lines[end - 1] == "":
        end -= 1
    return "\n".join(lines[start:end])


def _heading_levels(text: str) -> list[int]:
    body = text[_frontmatter_body_start(text):]
    open_fence: tuple[str, int] | None = None
    found: list[int] = []
    for line in body.splitlines():
        if open_fence is not None:
            open_fence = _step_fence(line, open_fence)
            continue
        if _is_indented_code(line):
            continue
        open_fence = _step_fence(line, None)
        if open_fence is not None:
            continue
        match = _FLEX_ATX.match(line)
        if match:
            found.append(len(match.group(1)))
    return found


def _demote_headings(text: str, shift: int) -> str:
    """Lower ATX headings so an inlined page sits under the outer section."""
    if shift <= 0:
        return text
    open_fence: tuple[str, int] | None = None
    lines: list[str] = []
    for line in text.splitlines():
        if open_fence is not None:
            open_fence = _step_fence(line, open_fence)
            lines.append(line)
            continue
        if _is_indented_code(line):
            lines.append(line)
            continue
        opened = _step_fence(line, None)
        if opened is not None:
            open_fence = opened
            lines.append(line)
            continue
        match = _ATX.match(line)
        if match:
            level = len(match.group(1)) + shift
            if level > 6:
                raise ValueError("embedded heading would pass level 6")
            line = ("#" * level) + match.group(2)
        lines.append(line)
    return "\n".join(lines)


def _validate_reference_links(skill: str) -> None:
    """A reference link ends its line, with at most one punctuation mark."""
    for match in _REF_LINK.finditer(skill):
        newline = skill.find("\n", match.end())
        tail = skill[match.end():] if newline < 0 else skill[match.end():newline]
        tail = tail.removesuffix("\r")
        if tail == "" or (len(tail) == 1 and tail in _TRAILING_PUNCT):
            continue
        raise ValueError("a reference link must end the line, with at most one punctuation mark")


def _reject_unhandled_reference(text: str, target: str) -> None:
    """Fences, indented code, empty ATX headings, and setext underlines are rejected."""
    for line in text.splitlines():
        if (
            _is_fence_line(line)
            or _INDENTED_ATX.match(line)
            or _is_indented_code(line)
            or _EMPTY_ATX.match(line)
            or _SETEXT_LINE.match(line)
        ):
            raise ValueError(f"{target} has a fence or an indented heading")


def _reject_outer_setext(skill: str) -> None:
    """Reject setext underlines in the outer skill. YAML frontmatter is not one."""
    body = skill[_frontmatter_body_start(skill):]
    open_fence: tuple[str, int] | None = None
    for line in body.splitlines():
        if open_fence is not None:
            open_fence = _step_fence(line, open_fence)
            continue
        if _is_indented_code(line):
            continue
        open_fence = _step_fence(line, None)
        if open_fence is not None:
            continue
        if _SETEXT_LINE.match(line):
            raise ValueError("skill has a setext underline")


def _outer_content_follows(skill: str, match_end: int, parent_level: int) -> bool:
    """True when the link's section still has text before its next heading."""
    body_start = _frontmatter_body_start(skill)
    open_fence: tuple[str, int] | None = None
    after = False
    offset = 0
    for line in skill.splitlines(keepends=True):
        raw = line.removesuffix("\n").removesuffix("\r")
        end = offset + len(line)
        if end <= body_start:
            offset = end
            continue
        indented = open_fence is None and _is_indented_code(raw)
        stepped = open_fence if indented else _step_fence(raw, open_fence)
        if not after:
            open_fence = stepped
            if offset < match_end <= end:
                after = True
            offset = end
            continue
        if stepped != open_fence:
            return True
        if open_fence is None and not indented:
            match = _FLEX_ATX.match(raw)
            if match and len(match.group(1)) <= parent_level:
                return False
        if raw.strip():
            return True
        offset = end
    return False


def render_grok(skill: str, references: dict[str, str], catalog_version: str) -> str:
    """Inline reference pages. The result has no relative links."""
    if _frontmatter_version(skill) != catalog_version:
        raise ValueError("skill metadata.version does not match the catalog")
    _validate_reference_links(skill)
    _reject_outer_setext(skill)

    def replace(match: re.Match[str]) -> str:
        label = match.group(1)
        target = match.group(2)
        punct = match.group(3) or ""
        if target not in references:
            raise ValueError(f"unknown skill reference {target}")
        raw = references[target]
        _reject_unhandled_reference(raw, target)
        body = _trim_blank_edges(raw)
        levels = _heading_levels(body)
        if levels:
            preceding = _heading_levels(skill[: match.start()])
            parent = preceding[-1] if preceding else 1
            if _outer_content_follows(skill, match.end(), parent):
                raise ValueError(
                    f"{target} has headings, and the link is not the last content before the next heading"
                )
            body = _demote_headings(body, (parent + 1) - min(levels))
        # Sentence punctuation stays on the label line. Leaving it for the
        # next line made a file whose only character was ".".
        return f"{label}{punct}\n\n{body}\n"

    flattened = _LINK.sub(replace, skill).rstrip() + "\n\n" + _GROK_NOTE.strip() + "\n"
    if "](" in flattened:
        raise ValueError("grok skill still has a markdown link")
    if "references/" in flattened:
        raise ValueError("grok skill still names a references path")
    for line in flattened.splitlines():
        if line.strip() and set(line.strip()) <= _ORPHAN_PUNCT:
            raise ValueError("flattened skill has a punctuation-only line")
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
