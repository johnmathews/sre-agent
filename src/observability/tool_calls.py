"""One log line and the tool metrics for every tool call, on every surface.

``observe_tool_call`` is the only writer of the tool metrics. ``/mcp`` calls it from its FastMCP middleware
(``src/api/mcp_server.py``), the SDK agent from its tool handler (``src/agent/mcp_tools.py``) and the LangGraph agent
from its callback (``src/observability/callbacks.py``). Each call writes one line on this module's logger::

    tool_call tool=… source=mcp|agent client=… outcome=… duration_ms=… result_chars=… args="k='v' …"
        [identity_ref=…] [error_class=… error="…"]

The arguments are summarised (sorted keys, scalars only, each value cut to 80 characters, the whole summary to 400),
control characters are replaced, and the result itself is never logged: only its length.
"""

import logging
import re
from collections.abc import Mapping
from typing import Any, Literal

import httpx
from fastmcp.exceptions import NotFoundError, ToolError
from fastmcp.exceptions import ValidationError as FastMCPValidationError
from langchain_core.tools import ToolException
from pydantic import ValidationError

from src.agent.tool_registry import InvalidToolArguments
from src.observability.metrics import TOOL_CALL_DURATION, TOOL_CALLS_TOTAL, TOOL_RESULT_CHARS
from src.upgrades.textutil import clean_text

logger = logging.getLogger(__name__)

type Source = Literal["mcp", "agent"]
type Outcome = Literal["success", "error", "invalid_args", "unknown_tool", "timeout", "internal"]

MAX_VALUE_CHARS = 80
MAX_ARGS_CHARS = 400
MAX_ERROR_CHARS = 200
# A tool name the client made up is logged, but never becomes a label value.
UNKNOWN_TOOL_LABEL = "unknown"
# How far down the __cause__ chain to look: FastMCP's ToolError <- the tool's ToolException <- httpx's error.
_MAX_CAUSE_DEPTH = 5
_NOT_BARE = re.compile(r'[\s"=\\]')


def _clean(text: str, limit: int) -> str:
    """``text`` with control and separator characters replaced by spaces, cut to ``limit`` characters."""
    return clean_text(text[:limit])


def _bare(text: str) -> str:
    """A value written unquoted: cleaned, cut to 80 characters, with spaces, quotes, ``=`` and ``\\`` as ``_``."""
    return _NOT_BARE.sub("_", _clean(text, MAX_VALUE_CHARS)) or "-"


def _quoted(text: str) -> str:
    """A double-quoted value with ``\\`` and ``"`` escaped, as Loki's ``| logfmt`` reads it."""
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _arg_value(value: Any) -> str:
    if isinstance(value, str):
        return f"'{_clean(value, MAX_VALUE_CHARS)}'"
    if value is None or isinstance(value, bool | int | float):
        return _clean(repr(value), MAX_VALUE_CHARS)
    return type(value).__name__


def summarize_args(arguments: Mapping[str, Any]) -> str:
    """``k='v' n=5 items=list``: sorted keys, scalars shown, anything else as its type name; at most 400 characters."""
    summary = " ".join(f"{_bare(str(key))}={_arg_value(arguments[key])}" for key in sorted(arguments, key=str))
    if len(summary) > MAX_ARGS_CHARS:
        summary = summary[: MAX_ARGS_CHARS - 1] + "…"
    return summary


def _cause_chain(error: BaseException) -> list[BaseException]:
    chain = [error]
    while len(chain) < _MAX_CAUSE_DEPTH and (cause := chain[-1].__cause__) is not None:
        chain.append(cause)
    return chain


def classify_tool_error(error: BaseException) -> tuple[Outcome, BaseException]:
    """The outcome of a failed call and the exception that decided it.

    The error and its ``__cause__`` chain are searched, so FastMCP's ``ToolError`` wrapping the tool's
    ``ToolException`` wrapping an ``httpx`` timeout is a ``timeout``. In order: an unknown tool, invalid arguments, a
    timeout, the tool's own ``ToolException`` (or a ``ToolError`` with no cause) as ``error``; anything else is
    ``internal``, decided by the innermost cause.
    """
    chain = _cause_chain(error)
    checks: tuple[tuple[Outcome, tuple[type[BaseException], ...]], ...] = (
        ("unknown_tool", (NotFoundError,)),
        ("invalid_args", (InvalidToolArguments, ValidationError, FastMCPValidationError)),
        ("timeout", (httpx.TimeoutException,)),
        ("error", (ToolException,)),
    )
    for outcome, types in checks:
        for candidate in chain:
            if isinstance(candidate, types):
                return outcome, candidate
    if len(chain) == 1 and isinstance(error, ToolError):
        return "error", error
    return "internal", chain[-1]


def observe_tool_call(
    tool: str,
    source: Source,
    client: str,
    outcome: Outcome,
    error_class: str | None,
    error: BaseException | str | None,
    duration_s: float,
    result_chars: int,
    arguments: Mapping[str, Any],
    identity_ref: str | None = None,
) -> None:
    """Write the call's one ``tool_call`` line and record its metrics.

    ``client`` must be a fixed label value (``agent``, ``in-process``, ``unknown`` or a mapped client name), never a
    raw identity. ``error`` may be the exception itself: its class fills ``error_class`` when that is ``None``, and an
    ``internal`` outcome logs its traceback. ``identity_ref`` is an opaque reference to the caller's verified identity,
    logged when given and never a label. Never raises: observation must not break a call.
    """
    try:
        if error_class is None and isinstance(error, BaseException):
            error_class = type(error).__name__
        parts = [
            "tool_call",
            f"tool={_bare(tool)}",
            f"source={source}",
            f"client={_bare(client)}",
            f"outcome={outcome}",
            f"duration_ms={round(duration_s * 1000)}",
            f"result_chars={result_chars}",
            f"args={_quoted(summarize_args(arguments))}",
        ]
        if identity_ref:
            parts.append(f"identity_ref={_bare(identity_ref)}")
        if error_class is not None or error is not None:
            parts.append(f"error_class={_bare(error_class or 'Error')}")
            parts.append(f"error={_quoted(_clean(str(error or ''), MAX_ERROR_CHARS))}")
        line = " ".join(parts)
        if outcome == "internal":
            exc_info = error if isinstance(error, BaseException) else True
            logger.error(line, exc_info=exc_info)
        else:
            logger.info(line)

        tool_label = UNKNOWN_TOOL_LABEL if outcome == "unknown_tool" else tool
        TOOL_CALLS_TOTAL.labels(tool_name=tool_label, status=outcome, source=source, client=client).inc()
        TOOL_CALL_DURATION.labels(tool_name=tool_label, source=source).observe(duration_s)
        if outcome == "success":
            TOOL_RESULT_CHARS.labels(tool_name=tool_label, source=source).observe(result_chars)
    except Exception:
        logger.warning("tool_call observation failed for %s", tool, exc_info=True)


__all__ = [
    "MAX_ARGS_CHARS",
    "MAX_ERROR_CHARS",
    "MAX_VALUE_CHARS",
    "Outcome",
    "Source",
    "classify_tool_error",
    "observe_tool_call",
    "summarize_args",
]
