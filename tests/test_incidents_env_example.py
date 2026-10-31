"""Unit tests for the incident explainer's entries in ``.env.example`` (a public file people copy)."""

from pathlib import Path

ENV_EXAMPLE = Path(__file__).resolve().parent.parent / ".env.example"


def _entry(name: str) -> tuple[str, list[str]]:
    """The value of ``name`` and the comment lines directly above it."""
    lines = ENV_EXAMPLE.read_text().splitlines()
    for index, line in enumerate(lines):
        if line.startswith(f"{name}="):
            comments: list[str] = []
            for above in reversed(lines[:index]):
                if not above.startswith("#"):
                    break
                comments.insert(0, above)
            return line.partition("=")[2], comments
    msg = f"{name} not in .env.example"
    raise AssertionError(msg)


def test_webhook_token_ships_empty_with_generation_advice() -> None:
    value, comments = _entry("INCIDENT_WEBHOOK_TOKEN")
    assert value == ""
    text = " ".join(comments)
    assert "at least 32 characters" in text
    assert "secrets.token_urlsafe(32)" in text


def test_incident_model_says_it_applies_to_anthropic_only() -> None:
    value, comments = _entry("INCIDENT_MODEL")
    assert value
    assert "LLM_PROVIDER=anthropic" in " ".join(comments)
