"""Gatus helpers: the endpoint key Gatus derives from (group, name), and the target named in an alert description."""

from src.incidents.models import Target

# Characters Gatus v5.37.0 (config/key/key.go, ``sanitize``) replaces with "-", each one individually.
_KEY_REPLACED = frozenset("/_., #+&")

# Go's ``strings.TrimSpace`` trims ``unicode.IsSpace`` runes: the Latin-1 spaces below plus the Unicode White_Space
# property (category Z). Python's ``str.strip()`` differs: it also trims U+001C..U+001F, so it is not used.
_GO_SPACE = frozenset(
    "\t\n\v\f\r \x85\xa0\u1680\u2000\u2001\u2002\u2003\u2004\u2005\u2006\u2007\u2008\u2009\u200a"
    "\u2028\u2029\u202f\u205f\u3000"
)


def _go_lower(s: str) -> str:
    """Go's ``strings.ToLower`` for the names Gatus sees: Python's ``lower()`` except U+0130, which Go maps to "i"
    (Python yields "i" plus a combining dot above)."""
    return s.replace("\u0130", "i").lower()


def _go_trim_space(s: str) -> str:
    start, end = 0, len(s)
    while start < end and s[start] in _GO_SPACE:
        start += 1
    while end > start and s[end - 1] in _GO_SPACE:
        end -= 1
    return s[start:end]


def _sanitize(s: str) -> str:
    s = _go_trim_space(_go_lower(s))
    return "".join("-" if ch in _KEY_REPLACED else ch for ch in s)


def endpoint_key(group: str, name: str) -> str:
    """The key Gatus uses for an endpoint in its API (``/api/v1/endpoints/{key}/statuses``) and UI URLs.

    A port of ``ConvertGroupAndNameToKey`` in Gatus v5.37.0 ``config/key/key.go``: lower-case, trim surrounding
    whitespace, replace each of ``/ _ . , space # + &`` with ``-``, then join group and name with ``_``. Runs of
    dashes are not collapsed, and every other character (parentheses included) is kept.
    """
    return f"{_sanitize(group)}_{_sanitize(name)}"


def parse_target(description: str) -> Target:
    """Read ``host=<h>;container=<c>`` from an alert description.

    Tolerant by design: keys are case-insensitive, whitespace around keys and values is stripped, empty values and
    unknown keys are ignored, a value may contain ``=``, and the last occurrence of a key wins. A description with
    neither key gives an empty target.
    """
    found: dict[str, str] = {}
    for part in description.split(";"):
        key, sep, value = part.partition("=")
        if not sep:
            continue
        key, value = key.strip().lower(), value.strip()
        if key in ("host", "container") and value:
            found[key] = value
    return Target(host=found.get("host"), container=found.get("container"))


__all__ = ["endpoint_key", "parse_target"]
