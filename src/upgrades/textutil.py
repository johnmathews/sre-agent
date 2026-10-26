"""Helpers shared by the Upgrade Advisor's HTTP clients: bounded, cleaned remote text and the response cap."""

import unicodedata

MAX_DETAIL_CHARS = 200
MAX_RESPONSE_BYTES = 5 * 1024 * 1024
# Unicode categories replaced by a space: all control/format/etc. (C*) plus line and paragraph separators.
_STRIPPED = frozenset({"Cc", "Cf", "Cs", "Co", "Cn", "Zl", "Zp"})


def clean_text(text: str) -> str:
    """Make text from a remote response safe for an error message: no control or line/paragraph separator
    characters, at most ``MAX_DETAIL_CHARS`` chars."""
    return "".join(" " if unicodedata.category(ch) in _STRIPPED else ch for ch in text[:MAX_DETAIL_CHARS])
