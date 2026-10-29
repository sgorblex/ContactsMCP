"""Strip control and invisible format characters from third-party text.

Contact data (and above all *Other contacts*, created by Google from anyone who
e-mailed the account) is untrusted text that reaches the model. Invisible
characters (``\\p{Cf}``: zero-width spaces, bidi overrides, tag characters), C0/C1
controls, fillers that render as nothing and runs of variation selectors (which can
carry hidden bytes) can hide instructions or spoof what a string looks like, so every
string a tool returns goes through :func:`clean`. Tab and newline are kept, line and
paragraph separators become newlines, and one variation selector may follow a visible
character (it picks the emoji or text form, as in "\u2764\ufe0f").
"""

from __future__ import annotations

import unicodedata
from typing import Any

_KEEP = {"\t", "\n"}
_DROPPED_CATEGORIES = {"Cc", "Cf", "Cs"}
_NEWLINE_CATEGORIES = {"Zl", "Zp"}
# Combining grapheme joiner and the Hangul fillers: no visible glyph of their own.
_INVISIBLE = {"\u034f", "\u115f", "\u1160", "\u3164", "\uffa0"}


def _is_variation_selector(ch: str) -> bool:
    return "\ufe00" <= ch <= "\ufe0f"


def _is_supplementary_selector(ch: str) -> bool:
    return "\U000e0100" <= ch <= "\U000e01ef"


def clean(text: str) -> str:
    """``text`` without invisible characters (see the module docstring)."""
    if text.isascii() and text.isprintable():
        return text
    out: list[str] = []
    for ch in text:
        if ch in _KEEP:
            out.append(ch)
            continue
        category = unicodedata.category(ch)
        if category in _NEWLINE_CATEGORIES:
            out.append("\n")
        elif (
            category in _DROPPED_CATEGORIES
            or ch in _INVISIBLE
            or _is_supplementary_selector(ch)
            or (_is_variation_selector(ch) and (not out or _is_variation_selector(out[-1])))
        ):
            continue
        else:
            out.append(ch)
    return "".join(out)


def clean_json(value: Any) -> Any:
    """:func:`clean` applied to every string (keys included) in a JSON-like value."""
    if isinstance(value, str):
        return clean(value)
    if isinstance(value, list):
        return [clean_json(v) for v in value]
    if isinstance(value, dict):
        return {clean(str(k)): clean_json(v) for k, v in value.items()}
    return value
