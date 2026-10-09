"""Review fixes for SAFE-02 and SAFE-05: sink escaping and per-item quarantine."""

from __future__ import annotations

import csv
import io

from guardian.news.feeds import parse_feed, parse_structured
from guardian.security.display_text import csv_record, escape_csv_cell, escape_markdown, escape_plain

_RSS = (
    '<?xml version="1.0"?><rss version="2.0"><channel>{items}</channel></rss>'
)


def _item(title: str, link: str) -> str:
    return f"<item><title>{title}</title><link>{link}</link></item>"


def test_mcm_safe_02_markdown_emphasis_and_c1_controls_are_escaped() -> None:
    rendered = escape_markdown("**SAFE** _ok_ ~~x~~ a\\b")
    assert "**" not in rendered.replace("\\*", "")
    assert "\\*\\*SAFE\\*\\*" in rendered
    assert "a\\\\b" in rendered
    assert escape_plain("x\u009by") == "x[U+009B]y"


def test_mcm_safe_02_empty_csv_cell_stays_empty_and_round_trips() -> None:
    assert escape_csv_cell("") == ""
    rows = list(csv.reader(io.StringIO(csv_record({"kind": "exposure", "detail": '=1+1,"q"', "what": ""}))))
    header, row = rows
    assert row[header.index("what")] == ""
    assert row[header.index("detail")] == "'=1+1,\"q\""
    assert row[header.index("kind")] == "exposure"


def test_mcm_safe_05_bad_port_sibling_is_quarantined_not_raised() -> None:
    data = _RSS.format(items=_item("Kinza ok", "https://example.invalid/a") + _item("Kinza bad", "https://example.invalid:bad/x")).encode()
    parsed = parse_feed(data, source_id="s")
    assert parsed.ok and len(parsed.items) == 1 and parsed.quarantined == 1


def test_mcm_safe_05_html_root_is_rejected_not_empty_success() -> None:
    parsed = parse_feed(b"<html><body><p>hi</p></body></html>", source_id="s")
    assert not parsed.ok


def test_mcm_safe_05_attempted_records_are_bounded() -> None:
    items = _item("Kinza ok", "https://example.invalid/a") + "".join(_item("", "x") for _ in range(101))
    assert parse_feed(_RSS.format(items=items).encode(), source_id="s").reason == "item_cap"
    rows = [{"title": f"Kinza {n}", "url": f"https://example.invalid/{n}"} for n in range(101)]
    assert parse_structured(rows, source_id="s").reason == "item_cap"
