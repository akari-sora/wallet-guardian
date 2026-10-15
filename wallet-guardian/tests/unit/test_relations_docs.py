"""Offline checks for docs/relations.md.

The test reads committed files and the argparse tree from build_parser().
It does not open a socket and it does not run a command subprocess.
"""

from __future__ import annotations

import argparse
import re
from pathlib import Path

from guardian.cli import build_parser

ROOT = Path(__file__).resolve().parents[2]
RELATIONS = ROOT / "docs" / "relations.md"
README = ROOT / "README.md"
REFERENCE = ROOT / "docs" / "cli-reference.md"

_REQUIRED = (
    "credential_missing",
    "bsc_source_gated",
    "unsupported",
    "source_gated",
    "etherscan_deferred",
    "redirect_host",
    "bot_challenge",
    "noise",
    "--yes",
    "--dry-run",
    "merged_addresses",
    "relations-labels.json",
    "0600",
)
_HEADINGS = (
    "## What it is",
    "## Layers",
    "## Disclosure and consent",
    "## Flags",
    "## Sources by network",
    "## Read status",
    "## Hub rule",
    "## Evidence and confidence",
    "## EVM cross-chain merge",
    "## Private labels file",
    "## Output files",
    "## Limits",
    "## Not built",
)
_FLAG = re.compile(r"(?<![\w-])--[A-Za-z0-9][A-Za-z0-9-]*")
_BANNED = (
    "real-time",
    "realtime",
    "all chains",
    "every chain",
    "complete coverage",
    "identify the owner",
    "find out who",
)
_NEGATION = re.compile(r"\b(?:not|no|never)\b|does not", re.IGNORECASE)


def _codes(*points: int) -> str:
    return "".join(chr(point) for point in points)


# Denylist tokens are built from code points so this file does not spell them.
_NEEDLES = (
    _codes(0x70, 0x65, 0x72, 0x73, 0x6F, 0x6E),
    _codes(0x70, 0x65, 0x6F, 0x70, 0x6C, 0x65),
    _codes(0x69, 0x6E, 0x64, 0x69, 0x76, 0x69, 0x64, 0x75, 0x61, 0x6C),
    _codes(0x63, 0x6F, 0x77, 0x6F, 0x72, 0x6B, 0x65, 0x72),
    _codes(0x63, 0x6F, 0x6C, 0x6C, 0x65, 0x61, 0x67, 0x75, 0x65),
    _codes(0x65, 0x6D, 0x70, 0x6C, 0x6F, 0x79, 0x65, 0x65),
    _codes(0x69, 0x64, 0x65, 0x6E, 0x74, 0x69, 0x74, 0x79),
)
_ZH = (
    _codes(0x5373, 0x6642),
    _codes(0x5168, 0x93C8),
    _codes(0x5B8C, 0x6574),
    _codes(0x80FD, 0x627E, 0x51FA),
)


def _text(path: Path) -> str:
    raw = path.read_bytes()
    assert b"\r" not in raw
    return raw.decode("utf-8")


def _child(parser: argparse.ArgumentParser, name: str) -> argparse.ArgumentParser:
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction) and name in action.choices:
            chosen = action.choices[name]
            assert isinstance(chosen, argparse.ArgumentParser)
            return chosen
    raise AssertionError(name)


def _option_flags(parser: argparse.ArgumentParser) -> set[str]:
    found: set[str] = set()
    for action in parser._actions:
        for opt in action.option_strings:
            if opt.startswith("--"):
                found.add(opt)
    return found


def _relations_flags() -> set[str]:
    parser = build_parser()
    relations = _child(parser, "relations")
    return _option_flags(_child(relations, "inbound")) | _option_flags(_child(relations, "expand"))


def _status_bullets(readme: str) -> list[str]:
    lines = readme.splitlines()
    start = next(index for index, line in enumerate(lines) if line.startswith("## Status and limits"))
    bullets: list[str] = []
    for line in lines[start + 1:]:
        if line.startswith("## "):
            break
        if line.startswith("- "):
            bullets.append(line)
            continue
        if line.strip() == "":
            continue
        if bullets:
            break
    return bullets


def _sentences(text: str) -> list[str]:
    parts = re.split(r"[.!?]+|\n+", text)
    return [part.strip() for part in parts if part.strip()]


def _assert_wording(text: str) -> None:
    lowered = text.lower()
    for phrase in _BANNED:
        assert phrase not in lowered
    for token in _ZH:
        assert token not in text
    for sentence in _sentences(text):
        for needle in _NEEDLES:
            if re.search(rf"\b{re.escape(needle)}\b", sentence, re.IGNORECASE):
                assert _NEGATION.search(sentence), sentence


def test_relations_doc_names_the_limits() -> None:
    """The relations page exists and names the gated and unsupported outcomes."""
    assert RELATIONS.is_file()
    text = _text(RELATIONS)
    for token in _REQUIRED:
        assert token in text
    headings = [line for line in text.splitlines() if line.startswith("## ") and not line.startswith("###")]
    assert headings == list(_HEADINGS)
    reference = _text(REFERENCE)
    sentence = "See docs/relations.md for semantics and limits."
    assert reference.count(sentence) == 1
    start = reference.index("## `guardian relations expand`")
    fence = reference.index("```text\n", start)
    fence_end = reference.index("\n```", fence)
    assert sentence not in reference[fence:fence_end]
    assert sentence in reference[fence_end:]


def test_relations_doc_flags_exist_on_the_parsers() -> None:
    """Every --flag in the relations page is accepted by inbound or expand."""
    documented = set(_FLAG.findall(_text(RELATIONS)))
    live = _relations_flags()
    assert documented
    assert documented <= live
    for flag in ("--yes", "--dry-run", "--since-days", "--max-pages", "--max-nodes", "--network", "--markdown", "--hub-threshold", "--expand-hub"):
        assert flag in documented
        assert flag in live


def test_relations_doc_and_readme_line_avoid_overclaim() -> None:
    """The page and the new status line stay inside the negative wording rule."""
    page = _text(RELATIONS)
    bullets = _status_bullets(_text(README))
    hits = [line for line in bullets if "guardian relations" in line]
    assert len(hits) == 1
    assert "docs/relations.md" in hits[0]
    _assert_wording(page)
    _assert_wording(hits[0])


def test_readme_status_list_mentions_relations_once() -> None:
    """The status list has one relations bullet, and that bullet links the page."""
    bullets = _status_bullets(_text(README))
    hits = [line for line in bullets if "guardian relations" in line]
    assert hits == [
        "- **`guardian relations` (manual, read-only):** lists addresses that paid a watched address and addresses a chosen payer also sent funds to, with heuristic evidence only; a gap stays a gap. See [docs/relations.md](docs/relations.md).",
    ]
