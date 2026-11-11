"""The app's own loggers (``src.*``) log at INFO under uvicorn, which configures only its own loggers."""

import io
import logging
from collections.abc import Generator
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from fastmcp.exceptions import ToolError
from langchain_core.tools import ToolException
from pydantic import BaseModel, ValidationError

from src.observability import logs


@pytest.fixture
def clean_src_logger() -> Generator[logging.Logger]:
    """Restore the ``src``, ``fastmcp`` and root loggers (level, handlers, propagation) after each test."""
    saved = [
        (lg, lg.level, list(lg.handlers), lg.propagate)
        for lg in (logging.getLogger(), logging.getLogger(logs.APP_LOGGER), logging.getLogger("fastmcp"))
    ]
    yield logging.getLogger(logs.APP_LOGGER)
    for lg, level, handlers, propagate in saved:
        lg.setLevel(level)
        lg.handlers = handlers
        lg.propagate = propagate


def _app_handlers(_logger: logging.Logger | None = None) -> list[logging.Handler]:
    """The marked handlers on the root logger (the only place the app installs one)."""
    return [h for h in logging.getLogger().handlers if getattr(h, logs.HANDLER_MARKER, False)]


def test_info_from_an_app_module_is_emitted(clean_src_logger: logging.Logger) -> None:
    logs.configure_app_logging("INFO")
    [handler] = _app_handlers()
    stream = io.StringIO()
    assert isinstance(handler, logging.StreamHandler)
    handler.setStream(stream)
    logging.getLogger("src.incidents.job").info("incident 7: explained push sent")
    assert stream.getvalue() == "INFO:src.incidents.job: incident 7: explained push sent\n"


def test_configuring_twice_adds_one_handler(clean_src_logger: logging.Logger) -> None:
    logs.configure_app_logging("INFO")
    logs.configure_app_logging("DEBUG")
    assert len(_app_handlers()) == 1
    assert clean_src_logger.level == logging.DEBUG
    assert clean_src_logger.handlers == []


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


def test_basicconfig_after_configure_adds_no_second_handler(
    clean_src_logger: logging.Logger, capsys: pytest.CaptureFixture[str]
) -> None:
    """``mcp`` calls ``logging.basicConfig()`` at runtime; that must not print every app line twice."""
    root = logging.getLogger()
    root.handlers = []  # a fresh process: nothing configured yet (pytest's own handlers are restored by the fixture)
    logs.configure_app_logging("INFO")
    logging.basicConfig()
    logging.getLogger("src.incidents.job").info("printed once")
    assert capsys.readouterr().err.count("printed once") == 1
    assert len(root.handlers) == 1


def _fastmcp_stream() -> io.StringIO:
    """Configure logging and capture what the app handler writes."""
    logs.configure_app_logging("INFO")
    [handler] = _app_handlers()
    stream = io.StringIO()
    assert isinstance(handler, logging.StreamHandler)
    handler.setStream(stream)
    return stream


def _log_fastmcp_error(message: str, error: BaseException) -> None:
    try:
        raise error
    except BaseException:
        logging.getLogger("fastmcp.server.server").exception(message)


def test_fastmcp_logs_in_the_app_format(clean_src_logger: logging.Logger) -> None:
    stream = _fastmcp_stream()
    assert logging.getLogger("fastmcp").propagate is True
    assert not any(type(h).__name__ == "RichHandler" for h in logging.getLogger("fastmcp").handlers)
    logging.getLogger("fastmcp.server.server").warning("Invalid arguments for tool 'x'")
    assert stream.getvalue() == "WARNING:fastmcp.server.server: Invalid arguments for tool 'x'\n"


def test_fastmcp_expected_tool_error_traceback_filtered(clean_src_logger: logging.Logger) -> None:
    """A ToolException, a pydantic ValidationError or a ToolError wrapping one is a bad call, not a crash."""
    stream = _fastmcp_stream()
    _log_fastmcp_error("Error calling tool 'a'", ToolException("Grafana API error: HTTP 404"))
    _log_fastmcp_error("Error validating tool 'b'", _pydantic_error())
    try:
        raise ToolError("Error calling tool 'c'") from ToolException("boom")
    except ToolError as wrapped:
        _log_fastmcp_error("Error calling tool 'c'", wrapped)
    assert stream.getvalue() == ""


def test_fastmcp_unexpected_error_still_logged(clean_src_logger: logging.Logger) -> None:
    stream = _fastmcp_stream()
    _log_fastmcp_error("Error calling tool 'a'", KeyError("surprise"))
    output = stream.getvalue()
    assert "Error calling tool 'a'" in output
    assert "Traceback" in output
    assert "KeyError" in output


def test_library_warning_reaches_handler(clean_src_logger: logging.Logger) -> None:
    stream = _fastmcp_stream()
    logging.getLogger("httpx").warning("slow upstream")
    logging.getLogger("httpx").info("per-request line")
    assert stream.getvalue() == "WARNING:httpx: slow upstream\n"


def _pydantic_error() -> ValidationError:
    try:
        _Probe(limit="not a number")  # type: ignore[arg-type]
    except ValidationError as e:
        return e
    raise AssertionError("expected a ValidationError")


class _Probe(BaseModel):
    limit: int


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
