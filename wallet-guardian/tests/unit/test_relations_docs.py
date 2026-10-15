"""Offline checks for docs/relations.md.

The test reads committed files and the argparse tree from build_parser().
It does not open a socket and it does not run a command subprocess.
"""

from __future__ import annotations

import argparse
import ast
import re
from pathlib import Path

import pytest

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
    "disclosure_plan",
    "stderr",
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


def _subparser(name: str) -> argparse.ArgumentParser:
    relations = _child(build_parser(), "relations")
    return _child(relations, name)


def _eval_r1_expr(node: ast.AST) -> str:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        return _eval_r1_expr(node.left) + _eval_r1_expr(node.right)
    if (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "chr"
        and len(node.args) == 1
        and isinstance(node.args[0], ast.Constant)
        and isinstance(node.args[0].value, int)
    ):
        return chr(node.args[0].value)
    raise AssertionError(type(node).__name__)


def _r1_needles() -> tuple[str, ...]:
    """Read the R-1 needle tuple. This file does not keep a second copy."""
    path = Path(__file__).with_name("test_relations_r1.py")
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        if not any(isinstance(target, ast.Name) and target.id == "_NEEDLES" for target in node.targets):
            continue
        if not isinstance(node.value, ast.Tuple):
            raise AssertionError("tuple")
        return tuple(_eval_r1_expr(item) for item in node.value.elts)
    raise AssertionError("_NEEDLES")


_ENGLISH_STEMS: tuple[str, ...] | None = None


def _english_stems() -> tuple[str, ...]:
    """ASCII stems from the R-1 needles. The ident stem drops its last letter."""
    global _ENGLISH_STEMS
    if _ENGLISH_STEMS is not None:
        return _ENGLISH_STEMS
    stems: list[str] = []
    for needle in _r1_needles():
        if not needle.isascii() or not needle.isalpha():
            continue
        if needle.startswith("ident"):
            needle = needle[:-1]
        stems.append(needle)
    _ENGLISH_STEMS = tuple(stems)
    return _ENGLISH_STEMS


def _flag_table(text: str) -> dict[str, tuple[str, str]]:
    lines = text.splitlines()
    start = next(index for index, line in enumerate(lines) if line.startswith("## Flags"))
    found: dict[str, tuple[str, str]] = {}
    for line in lines[start + 1 :]:
        if line.startswith("## "):
            break
        if not line.startswith("|"):
            continue
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if len(cells) < 3:
            continue
        flag = cells[0].strip("`")
        if _FLAG.fullmatch(flag) is None:
            continue
        found[flag] = (cells[1], cells[2])
    return found


def _command_set(cell: str) -> set[str]:
    tokens = set(re.split(r"[^a-z]+", cell.casefold()))
    if "both" in tokens:
        return {"inbound", "expand"}
    return {name for name in ("inbound", "expand") if name in tokens}


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
    parts = re.split(r"[.!?;]+|\n+", text)
    return [part.strip() for part in parts if part.strip()]


def _assert_wording(text: str) -> None:
    lowered = text.lower()
    for phrase in _BANNED:
        assert phrase not in lowered
    for token in _ZH:
        assert token not in text
    for sentence in _sentences(text):
        folded = sentence.casefold()
        for stem in _english_stems():
            if stem.casefold() in folded:
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
    """Inbound and expand each match their own flags, commands, and defaults."""
    text = _text(RELATIONS)
    documented = set(_FLAG.findall(text))
    inbound = _subparser("inbound")
    expand = _subparser("expand")
    inbound_live = _option_flags(inbound)
    expand_live = _option_flags(expand)
    table = _flag_table(text)
    inbound_doc = {flag for flag, (commands, _default) in table.items() if "inbound" in _command_set(commands)}
    expand_doc = {flag for flag, (commands, _default) in table.items() if "expand" in _command_set(commands)}
    assert documented
    assert documented <= (inbound_live | expand_live)
    assert inbound_doc == inbound_live
    assert expand_doc == expand_live
    for flag in inbound_live | expand_live:
        assert flag in text
    assert _command_set(table["--hub-threshold"][0]) == {"expand"}
    assert "--hub-threshold" not in inbound_live
    expected = {
        "--since-days": 90,
        "--max-pages": 10,
        "--max-nodes": 500,
        "--hub-threshold": 200,
    }
    for flag, number in expected.items():
        for parser, live in ((inbound, inbound_live), (expand, expand_live)):
            if flag not in live:
                continue
            dest = flag.lstrip("-").replace("-", "_")
            assert parser.get_default(dest) == number
            assert table[flag][1] == str(parser.get_default(dest))
    for flag in ("--yes", "--dry-run", "--since-days", "--max-pages", "--max-nodes", "--network", "--markdown", "--hub-threshold", "--expand-hub"):
        assert flag in documented
        assert flag in (inbound_live | expand_live)


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


def test_max_nodes_comparison_points_at_the_count() -> None:
    """The page states which count is above the cap, and omits the reversed phrase."""
    text = _text(RELATIONS)
    assert "A query count above `--max-nodes` exits 2 and sends nothing." in text
    assert (
        "An output row count above `--max-nodes` keeps the first `--max-nodes` rows and the status becomes `partial`."
        in text
    )
    assert "`--max-nodes` above the" not in text
    assert "The TRON client has a fixed cap of 48 requests (`request_cap`); that cap is not `--max-pages`." in text


def test_wording_scan_catches_stems_across_a_semicolon() -> None:
    """A plural stem is caught, including the clause before a semicolon."""
    stems = _english_stems()
    actor = next(stem for stem in stems if stem.startswith("per") and not stem.startswith("peo"))
    named = next(stem for stem in stems if stem.startswith("ident"))
    grouped = "Relations groups addresses by the " + actor + "s behind them."
    report = "The report shows the " + named + "ies of payers."
    for sentence in (grouped, grouped.upper(), report):
        with pytest.raises(AssertionError):
            _assert_wording(sentence)
    allowed = "This does not group addresses by the " + actor + "s behind them."
    _assert_wording(allowed)
    mixed = "the same " + actor + "; no request is sent"
    clauses = _sentences(mixed)
    assert actor in clauses[0]
    assert _NEGATION.search(clauses[0]) is None
    with pytest.raises(AssertionError):
        _assert_wording(clauses[0])
    with pytest.raises(AssertionError):
        _assert_wording(mixed)
