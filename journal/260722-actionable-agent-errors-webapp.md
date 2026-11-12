# Reason-specific agent error messages (260722)

Backend now emits structured SSE `error` events (`content` + `reason` +
`detail`). `ErrorBubble` keys its heading + explanation on `reason`:
`llm_auth_failed` → "Agent can't reach the LLM" + the operator-facing message;
otherwise the existing generic copy. `reason` absent ⇒ unchanged behavior
(`content` behind Details), so old backends and the prior E2E test still pass.

Contract: `docs/api-integration.md`. Backend + design spec live in `sre-agent`.
