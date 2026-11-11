# Actionable agent error messages (260722)

## Motivation

The 2026-07-22 OAuth refresh-token outage surfaced as the webapp's generic
"failed to produce an answer (often a tool error or LLM provider issue)". The
backend already knew the real cause (`get_token_health()` reported the dead
refresh token) but discarded it, forwarding the SDK's opaque
`Command failed with exit code 1`.

## Change

- New `src/agent/agent_errors.py`: `classify_agent_failure()` maps a failure to
  a typed `AgentError(reason, message, detail)`. `llm_auth_failed` when the
  token is unhealthy; `agent_no_answer` otherwise. Never raises.
- `stream_sdk_agent` emits a structured SSE error event: `content` (message),
  `reason`, `detail`. Backward-compatible — `content` stays the human message.
- New metric `sre_assistant_agent_errors_total{reason}` enables the
  `llm_auth_failed` alert the 260621 journal specced.

Design: `docs/archive/2026-07-22-actionable-agent-errors-design.md`.
Frontend half lives in the `sre-webapp` repo.
