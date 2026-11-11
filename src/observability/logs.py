"""Where the app's log lines go, and at what level.

uvicorn configures only its own loggers (``uvicorn``, ``uvicorn.access``, ``uvicorn.error``). Without this, every
``src.*`` logger falls through to Python's last-resort handler, which prints WARNING and above only, so INFO lines
such as an incident explanation that was sent never appear in ``docker logs sre-agent``.

``configure_app_logging`` installs one marked stderr handler on the **root** logger, at WARNING for libraries, and sets
the ``src`` logger to ``LOG_LEVEL`` with no handler of its own. Because root already has a handler, a later
``logging.basicConfig()`` (``mcp`` calls it at runtime) does nothing, so no line is printed twice. Library loggers
(``httpx``, ``anthropic``, ``langchain``) inherit the root WARNING level, so their per-request INFO lines stay hidden
but their warnings and errors appear. ``fastmcp`` loses its Rich handler and propagates to the root handler, so its
lines use the app format. A filter on that handler drops FastMCP's "Error calling tool" / "Error validating tool"
traceback for expected tool errors (a ``ToolException``, a pydantic ``ValidationError`` or a ``ToolError`` wrapping
one); the per-call log line covers those. Unexpected exceptions keep their traceback.
"""

import logging

from langchain_core.tools import ToolException
from pydantic import ValidationError

APP_LOGGER = "src"
LOG_FORMAT = "%(levelname)s:%(name)s: %(message)s"
# Attribute set on the handler this module adds, so a second call finds it instead of adding another.
HANDLER_MARKER = "_sre_agent_app_handler"
FASTMCP_LOGGER = "fastmcp"
_EXPECTED_ERROR_PREFIXES = ("Error calling tool", "Error validating tool")
_EXPECTED_ERRORS = (ToolException, ValidationError)


class ExpectedToolErrorFilter(logging.Filter):
    """Drop FastMCP's error record for a tool call that failed in an expected way (a bad call, not a crash)."""

    def filter(self, record: logging.LogRecord) -> bool:
        if not record.name.startswith(FASTMCP_LOGGER) or not record.getMessage().startswith(_EXPECTED_ERROR_PREFIXES):
            return True
        error = record.exc_info[1] if record.exc_info else None
        if error is None:
            return True
        # FastMCP's own ToolError wraps the original exception as its cause.
        cause = error.__cause__
        return not (isinstance(error, _EXPECTED_ERRORS) or isinstance(cause, _EXPECTED_ERRORS))


def configure_app_logging(level: str) -> None:
    """Log ``src.*`` records at ``level`` (a standard level name, e.g. ``INFO``) to stderr. Safe to call twice."""
    numeric = logging.getLevelNamesMapping().get(level.upper())
    if numeric is None:
        raise ValueError(f"LOG_LEVEL={level!r} is not a logging level (DEBUG, INFO, WARNING, ERROR, CRITICAL)")
    # fastmcp configures its own logger when first imported, so import it first or a later import would undo this.
    import fastmcp  # noqa: F401

    root = logging.getLogger()
    root.setLevel(logging.WARNING)
    if not any(getattr(h, HANDLER_MARKER, False) for h in root.handlers):
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter(LOG_FORMAT))
        handler.addFilter(ExpectedToolErrorFilter())
        setattr(handler, HANDLER_MARKER, True)
        root.addHandler(handler)
    app_logger = logging.getLogger(APP_LOGGER)
    app_logger.setLevel(numeric)
    for existing in [h for h in app_logger.handlers if getattr(h, HANDLER_MARKER, False)]:
        app_logger.removeHandler(existing)
    fastmcp_logger = logging.getLogger(FASTMCP_LOGGER)
    fastmcp_logger.handlers = []
    fastmcp_logger.propagate = True
    fastmcp_logger.setLevel(logging.WARNING)


__all__ = [
    "APP_LOGGER",
    "FASTMCP_LOGGER",
    "ExpectedToolErrorFilter",
    "HANDLER_MARKER",
    "LOG_FORMAT",
    "configure_app_logging",
]
