# Eval Quality: Improvement History and Known Issues

This document tracks eval answer quality improvements, known failure patterns, and lessons learned
about writing robust LLM-as-judge eval cases. Refer to this when debugging eval failures or writing
new eval cases.

## How the Eval Works

Two independent scoring axes — both must pass for a case to pass:

1. **Tool score (deterministic)**: Did the agent call the tools in `must_call`? Did it avoid
   `must_not_call`? Binary pass/fail, no LLM involved.
2. **Judge score (LLM-as-judge)**: A smaller LLM evaluates the agent's final answer against a
   numbered rubric. Also receives mock data summary to detect hallucination. Binary pass/fail.

Key architecture details:
- Unmocked URLs return **503** via `router.route().mock(return_value=httpx.Response(503, ...))`.
- `_extract_tool_calls()` captures the LLM's **requested** calls from `AIMessage.tool_calls`,
  not just successful executions. A call to a non-existent tool still appears in the list.
- `required_services` controls which service URLs are set in fake settings. Empty URL = tool not
  registered. But the LLM may still attempt to call unregistered tools (returning errors).
- `_summarize_available_data()` builds judge context from mocks + memory_seed.

## Improvement History

### 2026-03-01: 26/30 → 4 fixes (judge, mock, tool description)

Four answer quality failures with different root causes:

| Case | Root Cause | Fix |
|------|-----------|-----|
| proxmox-list-guests | Judge self-corrected (two JSON objects), parser failed on concatenated JSON | Added fallback in `judge.py` to extract the last `{...}` block when `json.loads` fails |
| prom-container-state | `_summarize_available_data()` truncated mock body at 500 chars, cutting off the adguard entry — judge couldn't see it and called it fabrication | Increased truncation limit from 500 to 2000 chars |
| prom-vm-count | Agent said "no stopped VMs" but data only shows running count; metadata `help` was generic "Guest information" | Changed metadata help to "Running Proxmox VE guest count (VMs and LXCs currently online)" |
| memory-baseline-check | Tool schema example `node_cpu_seconds_total` steered LLM away from seeded baseline names | Changed example to `node_cpu_usage_ratio` (matches first seeded variant) |

### 2026-03-01: 21/30 → 9 cases fixed (rubric + mock issues)

All 9 failures were **answer quality** (tool selection passed 30/30). Three root cause categories:

**Category 1: Judge can't verify tool calls** (memory-incident-search, memory-previous-report,
memory-baseline-check)

Rubric criteria like "Call memory_search_incidents" caused false failures. The judge only sees the
final answer text, not actual tool invocations — so it checked whether the answer *mentions* calling
the tool, not whether it was actually called. The deterministic tool scorer already enforces
`must_call`/`must_not_call`.

Fix: Changed rubric criteria to focus on answer content ("Reference past incident data...",
"Reference data from the archived report...") instead of tool-calling actions.

**Category 2: Missing mocks cause agent confusion** (cross-tool-alert-investigation,
prom-container-state)

The agent commonly calls `prometheus_search_metrics` before `prometheus_instant_query`. This hits
`/api/v1/label/__name__/values` which had no mock in several cases. The 503 response made the
agent conclude "API connectivity issues" and ignore valid data from other calls.

Fix: Added `/api/v1/label/__name__/values` mocks to cases that expect `prometheus_instant_query`.
Also added Grafana `/api/v1/provisioning/alert-rules` mock to cross-tool-alert-investigation.

**Category 3: Overly strict or ambiguous rubrics** (alert-explain-high-cpu, prom-vm-count,
proxmox-list-guests, truenas-pool-health, prom-container-state)

| Case | Issue | Fix |
|------|-------|-----|
| alert-explain-high-cpu | Judge treated computed duration from startsAt as fabrication | Added note: computed duration is acceptable |
| prom-vm-count | Agent fabricated "no stopped VMs"; rubric asked for vague "useful context" | Changed to specific anti-fabrication criterion |
| proxmox-list-guests | Judge treated maxmem→"4 GB RAM" as fabrication (mem also exists) | Added note: both maxmem and mem are valid |
| truenas-pool-health | Agent said "10.0 TiB", rubric said "approximately 11TB" (same bytes) | Accept both TiB and TB unit systems |
| prom-container-state | Agent flagged adguard in header but not in structured list | Minor rubric softening |

**Additional fix: memory-baseline-check metric name variants**

`get_baseline()` does exact match on `metric_name`. Seeded baselines with 3 variants
(`node_cpu_usage_ratio`, `cpu_usage`, `cpu_usage_ratio`) so whichever name the LLM picks is more
likely to find a match.

