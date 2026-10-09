"""Format-specific escaping for untrusted display text.

Retrieved titles, names, and error strings stay data. Each sink escapes for
its own format. A single stripped string is not reused for plain text, HTML,
Markdown, CSV, and JSON. Canonical addresses and plain numbers are left intact.
Stripping a phrase such as "ignore previous instructions" is not a control.
"""

from __future__ import annotations

import re
from collections.abc import Mapping

_BIDI = {
    "\u202a": "[U+202A]",
    "\u202b": "[U+202B]",
    "\u202c": "[U+202C]",
    "\u202d": "[U+202D]",
    "\u202e": "[U+202E]",
    "\u2066": "[U+2066]",
    "\u2067": "[U+2067]",
    "\u2068": "[U+2068]",
    "\u2069": "[U+2069]",
}
_ADDRESS = re.compile(r"^0x[0-9a-fA-F]{40}$")
_NUMBER = re.compile(r"^-?\d+(\.\d+)?$")
_CSV_COLUMNS = ("kind", "network", "identity", "gross", "net", "detail", "what")
_PLAIN_FIELDS = ("kind", "network", "identity", "gross", "net", "detail", "what", "why", "step")


def escape_plain(value: str) -> str:
    """One log line cannot be forged, and bidi overrides cannot reorder it."""
    if not isinstance(value, str):
        value = str(value)
    if _ADDRESS.fullmatch(value) or _NUMBER.fullmatch(value):
        return value
    parts: list[str] = []
    for char in value:
        if char in _BIDI:
            parts.append(_BIDI[char])
            continue
        if char == "\n":
            parts.append("\\n")
            continue
        if char == "\r":
            parts.append("\\r")
            continue
        if char == "\t":
            parts.append("\\t")
            continue
        code = ord(char)
        if code < 32 or 127 <= code <= 159:
            parts.append(f"[U+{code:04X}]")
            continue
        parts.append(char)
    return "".join(parts)


def escape_html(value: str) -> str:
    """Text for an HTML sink. Tags and quotes cannot become markup."""
    text = escape_plain(value)
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&#x27;")
    )


def escape_markdown(value: str) -> str:
    """Text for a Markdown sink. Link and image syntax stays literal."""
    text = escape_html(value)
    return "".join("\\" + char if char in "\\[]()`!*_~|" else char for char in text)


def escape_csv_cell(value: str) -> str:
    """One CSV cell. Formula leaders are text. Numbers and addresses stay data."""
    text = escape_plain(value)
    if _ADDRESS.fullmatch(text) or _NUMBER.fullmatch(text):
        return text
    if text and text[0] in "=+-@":
        text = "'" + text
    if any(char in text for char in '",\r\n'):
        text = '"' + text.replace('"', '""') + '"'
    return text


def plain_alert(alert: Mapping[str, object]) -> str:
    """Plain rendering of bounded display fields. Control keys are not rewritten."""
    lines: list[str] = []
    for key in _PLAIN_FIELDS:
        if key not in alert:
            continue
        value = alert[key]
        if value is None or isinstance(value, bool):
            continue
        if not isinstance(value, (str, int, float)):
            continue
        lines.append(f"{key}={escape_plain(str(value))}")
    return "\n".join(lines) if lines else "wallet-guardian alert"


def csv_record(alert: Mapping[str, object]) -> str:
    """One header and one row. Each cell is escaped for a spreadsheet."""
    cells: list[str] = []
    for key in _CSV_COLUMNS:
        value = alert.get(key, "")
        if value is None or isinstance(value, bool):
            value = ""
        elif not isinstance(value, (str, int, float)):
            value = ""
        cells.append(escape_csv_cell(str(value)))
    return ",".join(_CSV_COLUMNS) + "\n" + ",".join(cells)


def html_alert(alert: Mapping[str, object]) -> str:
    return "<p>" + "</p><p>".join(escape_html(line) for line in plain_alert(alert).split("\n")) + "</p>"


def markdown_alert(alert: Mapping[str, object]) -> str:
    return "\n".join(escape_markdown(line) for line in plain_alert(alert).split("\n"))


def render_alert(alert: Mapping[str, object]) -> dict[str, str]:
    """Render one stored alert for each sink. The stored mapping is not modified."""
    if "severity_label" in alert:
        from guardian.domain import format_alert

        plain = format_alert(dict(alert))
    else:
        plain = plain_alert(alert)
    return {
        "csv": csv_record(alert),
        "html": html_alert(alert),
        "markdown": markdown_alert(alert),
        "plain": plain,
    }
