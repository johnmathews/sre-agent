"""Integration tests for the Claude credentials gauge and background refresh wiring.

The FastAPI lifespan registers ``sre_assistant_claude_credentials_healthy`` and
starts ``keep_fresh`` only when ``LLM_PROVIDER=anthropic``. The real loop is
patched out: left running, it would read the developer's ``~/.claude`` and
could spend a real refresh token.
"""

from collections.abc import Generator
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from prometheus_client import REGISTRY

from src.observability import metrics

_GAUGE = "sre_assistant_claude_credentials_healthy"


@pytest.fixture(autouse=True)
def _fresh_gauge() -> Generator[None]:
    """Unregister the gauge around each test: Prometheus registration is process-global."""

    def reset() -> None:
        if metrics._claude_credentials_gauge is not None:
            REGISTRY.unregister(metrics._claude_credentials_gauge)
            metrics._claude_credentials_gauge = None

    reset()
    yield
    reset()


def _run_lifespan(started: list[float]) -> Generator[TestClient]:
    async def fake_keep_fresh(interval_s: float) -> None:
        started.append(interval_s)

    with (
        patch("src.api.main.build_agent", return_value=MagicMock(name="fake_agent")),
        patch("src.agent.oauth_refresh.keep_fresh", fake_keep_fresh),
        patch("src.agent.oauth_refresh.credentials_healthy", return_value=0.0),
    ):
        from src.api.main import app

        with TestClient(app) as tc:
            yield tc


@pytest.mark.integration
def test_anthropic_exports_gauge_and_starts_refresh(mock_settings: Any) -> None:
    mock_settings.llm_provider = "anthropic"
    started: list[float] = []

    for tc in _run_lifespan(started):
        body = tc.get("/metrics").text
        assert f"{_GAUGE} 0.0" in body

    assert started == [900.0]


@pytest.mark.integration
def test_interval_zero_exports_gauge_without_refresh(mock_settings: Any) -> None:
    mock_settings.llm_provider = "anthropic"
    mock_settings.claude_credentials_check_interval_s = 0
    started: list[float] = []

    for tc in _run_lifespan(started):
        assert _GAUGE in tc.get("/metrics").text

    assert started == []


@pytest.mark.integration
def test_openai_exports_no_gauge_and_starts_nothing(mock_settings: Any) -> None:
    assert mock_settings.llm_provider == "openai"
    started: list[float] = []

    for tc in _run_lifespan(started):
        assert _GAUGE not in tc.get("/metrics").text

    assert started == []


def test_reobserving_repoints_the_gauge() -> None:
    metrics.observe_claude_credentials(lambda: 1.0)
    assert REGISTRY.get_sample_value(_GAUGE) == 1.0

    metrics.observe_claude_credentials(lambda: 0.0)
    assert REGISTRY.get_sample_value(_GAUGE) == 0.0
