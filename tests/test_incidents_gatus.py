"""Unit tests for the Gatus helpers: the endpoint-key port and the description parser."""

import pytest

from src.incidents.gatus import endpoint_key, parse_target
from src.incidents.models import Target

# Expected keys follow Gatus v5.37.0 config/key/key.go by hand: lower-case, trim surrounding whitespace, then replace
# each of "/", "_", ".", ",", " ", "#", "+", "&" with "-" (one dash per character, never collapsed), and join
# group and name with "_". Everything else (parentheses, dashes, digits, other punctuation) is kept as is.
KEY_CASES: list[tuple[str, str, str]] = [
    # Known-good fixture from the live Gatus instance.
    ("monitoring", "Dozzle", "monitoring_dozzle"),
    # Upstream key_test.go scenarios.
    ("Core", "Front End", "core_front-end"),
    ("Load balancers", "us-west-2", "load-balancers_us-west-2"),
    ("a/b test", "a", "a-b-test_a"),
    ("", "name", "_name"),
    ("API (v1)", "endpoint", "api-(v1)_endpoint"),
    ("search", "query&filter", "search_query-filter"),
    # Spaces and parentheses kept, dots become dashes (TEST-NET-1 address stands in for a LAN one).
    ("dns", "AdGuard DNS (LAN 192.0.2.11)", "dns_adguard-dns-(lan-192-0-2-11)"),
    # " - " is three replaced/kept characters: space, dash, space -> three dashes.
    ("logging", "Alloy - Infra", "logging_alloy---infra"),
    # Slash.
    ("media", "Jellyfin/Web", "media_jellyfin-web"),
    # Comma followed by a space -> two dashes.
    ("apps", "Immich, Server", "apps_immich--server"),
    # Hash followed by nothing special; the space before it also becomes a dash.
    ("nodes", "Node #1", "nodes_node--1"),
    # Underscore inside a name never survives, so "_" only ever separates group and name.
    ("my_group", "my_service", "my-group_my-service"),
    # Plus and ampersand.
    ("x", "A+B & C", "x_a-b---c"),
    # Surrounding whitespace is trimmed after lower-casing; inner whitespace is kept as dashes.
    ("  Monitoring ", "  Dozzle  ", "monitoring_dozzle"),
    # Tabs are trimmed at the edges but an inner tab is not one of the replaced characters.
    ("\tg\t", "a\tb", "g_a\tb"),
    # Other punctuation (colon, apostrophe, brackets) is kept.
    ("Home", "Bob's [NAS]: UI", "home_bob's-[nas]:-ui"),
]


@pytest.mark.parametrize(("group", "name", "expected"), KEY_CASES)
def test_endpoint_key_matches_gatus(group: str, name: str, expected: str) -> None:
    assert endpoint_key(group, name) == expected


def test_endpoint_key_trims_go_whitespace_only() -> None:
    # Go's strings.TrimSpace trims Unicode White_Space (NBSP, NEL, U+3000 ...) but not the ASCII separators
    # U+001C-U+001F, which Python's str.strip() would also remove.
    assert endpoint_key("\xa0g\u3000", "\x85n\u2028") == "g_n"
    assert endpoint_key("g", "\x1fn") == "g_\x1fn"


def test_endpoint_key_lowercases_dotted_capital_i_like_go() -> None:
    # Go's unicode.ToLower maps U+0130 to a plain "i"; Python's str.lower() would add a combining dot.
    assert endpoint_key("g", "\u0130stanbul") == "g_istanbul"


@pytest.mark.parametrize(
    ("description", "expected"),
    [
        ("host=infra;container=dozzle", Target(host="infra", container="dozzle")),
        ("container=dozzle;host=infra", Target(host="infra", container="dozzle")),
        ("host=infra", Target(host="infra", container=None)),
        ("container=dozzle", Target(host=None, container="dozzle")),
        ("", Target(host=None, container=None)),
        ("   ", Target(host=None, container=None)),
        ("host=;container=", Target(host=None, container=None)),
        (" host = infra ; container = dozzle ; ", Target(host="infra", container="dozzle")),
        ("owner=john;host=infra;flavour", Target(host="infra", container=None)),
        ("HOST=infra", Target(host="infra", container=None)),
        ("host=a=b", Target(host="a=b", container=None)),
        ("just a free-text description", Target(host=None, container=None)),
        ("host=first;host=second", Target(host="second", container=None)),
    ],
)
def test_parse_target(description: str, expected: Target) -> None:
    assert parse_target(description) == expected
