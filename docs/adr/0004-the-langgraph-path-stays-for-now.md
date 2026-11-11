# ADR-0004: The LangGraph/OpenAI agent path stays for now

**Status:** Accepted. **Date:** 2026-03-21, recorded 2026-11-11.

## 1. Context

- The chat agent behind `/ask` has two implementations, chosen by `LLM_PROVIDER` in `src/agent/agent.py`.
  `anthropic` runs the Claude Agent SDK, and `openai` runs a LangGraph agent (`create_agent` with a `MemorySaver`
  checkpointer). Production runs `anthropic`.
- Commit `f5f2d64` (2026-03-21) added the SDK path and kept LangGraph for OpenAI.
- The eval runner (`src/eval/runner.py`) is written against the LangGraph agent. It calls `ainvoke` and scores tool
  choice from the `AIMessage.tool_calls` in LangGraph's message history.
- The 2026-11-11 evaluation found that the LangGraph path serves only `LLM_PROVIDER=openai` and the evals. Retiring it,
  or porting the evals to the SDK path, was left to the owner.

## 2. Decision

Keep the LangGraph/OpenAI path. It stays until the evals can score the SDK agent and the owner decides whether an
OpenAI provider is still wanted. Issue #42 tracks that decision.

## 3. Consequences

1. Two agent loops stay in the code. Both take their tools from the registry (`enabled_specs(settings, "agent")`), so
   their tool lists match.
2. `langgraph`, `langchain-openai` and `openai` stay dependencies, capped below their next major versions in
   `pyproject.toml`. The runbook embeddings need `OPENAI_API_KEY` whichever provider runs the agent.
3. The evals measure the LangGraph agent, so their scores say little about the SDK agent that serves production.
4. Tool descriptions steer both agents and the MCP clients, so a description change can move eval results.
