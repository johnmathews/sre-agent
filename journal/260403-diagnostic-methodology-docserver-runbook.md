# Diagnostic Methodology & Docserver Runbook

## Context

Reviewed the last production conversation (session `58c7bdbb`, April 3 2026) where the SRE agent
confidently misdiagnosed documentation server git fetch failures as an "expired GitHub PAT" without
making a single tool call to check the actual error messages. The real root cause was container
memory exhaustion — `BlockingIOError: [Errno 11] Resource temporarily unavailable` from
`ThreadPoolExecutor(max_workers=4)` forking 4 parallel git fetches, each duplicating the ~800MB
Python process and exceeding the 1536MB container limit.

The agent had Loki tools available and could have queried `{service_name="docserver",
detected_level=~"error|warn"}` to see the actual errors. Instead it pattern-matched "GitHub fetch
failures" → "expired token" — a common-cause heuristic that ignored the evidence.

## Changes

### System prompt: Diagnostic Methodology section

Added a structured "Diagnostic Methodology — Evidence Before Diagnosis" section with four steps:

1. **Gather actual error messages** — check Loki logs before forming hypotheses
2. **Identify error category** — map error types to root cause categories based on the actual
   message (resource exhaustion, network, auth, filesystem, performance)
3. **Check failure scope** — use the pattern of what's failing to narrow causes
4. **Form and state diagnosis** — only after evidence, with appropriate hedging

Also updated the existing investigation guideline from "query metrics first" to "check Loki logs
for actual error messages first, then query metrics."

### Docserver runbook

Created `runbooks/documentation-server.md` covering the documentation server's architecture,
Loki labels (`hostname=infra`, `service_name=docserver`, `container=documentation-server`), and
four documented failure modes — critically including the memory exhaustion from parallel git
fetches. The runbook explicitly documents the diagnostic distinction between resource exhaustion
(all repos fail identically) and auth failure (only private repos fail).

### Eval case

Created `src/eval/cases/diagnostic-service-failure.yaml` that simulates the exact misdiagnosis
scenario — the agent is told about git fetch failures and must check Loki logs to find
`BlockingIOError`/`RuntimeError` errors, correctly categorize them as resource exhaustion, and
NOT pattern-match to "expired token."

### Tests

Added 5 new system prompt unit tests verifying the diagnostic methodology section content.
Updated existing `test_advises_metrics_first` → `test_advises_logs_then_metrics` to reflect the
new investigation priority order.

## Key Decisions

- The diagnostic methodology is prompt-level guidance, not a code change. The agent's tools are
  already capable — the gap was in reasoning, not capability.
- Error category mapping in the prompt is deliberately not exhaustive — it covers the most common
  categories to prime the agent's thinking, not to replace reading the actual error message.
- The eval case uses `must_call: [loki_query_logs]` to enforce that the agent checks logs, and
  the rubric verifies it doesn't present a hypothesis that contradicts the log evidence.

## Test Results

799 tests passing (up from 781 at session start — 18 new tests from the diagnostic methodology
additions). Lint and typecheck clean.
