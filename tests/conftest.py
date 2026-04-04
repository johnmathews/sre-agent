"""Shared pytest configuration and fixtures."""

from collections.abc import Generator
from typing import Any
from unittest.mock import patch

import pytest

from src.config import Settings, get_settings


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--run-e2e",
        action="store_true",
        default=False,
        help="Run e2e tests that hit real services (requires .env with valid credentials)",
    )


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    if config.getoption("--run-e2e"):
        return
    skip_e2e = pytest.mark.skip(reason="Need --run-e2e flag to run")
    for item in items:
        if "e2e" in item.keywords:
            item.add_marker(skip_e2e)


@pytest.fixture(autouse=True)
def _no_dotenv(request: pytest.FixtureRequest) -> Generator[None]:
    """Block .env loading so tests that forget mock_settings fail locally, not just in CI.

    Sets Settings.model_config['env_file'] = None before each test (except e2e).
    Tests that use mock_settings bypass Settings() entirely, so this is transparent.
    Tests that forget mock_settings will hit a validation error on required fields.
    """
    if "e2e" in request.keywords:
        yield
        return

    get_settings.cache_clear()
    original = Settings.model_config.get("env_file")
    Settings.model_config["env_file"] = None

    try:
        yield
    finally:
        Settings.model_config["env_file"] = original
        get_settings.cache_clear()


@pytest.fixture
def mock_settings() -> Generator[Any]:
    """Provide fake settings so tests don't need a .env file.

    Patches get_settings at every import site so cached references are overridden.
    """
    fake_settings = type(
        "FakeSettings",
        (),
        {
            "llm_provider": "openai",
            "openai_api_key": "sk-proj-test-fake",
            "openai_model": "gpt-4o-mini",
            "openai_base_url": "",
            "anthropic_api_key": "",
            "anthropic_model": "claude-sonnet-4-20250514",
            "extra_docs_dirs": "",
            "prometheus_url": "http://prometheus.test:9090",
            "grafana_url": "http://grafana.test:3000",
            "grafana_service_account_token": "glsa_test_fake",
            "proxmox_url": "https://proxmox.test:8006",
            "proxmox_api_token": "test@pam!test=fake-token",
            "proxmox_verify_ssl": False,
            "proxmox_ca_cert": "",
            "proxmox_node": "proxmox",
            "pbs_url": "https://pbs.test:8007",
            "pbs_api_token": "test@pbs!test=fake-token",
            "pbs_verify_ssl": False,
            "pbs_ca_cert": "",
            "pbs_node": "localhost",
            "pbs_default_datastore": "backups",
            "loki_url": "http://loki.test:3100",
            "truenas_url": "https://truenas.test",
            "truenas_api_key": "1-fake-truenas-api-key",
            "truenas_verify_ssl": False,
            "truenas_ca_cert": "",
            # SMTP / Email
            "smtp_host": "smtp.test.com",
            "smtp_port": 587,
            "smtp_username": "test@test.com",
            "smtp_password": "test-password",
            "report_recipient_email": "recipient@test.com",
            # Report schedule
            "report_schedule_cron": "",
            "report_lookback_days": 7,
            # Conversation history
            "conversation_history_dir": "",
            # Agent memory store
            "memory_db_path": "",
            # Request timeout
            "request_timeout_seconds": 120,
            # Documentation MCP server
            "documentation_mcp_url": "",
            # MCP server auth token
            "mcp_auth_token": "",
        },
    )()
    with (
        patch("src.config.get_settings", return_value=fake_settings),
        patch("src.agent.tools.prometheus.get_settings", return_value=fake_settings),
        patch("src.agent.tools.grafana_alerts.get_settings", return_value=fake_settings),
        patch("src.agent.tools.grafana_dashboards.get_settings", return_value=fake_settings),
        patch("src.agent.tools.proxmox.get_settings", return_value=fake_settings),
        patch("src.agent.tools.pbs.get_settings", return_value=fake_settings),
        patch("src.agent.tools.loki.get_settings", return_value=fake_settings),
        patch("src.agent.tools.truenas.get_settings", return_value=fake_settings),
        patch("src.agent.agent.get_settings", return_value=fake_settings),
        patch("src.agent.tools.disk_status.get_settings", return_value=fake_settings),
        patch("src.agent.retrieval.embeddings.get_settings", return_value=fake_settings),
        patch("src.api.main.get_settings", return_value=fake_settings),
        patch("src.report.generator.get_settings", return_value=fake_settings),
        patch("src.report.email.get_settings", return_value=fake_settings),
        patch("src.report.scheduler.get_settings", return_value=fake_settings),
        patch("src.memory.store.get_settings", return_value=fake_settings),
        patch("src.memory.baselines.get_settings", return_value=fake_settings),
        patch("src.agent.mcp_tools.get_settings", return_value=fake_settings),
        patch("src.agent.sdk_agent.get_settings", return_value=fake_settings),
        patch("src.api.mcp_server.get_settings", return_value=fake_settings),
    ):
        yield fake_settings