## Known Fragile Cases

These cases may still fail intermittently due to LLM variance:

### memory-baseline-check
The 3 metric name variants improve hit rate but aren't guaranteed. The tool's example in
`CheckBaselineInput` was changed from `node_cpu_seconds_total` to `node_cpu_usage_ratio` (matching
the first seeded variant). This should steer the LLM toward a name that has a baseline.

**If this keeps failing**, consider: (a) fuzzy/substring matching in `get_baseline()`, or
(b) more metric name variants in the seed data.

### cross-tool-alert-investigation
Agent makes ~11 tool calls including attempts to call tools that may not be registered (proxmox,
loki). Unregistered tool calls return errors that add noise. The fix added mocks for the most
impactful 503s (prometheus search, grafana alert rules), but edge-case errors from non-existent
tools may still cause the agent to report "connectivity issues."

### prom-container-state
Previous formatting variance issue was compounded by a data truncation bug: the 500-char limit on
mock body summaries cut off the adguard entry, so the judge couldn't see it and flagged it as
fabrication. Fixed by increasing limit to 2000 chars. LLM formatting non-determinism may still
cause intermittent failures.

### prom-vm-count
Agent tends to infer "no stopped VMs" from a running-only count. Metadata help text now clarifies
the metric only counts running guests, which should reduce fabrication. The rubric criterion 2
specifically checks for this anti-fabrication.

## Guidelines for Writing Eval Cases

Lessons learned from debugging these failures:

### Rubric guidelines
1. **Never use "Call [tool_name]" in rubric criteria.** The judge can't verify tool calls. Use
   content-focused criteria instead. Tool calling is enforced by `must_call`/`must_not_call`.
2. **Accept both TiB and TB** when rubrics mention storage sizes. Include a note:
   "either unit system is correct."
3. **Add notes for derived values.** If the agent can legitimately compute a value from available
   data (e.g., duration from a timestamp, percentage from two metrics), add a rubric note saying
   this is acceptable and not fabrication.
4. **Be specific, not vague.** "Provide useful context" is judge-dependent and unreliable.
   "Not fabricate claims about X" is specific and testable.
5. **Clarify ambiguous data fields.** If mock data has multiple representations of the same
   concept (maxmem vs mem, different unit systems), add a rubric note explaining which
   interpretations are acceptable.

### Mock guidelines
1. **Always mock `/api/v1/label/__name__/values`** for cases that expect `prometheus_instant_query`.
   The agent calls `prometheus_search_metrics` before querying — if this returns 503, the agent
   may conclude the service is down and ignore valid data.
2. **Mock adjacent endpoints** the agent is likely to call. Common ones:
   - Prometheus: `/api/v1/query`, `/api/v1/query_range`, `/api/v1/metadata`,
     `/api/v1/label/__name__/values`
   - Grafana: `/api/alertmanager/grafana/api/v2/alerts/groups`,
     `/api/v1/provisioning/alert-rules`
3. **Remember the 503 catch-all.** Any unmocked URL returns 503. If the agent tries to call
   tools from services not in `required_services`, those HTTP calls also return 503. The agent
   may interpret multiple 503s as a systemic connectivity issue.

### Diagnostic methodology guidelines
1. **Test evidence-gathering, not just tool calls.** The `diagnostic-service-failure` eval case
   tests whether the agent checks Loki logs for actual error messages before diagnosing. The rubric
   verifies the agent cites specific error messages (`BlockingIOError`, `RuntimeError`) and correctly
   categorizes them as resource exhaustion — not pattern-matching "git fetch failure" to "expired
   token." This is the eval equivalent of the system prompt's "Diagnostic Methodology" section.
2. **Include both public and private resources in mock data.** When testing scope analysis, include
   failures from resources that don't require authentication alongside ones that do. If the agent
   claims "auth failure" when unauthenticated resources also fail, the rubric should catch this.
3. **Mock Loki endpoints comprehensively.** Diagnostic cases need `/loki/api/v1/query_range`,
   `/loki/api/v1/labels`, `/loki/api/v1/label/hostname/values`, and `/ready`. Missing mocks cause
   503s that the agent may interpret as "Loki is down" rather than using available data.

### Memory tool guidelines
1. **Seed baselines with multiple metric name variants** since `get_baseline()` does exact match.
   The LLM may use any reasonable metric name.
2. **Test memory tool selection separately from answer quality.** The `must_call` enforces the
   tool is called. The rubric should focus on whether the answer includes the data that would
   come from the tool.
