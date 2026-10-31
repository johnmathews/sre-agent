"""Log level for the app's own loggers.

uvicorn configures only its own loggers (``uvicorn``, ``uvicorn.access``, ``uvicorn.error``). Without this, every
``src.*`` logger falls through to Python's last-resort handler, which prints WARNING and above only, so INFO lines
such as an incident explanation that was sent never appear in ``docker logs sre-agent``.

``configure_app_logging`` gives the ``src`` logger one stderr handler at ``LOG_LEVEL``. Library loggers (``httpx``,
``anthropic``, ``langchain``) are left at Python's default, so their per-request INFO lines stay hidden. Propagation
stays on, so a root handler (pytest's caplog, for one) still receives app records.
"""

import logging

APP_LOGGER = "src"
LOG_FORMAT = "%(levelname)s:%(name)s: %(message)s"
# Attribute set on the handler this module adds, so a second call finds it instead of adding another.
HANDLER_MARKER = "_sre_agent_app_handler"


def configure_app_logging(level: str) -> None:
    """Log ``src.*`` records at ``level`` (a standard level name, e.g. ``INFO``) to stderr. Safe to call twice."""
    numeric = logging.getLevelNamesMapping().get(level.upper())
    if numeric is None:
        raise ValueError(f"LOG_LEVEL={level!r} is not a logging level (DEBUG, INFO, WARNING, ERROR, CRITICAL)")
    logger = logging.getLogger(APP_LOGGER)
    logger.setLevel(numeric)
    if not any(getattr(h, HANDLER_MARKER, False) for h in logger.handlers):
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter(LOG_FORMAT))
        setattr(handler, HANDLER_MARKER, True)
        logger.addHandler(handler)


__all__ = ["APP_LOGGER", "HANDLER_MARKER", "LOG_FORMAT", "configure_app_logging"]
