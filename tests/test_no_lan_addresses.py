"""Guard: tracked files hold no LAN address and no private root domain (public repo).

``runbooks/`` and ``uv.lock`` are skipped (runbooks wait for a separate cleanup issue). The private root domain is
never written here: the test compares the SHA-256 of every adjacent label pair of each domain-like token (lower-cased,
regex-escaped dots such as ``\\.`` and ``[.]`` undone) against ``DOMAIN_DIGEST``. A failure names file:line only.
"""

import hashlib
import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
LAN_IP = re.compile(r"192\.168\.\d+\.\d+")
DOMAIN_TOKEN = re.compile(r"[a-z0-9-]+(?:\.[a-z0-9-]+)*\.[a-z]{2,}")
DOMAIN_DIGEST = "3b5164e9562708835c165145fa32285a4ab9e6d33661fee414cb7dc383f4b67b"
SKIP_PREFIXES = ("runbooks/", "uv.lock")


ESCAPED_DOT = re.compile(r"\\+\.|\[\.\]")


def _label_pairs(token: str) -> list[str]:
    """Every adjacent ``label.label`` pair, so the domain is found mid-token (``sre.<domain>.pem``)."""
    labels = token.split(".")
    return [f"{a}.{b}" for a, b in zip(labels, labels[1:], strict=False)]


def scan_text(text: str) -> list[tuple[int, str]]:
    """Return (line number, kind) for each LAN address or private-domain hit in ``text``."""
    hits: list[tuple[int, str]] = []
    for number, line in enumerate(text.splitlines(), start=1):
        if LAN_IP.search(line):
            hits.append((number, "LAN address"))
        lowered = ESCAPED_DOT.sub(".", line.lower())
        pairs = (pair for match in DOMAIN_TOKEN.finditer(lowered) for pair in _label_pairs(match.group(0)))
        if any(hashlib.sha256(pair.encode()).hexdigest() == DOMAIN_DIGEST for pair in pairs):
            hits.append((number, "private domain"))
    return hits


def _tracked_files() -> list[str]:
    if shutil.which("git") is None:
        pytest.skip("git is not available")
    result = subprocess.run(["git", "ls-files", "-z"], cwd=REPO, capture_output=True, check=False)
    if result.returncode != 0:
        pytest.skip("not a git checkout")
    names = result.stdout.decode().split("\0")
    return [n for n in names if n and not n.startswith(SKIP_PREFIXES)]


def test_scan_text_flags_planted_values() -> None:
    lan = ".".join(["192", "168", "7", "9"])  # built at runtime so this file passes its own scan
    planted = f"ok line\nhost at {lan} here\nsee https://foo.bar.example.org/x\n"
    assert scan_text(planted) == [(2, "LAN address")]
    assert scan_text("use 192.0.2.5 and example.com") == []


def test_scan_text_flags_planted_domain(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("tests.test_no_lan_addresses.DOMAIN_DIGEST", hashlib.sha256(b"example.org").hexdigest())
    assert scan_text("fine\nvisit https://Sub.Example.org/path") == [(2, "private domain")]


@pytest.mark.parametrize(
    "line",
    [
        "cert sre.example.org.pem",
        "example.org.json",
        r"rule = r'.*\.example\.org'",
        "HostRegexp(`{sub:[a-z]+}.example\\.org`)",
        "example[.]org",
    ],
)
def test_scan_text_flags_domain_followed_by_labels_or_escaped(monkeypatch: pytest.MonkeyPatch, line: str) -> None:
    monkeypatch.setattr("tests.test_no_lan_addresses.DOMAIN_DIGEST", hashlib.sha256(b"example.org").hexdigest())
    assert scan_text(line) == [(1, "private domain")]


def test_tracked_files_hold_no_lan_address_or_private_domain() -> None:
    problems: list[str] = []
    for name in _tracked_files():
        path = REPO / name
        if not path.is_file():
            continue
        data = path.read_bytes()
        if b"\0" in data:
            continue
        for number, kind in scan_text(data.decode("utf-8", errors="ignore")):
            problems.append(f"{name}:{number}: {kind}")
    assert not problems, "Remove these from tracked files (use 192.0.2.x / example.com):\n" + "\n".join(problems)
