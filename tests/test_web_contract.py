"""The HTTP contract between the server and the web UI in web/ (ADR-0006, ADR-0007).

The UI declares what it reads and sends as TypeScript types in web/src/api/. These tests read those
types and compare them with the server's Pydantic models and SSE events, so a server change that
would break the UI fails here instead of in the browser. The /mcp contract has its own snapshot test
(test_mcp_server_integration.py); this is the same guard for the UI's surface.
"""

import ast
import re
from pathlib import Path
from typing import Any, get_type_hints

import pytest
from pydantic import BaseModel

REPO = Path(__file__).resolve().parent.parent
WEB_API = REPO / "web" / "src"

# Files whose generators produce the /ask/stream events.
STREAM_SOURCES = [
    REPO / "src" / "agent" / "agent.py",
    REPO / "src" / "agent" / "sdk_agent.py",
    REPO / "src" / "api" / "main.py",
]

_INTERFACE_RE = re.compile(r"export interface (\w+)(?:\s+extends\s+(\w+))?\s*\{(.*?)\n\}", re.DOTALL)
_FIELD_RE = re.compile(r"^\s*(\w+)(\?)?\s*:\s*(.+?)\s*;?\s*$")
_UNION_RE = re.compile(r"export type (\w+)\s*=((?:\s*\|\s*'[^']+')+)")


def _ts_sources() -> str:
    return "\n".join(p.read_text() for p in sorted(WEB_API.rglob("*.ts")))


def _ts_interfaces(source: str) -> dict[str, dict[str, bool]]:
    """Interface name -> {field name: optional}, with `extends` resolved."""
    raw: dict[str, tuple[str | None, dict[str, bool]]] = {}
    for name, parent, body in _INTERFACE_RE.findall(source):
        fields: dict[str, bool] = {}
        for line in body.splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith(("//", "/*", "*")):
                continue
            match = _FIELD_RE.match(line)
            if match:
                fields[match.group(1)] = match.group(2) == "?"
        raw[name] = (parent or None, fields)

    def resolve(name: str) -> dict[str, bool]:
        parent, fields = raw[name]
        return {**(resolve(parent) if parent else {}), **fields}

    return {name: resolve(name) for name in raw}


def _ts_union(source: str, name: str) -> set[str]:
    for union_name, members in _UNION_RE.findall(source):
        if union_name == name:
            return set(re.findall(r"'([^']+)'", members))
    raise AssertionError(f"type {name} not found in web/src")


def _server_event_types() -> set[str]:
    """Every `{"type": ..., "content": ...}` literal the stream code yields or queues."""
    types: set[str] = set()
    for path in STREAM_SOURCES:
        for node in ast.walk(ast.parse(path.read_text())):
            if not isinstance(node, ast.Dict):
                continue
            keys = {k.value: v for k, v in zip(node.keys, node.values, strict=True) if isinstance(k, ast.Constant)}
            type_node = keys.get("type")
            if "content" in keys and isinstance(type_node, ast.Constant) and isinstance(type_node.value, str):
                types.add(type_node.value)
    return types


def _models() -> dict[str, tuple[Any, str]]:
    """UI interface -> (server type, direction). Imported lazily: src.api.main reads settings."""
    from src.agent.history import Turn
    from src.api import main

    return {
        "StreamRequest": (main.AskRequest, "request"),
        "HealthResponse": (main.HealthResponse, "response"),
        "ComponentHealth": (main.ComponentHealth, "response"),
        "ConversationSummary": (main.ConversationSummary, "response"),
        "ConversationDetail": (main.ConversationDetail, "response"),
        "ConversationSearchResult": (main.ConversationSearchResult, "response"),
        "SearchMatch": (main.SearchMatch, "response"),
        "ConversationTurn": (Turn, "response"),
    }


def _server_fields(model: Any) -> dict[str, bool]:
    """Field name -> required. A TypedDict with total=False has no required keys."""
    if isinstance(model, type) and issubclass(model, BaseModel):
        return {name: info.is_required() for name, info in model.model_fields.items()}
    required: frozenset[str] = getattr(model, "__required_keys__", frozenset())
    return {name: name in required for name in get_type_hints(model)}


def test_every_mapped_interface_exists_in_the_ui(mock_settings: Any) -> None:
    """A renamed or deleted UI type must be remapped here, or the checks below go quiet."""
    interfaces = _ts_interfaces(_ts_sources())
    missing = sorted(set(_models()) - set(interfaces))
    assert not missing, f"web/src no longer declares {missing}; update _models() in this test"


@pytest.mark.parametrize(
    "interface",
    [
        "StreamRequest",
        "HealthResponse",
        "ComponentHealth",
        "ConversationSummary",
        "ConversationDetail",
        "ConversationSearchResult",
        "SearchMatch",
        "ConversationTurn",
    ],
)
def test_ui_type_matches_server_model(mock_settings: Any, interface: str) -> None:
    """Fields the UI reads must exist on the server, and fields the server requires must be sent."""
    ui_fields = _ts_interfaces(_ts_sources())[interface]
    model, direction = _models()[interface]
    server = _server_fields(model)

    unknown = sorted(set(ui_fields) - set(server))
    assert not unknown, f"{interface}: the UI declares {unknown}, which {model.__name__} does not have"

    if direction == "request":
        unsent = sorted(name for name, required in server.items() if required and ui_fields.get(name, True))
        assert not unsent, f"{interface}: {model.__name__} requires {unsent}, which the UI does not always send"
    elif isinstance(model, type) and issubclass(model, BaseModel):
        loose = sorted(name for name, optional in ui_fields.items() if not optional and not server[name])
        assert not loose, f"{interface}: the UI treats {loose} as always present, but {model.__name__} may omit them"


def test_stream_event_types_match_ui() -> None:
    """The /ask/stream event types the server emits are exactly the ones the UI handles."""
    ui = _ts_union(_ts_sources(), "StreamEventType")
    server = _server_event_types()
    assert server, "found no stream events in the server; has the stream code moved?"
    assert server == ui, f"server only: {sorted(server - ui)}; UI only: {sorted(ui - server)}"


def test_error_reasons_the_ui_checks_exist_on_the_server() -> None:
    """The UI special-cases some `reason` values; each must still be one the server sends."""
    from src.agent.agent_errors import AgentErrorReason

    source = "\n".join(p.read_text() for p in sorted(WEB_API.rglob("*")) if p.suffix in {".ts", ".vue"})
    checked = set(re.findall(r"reason\s*===\s*'([^']+)'", source))
    assert checked, "the UI checks no error reasons; drop this test or fix the pattern"
    unknown = sorted(checked - {r.value for r in AgentErrorReason})
    assert not unknown, f"the UI checks reasons the server never sends: {unknown}"
