"""Untrusted text from feeds and token metadata. Stored as data, never executed."""

from __future__ import annotations

_LIMIT = 180
_NOTES: list[str] = []


def sanitize_untrusted(value: str) -> str:
    """Strip controls, collapse whitespace, escape markup, and truncate.

    Ordinary titles that contain none of those characters are unchanged.
    """
    if not isinstance(value, str):
        return ""
    cleaned = "".join(ch for ch in value if ord(ch) >= 32 and ord(ch) != 127)
    cleaned = " ".join(cleaned.split())
    cleaned = cleaned.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    if len(cleaned) > _LIMIT:
        cleaned = cleaned[:_LIMIT]
    return cleaned


def begin_untrusted_notes() -> None:
    """Drop notes from an earlier round before a new run records token text."""
    _NOTES.clear()


def note_untrusted_token(value: str) -> None:
    """Keep a sanitized token string for the state row. The raw chain string is not copied."""
    cleaned = sanitize_untrusted(value)
    if cleaned and cleaned not in _NOTES:
        _NOTES.append(cleaned)


def untrusted_notes() -> list[str]:
    return list(_NOTES)
