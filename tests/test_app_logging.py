"""The app's own loggers (``src.*``) log at INFO under uvicorn, which configures only its own loggers."""

import io
import logging
from collections.abc import Generator
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from src.observability import logs


@pytest.fixture
def clean_src_logger() -> Generator[logging.Logger]:
    """Restore the ``src`` logger's level and handlers after each test."""
    logger = logging.getLogger(logs.APP_LOGGER)
    level, handlers = logger.level, list(logger.handlers)
    yield logger
    logger.setLevel(level)
    logger.handlers = handlers


def _app_handlers(logger: logging.Logger) -> list[logging.Handler]:
    return [h for h in logger.handlers if getattr(h, logs.HANDLER_MARKER, False)]


def test_info_from_an_app_module_is_emitted(clean_src_logger: logging.Logger) -> None:
    logs.configure_app_logging("INFO")
    [handler] = _app_handlers(clean_src_logger)
    stream = io.StringIO()
    assert isinstance(handler, logging.StreamHandler)
    handler.setStream(stream)
    logging.getLogger("src.incidents.job").info("incident 7: explained push sent")
    assert stream.getvalue() == "INFO:src.incidents.job: incident 7: explained push sent\n"


def test_configuring_twice_adds_one_handler(clean_src_logger: logging.Logger) -> None:
    logs.configure_app_logging("INFO")
    logs.configure_app_logging("DEBUG")
    assert len(_app_handlers(clean_src_logger)) == 1
    assert clean_src_logger.level == logging.DEBUG


def test_library_loggers_are_left_alone(clean_src_logger: logging.Logger) -> None:
    before = logging.getLogger("httpx").getEffectiveLevel()
    logs.configure_app_logging("INFO")
    assert logging.getLogger("httpx").getEffectiveLevel() == before


def test_app_records_still_reach_the_root_logger(
    clean_src_logger: logging.Logger, caplog: pytest.LogCaptureFixture
) -> None:
    """Propagation stays on, so pytest's caplog (a root handler) still sees app records."""
    logs.configure_app_logging("INFO")
    with caplog.at_level(logging.INFO, logger="src.api.incidents"):
        logging.getLogger("src.api.incidents").info("incident 3 (monitoring_dozzle): RESOLVED")
    assert "incident 3 (monitoring_dozzle): RESOLVED" in caplog.text


def test_unknown_level_is_rejected(clean_src_logger: logging.Logger) -> None:
    with pytest.raises(ValueError, match="LOG_LEVEL"):
        logs.configure_app_logging("LOUD")


@pytest.mark.integration
def test_lifespan_configures_app_logging_with_log_level(mock_settings: Any) -> None:
    mock_settings.log_level = "DEBUG"
    with (
        patch("src.api.main.build_agent", return_value=MagicMock(name="fake_agent")),
        patch("src.api.main.configure_app_logging") as configure,
    ):
        from src.api.main import app

        with TestClient(app):
            pass
    configure.assert_called_once_with("DEBUG")
