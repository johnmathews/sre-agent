"""docs/tool-reference.md documents exactly the tools the registry serves.

A tool section is a ``## <group> (<when it is enabled>)`` heading: ``(always enabled)``, ``(enabled when ...)`` or
``(MCP server only)``. Every ``### <heading>`` inside a tool section must be a tool name, and every tool the registry
serves with every integration configured must have one. ``###`` headings in other sections are ignored.
"""

import re
from pathlib import Path
from typing import Any

from src.agent.tool_registry import TOOL_SPECS, enabled_specs

DOC = Path(__file__).resolve().parent.parent / "docs" / "tool-reference.md"
TOOL_SECTION = re.compile(r"^## .+ \((?:always enabled|enabled when .+|MCP server only)\)\s*$")
SUBHEADING = re.compile(r"^### (.+?)\s*$")
COUNT = re.compile(r"exposes up to (\d+) tools")


def documented_tools(text: str) -> list[str]:
    """The ``###`` headings inside tool sections, in document order. Fenced code blocks are skipped."""
    names: list[str] = []
    in_tool_section = False
    in_fence = False
    for line in text.splitlines():
        if line.startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        if line.startswith("## "):
            in_tool_section = bool(TOOL_SECTION.match(line))
        elif in_tool_section and (match := SUBHEADING.match(line)):
            names.append(match.group(1))
    return names


def _everything_configured(settings: Any, tmp_path: Path) -> Any:
    """``mock_settings`` already sets every URL; the memory and conversation tools need their paths too."""
    settings.conversation_history_dir = str(tmp_path)
    settings.memory_db_path = str(tmp_path / "memory.db")
    return settings


def test_documented_tools_reads_only_tool_sections() -> None:
    text = (
        "# Tool Reference\n\n"
        "## MCP Server Endpoint\n\n### Access patterns\n\n"
        "## Loki (enabled when `LOKI_URL` is set)\n\n### loki_query_logs\n\n"
        "```bash\n### not_a_heading\n```\n\n"
        "## Time (always enabled)\n\n### get_current_time\n\n"
        "## Conversation History (MCP server only)\n\n### sre_agent_get_conversation\n\n"
        "## Notes\n\n### Something else\n"
    )
    assert documented_tools(text) == ["loki_query_logs", "get_current_time", "sre_agent_get_conversation"]


def test_tool_reference_headings_match_registry(mock_settings: Any, tmp_path: Path) -> None:
    settings = _everything_configured(mock_settings, tmp_path)
    served = [spec.name for spec in enabled_specs(settings, "mcp")]
    # With every integration configured, /mcp serves the whole registry.
    assert sorted(served) == sorted(spec.name for spec in TOOL_SPECS)

    documented = documented_tools(DOC.read_text(encoding="utf-8"))
    duplicates = sorted({name for name in documented if documented.count(name) > 1})
    assert not duplicates, f"documented twice in docs/tool-reference.md: {duplicates}"
    undocumented = sorted(set(served) - set(documented))
    unknown = sorted(set(documented) - set(served))
    assert not undocumented and not unknown, (
        f"docs/tool-reference.md is out of step with TOOL_SPECS. "
        f"No '### <name>' section: {undocumented}. Not a registry tool: {unknown}"
    )


def test_tool_reference_count_matches_registry() -> None:
    match = COUNT.search(DOC.read_text(encoding="utf-8"))
    assert match is not None, "docs/tool-reference.md no longer says 'exposes up to N tools'"
    assert int(match.group(1)) == len(TOOL_SPECS)
