# Code Flow

## Request Lifecycle

### 1. HTTP Request

A user question arrives via one of three interfaces:

- **FastAPI** — `POST /ask` with JSON body `{"question": "...", "session_id": "..."}` (`src/api/main.py`)
- **FastAPI (streaming)** — `POST /ask/stream` with the same JSON body, returns Server-Sent Events (`src/api/main.py`)
- **Streamlit UI** — chat input calls `/ask/stream` SSE endpoint for live tool progress (`src/ui/app.py`)
- **CLI** — interactive REPL calls the agent directly (`src/cli.py`)

### 2. Agent Invocation

The agent is built once at startup via `build_agent()` and stored in `app.state.agent` as an `SREAgent` dataclass.
`build_agent()` detects `LLM_PROVIDER` and constructs either an SDK agent (Anthropic) or LangGraph agent (OpenAI).

**OpenAI / LangGraph path:**
```
src/api/main.py::ask()
  -> invoke_agent(SREAgent, message, session_id)
    -> _invoke_langgraph_agent(agent.langgraph_agent, ...)
      -> agent.ainvoke({"messages": [HumanMessage(content=message)]}, config)
```

**Anthropic / SDK path:**
```
src/api/main.py::ask()
  -> invoke_agent(SREAgent, message, session_id)
    -> invoke_sdk_agent(agent.sdk_options, message, session_id)
      -> ensure_valid_token()  # refresh OAuth access token if expired
      -> query(prompt=full_prompt, options=options)  # claude-agent-sdk
```

The SDK path uses MCP tools (`src/agent/mcp_tools.py`) that wrap the same LangChain tool functions. The system prompt
is built fresh each call with current timestamps and `mcp__sre__` tool name prefixes. Conversation history is injected
into the prompt (context stuffing) since each `query()` call is stateless.

For both paths, `invoke_agent()` and `stream_agent()` dispatch based on `SREAgent.provider`.

### Streaming Flow (SSE)

`stream_agent()` uses LangGraph's `astream_events(version="v2")` filtered to `tool` and `chat_model` event types. It
yields structured dicts as events:

| Event type   | When emitted                        | Content                                      |
| ------------ | ----------------------------------- | --------------------------------------------- |
| `status`     | Start of processing                 | "Thinking..."                                 |
| `tool_start` | Tool execution begins               | Human-readable label + input summary          |
| `tool_end`   | Tool execution completes            | Label + "done"                                |
| `answer`     | Final AI response (no tool_calls)   | Response text + session_id                    |
| `error`      | Unrecoverable error                 | Error message                                 |

The Streamlit UI consumes these events via `httpx.stream()`, rendering a live checklist of tool calls with progress
indicators before displaying the final answer.

### 3. Tool Selection

LangGraph's `create_agent` compiles the LLM + tools into a graph. The LLM:

1. Reads the system prompt (which includes the tool selection guide, current date/time, and Prometheus retention limit)
2. Examines the user's question
3. Decides which tool(s) to call (or responds directly)
4. Calls tools, reads results, and reasons about the response

### 4. Tool Execution

Each tool follows the same pattern:

```python
@tool("tool_name", args_schema=InputSchema)
async def tool_name(param: str) -> str:
    # 1. Guard: check if service is configured
    # 2. Build HTTP request (URL, headers, params)
    # 3. Make async httpx request with timeout
    # 4. Handle errors (ConnectError, Timeout, HTTPStatusError)
    # 5. Format response data into readable string
    # 6. Return formatted string to LLM
```

### 5. Response Formatting

Tool results are formatted into human-readable strings (not raw JSON) so the LLM can reason about them effectively. The
LLM composes a final answer from tool results and its knowledge.

### 6. Conversation Persistence

After `ainvoke()` returns, the user question and final assistant response are each saved as a turn to a JSON
file in `/app/conversations`, using the unified turn-based format:

```
result = agent.ainvoke(...)
messages = result["messages"]
response_text = extract final AIMessage text
  -> save_turn(history_dir, session_id, "user", message, model, "openai")
  -> save_turn(history_dir, session_id, "assistant", response_text, model, "openai")
       -> load existing file (if any), append turn, bump updated_at/turn_count
       -> set title from first user message (truncated to 60 chars) on first turn only
       -> atomic write: tempfile.mkstemp() + os.replace()
       -> errors logged and swallowed (never crashes the request)
```

Tool calls and tool responses are NOT persisted in `turns[]` — they live only in LangGraph's in-process
checkpointer. If the agent entered the error recovery path (corrupted tool-call history), the fresh session
ID is used for the saved file, not the original session ID.

Before `ainvoke()`, if the LangGraph checkpointer is empty for this `thread_id`, prior turns are loaded from
the file via `load_turns_as_langchain_messages()` and prepended to the input, enabling resume after process
restart.

See `docs/conversation-history.md` for the full schema, API endpoints, and UI sidebar details.

### 7. Post-Response Actions

After extracting the response text, `invoke_agent()` runs `_post_response_actions()` (best-effort, never crashes):

```
_post_response_actions(messages, question, response_text)
  -> extract tool names from AIMessage.tool_calls
  -> save_query_pattern(question, tool_names)  # memory store
  -> cleanup_old_query_patterns(keep=100)       # prevent unbounded growth
  -> detect_incident_suggestion(tool_names, response_text)
       -> if investigation tools used AND outcome keywords found:
          append suggestion to record incident
```

### 8. Response Return

```
agent.ainvoke() returns {"messages": [...]}
  -> invoke_agent extracts last AIMessage.content
    -> FastAPI returns AskResponse(response=..., session_id=...)
```

## Tool Registration

Tools are registered in `src/agent/agent.py::_get_tools()`:

```
Always included:
  - prometheus_search_metrics, prometheus_instant_query, prometheus_range_query
  - grafana_get_alerts, grafana_get_alert_rules

Conditional (config-dependent):
  - truenas_* tools  (if TRUENAS_URL is set)
  - loki_* tools     (if LOKI_URL is set)
  - proxmox_* tools  (if PROXMOX_URL is set)
  - pbs_* tools      (if PBS_URL is set)
  - runbook_search   (if vector store directory exists)
  - memory_* tools   (if MEMORY_DB_PATH is set)
```

## Settings Loading

`src/config.py::Settings` uses `pydantic-settings` to load from environment variables and `.env`:

```
get_settings() -> Settings()  [cached via @lru_cache]
  1. Reads .env file
  2. Validates required fields (prometheus_url, grafana_url, grafana_service_account_token)
  3. Validates provider-specific keys (openai_api_key if LLM_PROVIDER=openai, anthropic_api_key if anthropic)
  4. Optional fields default to empty string (truenas_url, loki_url, proxmox_url, pbs_url, smtp_host, etc.)
  5. Returns singleton Settings instance
```

Each tool module imports `get_settings` independently. In tests, `conftest.py::mock_settings` patches `get_settings` at
every import site (16 patch sites as of Phase 7).

### Provider Selection

`LLM_PROVIDER` selects which LLM backend to use:

```
LLM_PROVIDER=openai     → OPENAI_API_KEY required, uses ChatOpenAI
LLM_PROVIDER=anthropic  → ANTHROPIC_API_KEY required, uses ChatAnthropic
```

The factory function `src/agent/llm.py::create_llm()` centralises LLM instantiation. All three call sites — `build_agent()`,
`_generate_narrative()` (report generator), and `judge_answer()` (eval judge) — use this factory instead of constructing
`ChatOpenAI` directly. Both providers share the same `MetricsCallbackHandler` — Claude models populate `llm_output` with
`token_usage` and `model_name` in the same format as OpenAI, so cost tracking works automatically.

### Anthropic OAuth Token Handling

When `ANTHROPIC_API_KEY` starts with `sk-ant-oat`, `create_anthropic_chat()` activates OAuth mode:

```
create_anthropic_chat(api_key, model, temperature, max_tokens)
  1. Detect OAuth prefix → set default_headers:
     - Authorization: Bearer {token}
     - anthropic-beta: claude-code-20250219,oauth-2025-04-20
     - user-agent: claude-cli/2.1.62
     - x-app: cli
  2. Construct ChatAnthropic with placeholder api_key + headers
  3. Post-construction: inject Omit() into _custom_headers["X-Api-Key"]
     on both sync and async clients to suppress the placeholder
```

`build_agent()` also prepends `"You are Claude Code, Anthropic's official CLI for Claude."` to the system prompt when
OAuth is detected. Regular API keys (`sk-ant-api03-*`) skip all of this and pass the key directly.

## Metrics Flow

Self-instrumentation metrics are collected at two levels:

### Request level (FastAPI)

```
POST /ask (and POST /ask/stream)
  -> REQUESTS_IN_PROGRESS.inc()
  -> start = time.monotonic()
  -> invoke_agent(...) or stream_agent(...)
  -> REQUEST_DURATION.observe(elapsed)
  -> REQUESTS_TOTAL.labels(status="success"|"error").inc()
  -> REQUESTS_IN_PROGRESS.dec()  (in finally block — always runs)
```

On error, both the error counter and duration histogram are recorded _before_ the HTTPException is raised, so failed
requests are fully instrumented.

### Agent level (LangChain callback handler)

The `MetricsCallbackHandler` (`src/observability/callbacks.py`) captures tool-call and LLM metrics from _inside_
LangGraph's execution loop.

#### Why callbacks instead of decorating tools?

LangGraph runs an internal agentic loop: the LLM decides to call a tool → the tool runs → the result is fed back → the
LLM decides whether to call another tool or respond. This loop is invisible to FastAPI middleware, which only sees the
outer HTTP request. Three alternative approaches were considered:

1. **Decorating each tool function** — requires modifying every `@tool` definition and remembering to decorate new tools.
   Fragile and repetitive.
2. **FastAPI middleware** — can time the overall request but cannot see individual tool calls or LLM invocations inside
   the agent loop.
3. **LangChain callbacks** (chosen) — `BaseCallbackHandler` is invoked automatically by LangGraph at each lifecycle
   event. Zero changes to tool code. New tools are automatically instrumented. Works inside the agent's internal loop.

#### Lifecycle

A fresh `MetricsCallbackHandler` instance is created per request in `invoke_agent()` and injected via
`config["callbacks"]`. The handler instance is request-scoped (its `_start_times` dict is private), but all
counter/histogram writes target the module-level Prometheus singletons in `metrics.py`.

```
invoke_agent(agent, message, session_id)
  -> metrics_cb = MetricsCallbackHandler()
  -> config = {"configurable": {"thread_id": session_id}, "callbacks": [metrics_cb]}
  -> agent.ainvoke({"messages": [...]}, config)
```

During execution, LangGraph calls the handler at these points:

**Tool lifecycle:**

```
on_tool_start(serialized, input_str, run_id)
  -> stores run_id → (time.monotonic(), tool_name) in _start_times dict

on_tool_end(output, run_id)
  -> pops run_id from _start_times
  -> duration = now - start_time
  -> TOOL_CALL_DURATION.labels(tool_name).observe(duration)
  -> TOOL_CALLS_TOTAL.labels(tool_name, status="success").inc()

on_tool_error(error, run_id)
  -> pops run_id from _start_times
  -> TOOL_CALL_DURATION.labels(tool_name).observe(duration)
  -> TOOL_CALLS_TOTAL.labels(tool_name, status="error").inc()
```

**LLM lifecycle:**

```
on_llm_end(response, run_id)
  -> LLM_CALLS_TOTAL.labels(status="success").inc()
  -> extracts token_usage from response.llm_output (if present)
  -> LLM_TOKEN_USAGE.labels(type="prompt").inc(prompt_tokens)
  -> LLM_TOKEN_USAGE.labels(type="completion").inc(completion_tokens)
  -> looks up model pricing (falls back to gpt-4o rates for unknown models)
  -> LLM_ESTIMATED_COST.inc(calculated_cost)

on_llm_error(error, run_id)
  -> LLM_CALLS_TOTAL.labels(status="error").inc()
```

#### Error resilience

Every callback method is wrapped in `try/except`. Metrics collection must never crash a request — if a callback fails
(e.g., unexpected `llm_output` format, missing `token_usage` key), it logs at DEBUG level and continues. This is critical
because `llm_output` format varies across LLM providers and can change between library versions.

#### Cost estimation

The handler matches `model_name` from `llm_output` against a prefix-based pricing table:

| Model prefix      | Prompt (per 1M tokens) | Completion (per 1M tokens) |
| ----------------- | ---------------------- | -------------------------- |
| `gpt-4o-mini`     | $0.15                  | $0.60                      |
| `gpt-4o`          | $2.50                  | $10.00                     |
| `gpt-4-turbo`     | $10.00                 | $30.00                     |
| `claude-sonnet-4` | $3.00                  | $15.00                     |
| `claude-opus-4`   | $15.00                 | $75.00                     |
| `claude-haiku-4`  | $0.80                  | $4.00                      |

Unknown models fall back to `gpt-4o` pricing (the conservative default). Claude pricing is informational — when using an
Anthropic Max subscription (direct API via `claude setup-token` or via `claude-max-api-proxy`), the actual cost is the
flat subscription rate. The total cost counter is monotonically increasing — Prometheus `rate()` computes cost per time
window for the dashboard.

### Exposition

`GET /metrics` returns all metrics in Prometheus exposition format via `prometheus_client.generate_latest()`.

### Health gauge updates

`GET /health` updates the `sre_assistant_component_healthy` gauge for each component after checking its status. This
means the gauge reflects the last health check result. Prometheus scrapes `/metrics` on its own schedule, so the gauge
value between health checks remains at the last-known state.

## Report Generation Flow

The weekly report is generated via direct API queries (not the LangChain agent) for determinism and cost efficiency.

### On-demand (`POST /report`)

```
POST /report {lookback_days: 7}
  -> generate_report(lookback_days)
       -> collect_report_data(lookback_days)
            -> asyncio.gather(
                 _collect_alert_summary(),   # Grafana API
                 _collect_slo_status(),      # Prometheus (per-component availability)
                 _collect_tool_usage(),      # Prometheus queries
                 _collect_cost_data(),       # Prometheus queries
                 _collect_loki_errors(),     # Loki (current + previous period + samples)
                 _collect_backup_health(),   # PBS API (if configured)
               )
       -> _load_previous_report()              # Memory store (if configured)
       -> _generate_narrative(collected, prev)  # Single LLM call with prior context
       -> format_report_markdown(report_data)   # Pure function → markdown
       -> format_report_html(report_data)       # Pure function → HTML email
       -> _archive_report(report_data, md)      # Memory store (if configured)
       -> _compute_post_report_baselines(days)  # Prometheus → Memory (if configured)
       -> return GeneratedReport(markdown, html)
  -> send_report_email(result.markdown, result.html)  (if SMTP configured)
       # Sends multipart/alternative: HTML + plain-text fallback
  -> REPORTS_TOTAL.labels(trigger="manual", status="success").inc()
  -> REPORT_DURATION.observe(elapsed)
  -> return ReportResponse(report=result.markdown, emailed=bool, timestamp=iso)
```

### Scheduled (APScheduler)

```
start_scheduler()  (called in FastAPI lifespan)
  -> CronTrigger.from_crontab(settings.report_schedule_cron)
  -> AsyncIOScheduler.add_job(_scheduled_report_job)
  -> _scheduled_report_job()  (fires on cron schedule)
       -> generate_report()  → GeneratedReport(markdown, html)
       -> send_report_email(result.markdown, result.html)  (if configured)
       -> REPORTS_TOTAL.labels(trigger="scheduled", status=...).inc()
```

### Collector Error Handling

Each collector runs independently. If a collector raises (e.g., Prometheus is down),
`asyncio.gather(return_exceptions=True)` catches it and stores `None` for that section. The report is always produced,
with "data unavailable" placeholders for failed sections.

## Health Check Flow

`GET /health` checks each dependency:

1. Prometheus — `GET /-/healthy`
2. Grafana — `GET /api/health` with auth header
3. Loki (if configured) — `GET /ready`
4. TrueNAS (if configured) — `GET /api/v2.0/core/ping` with Bearer token
5. Proxmox VE (if configured) — `GET /api2/json/version` with API token
6. PBS (if configured) — `GET /api2/json/version` with API token
7. Vector store — checks if `chroma_db/` directory exists

Returns overall status: "healthy" (all OK), "degraded" (some failing), "unhealthy" (all failing).

## Eval Framework Flow

The eval framework (`make eval`) tests the agent's end-to-end reasoning with real LLM calls against mocked
infrastructure. It runs separately from `make test` because it costs tokens.

```
scripts/run_eval.py
  -> load_eval_cases(case_ids)           # YAML files from src/eval/cases/
  -> for each case:
       run_eval_case(case, api_key, model, ...)
         1. Build FakeSettings (real LLM key + fake infra URLs)
         2. Patch get_settings at all 11 import sites
         3. Disable runbook_search (no vector store needed)
         4. Set up respx mocks from case.mocks
         5. build_agent() + agent.ainvoke() with full message history
         6. Extract tool names from AIMessage.tool_calls
         7. Score tools: missing = must_call - called, forbidden = must_not_call ∩ called
         8. Judge answer: send (question, answer, rubric) to grading LLM
         9. Return EvalResult(tool_score, judge_score, answer)
       print_case_result(result)
  -> print_summary(results)
  -> exit(0 if all passed else 1)
```

### Why `agent.ainvoke()` not `invoke_agent()`?

`invoke_agent()` discards the message list and returns only the final text. The eval runner needs the full message list
to extract `AIMessage.tool_calls` for deterministic tool scoring. It reimplements the 3-line answer extraction.

### Why HTTP-level mocking (not tool-function mocking)?

HTTP-level mocking via respx tests the full tool implementation — URL construction, headers, query parameters, response
parsing, error formatting. Function-level mocking would only test whether the LLM picks the right tool name.

### Eval cost estimation

Each eval case makes **multiple real LLM API calls** — the agent runs a ReAct loop (2–4 round-trips per case), and then
the judge makes one additional call. Every round-trip resends the full accumulated context, so the dominant cost driver
is the **system prompt (~6,400 tokens) and tool definitions (~1,500+ tokens)** included in every call.

**Token breakdown per eval case (gpt-4o-mini):**

| Component | Prompt tokens | Completion tokens |
| --------- | ------------- | ----------------- |
| Agent round 1 (decide tool) | ~7,930 (system + tools + question) | ~50 (tool call) |
| Agent round 2 (answer or next tool) | ~8,180 (+ prev completion + tool result) | ~200 (answer) |
| Additional rounds (multi-tool cases) | +200 per round | ~50-200 per round |
| Judge call | ~500 (question + answer + rubric) | ~50 (JSON verdict) |

**Cost per case (gpt-4o-mini at $0.15/$0.60 per 1M prompt/completion):**

| Case type | Prompt tokens | Completion tokens | Cost |
| --------- | ------------- | ----------------- | ---- |
| Simple (1 tool, 2 LLM calls) | ~16,600 | ~300 | ~$0.003 |
| Complex (3 tools, 4 LLM calls) | ~33,000 | ~450 | ~$0.005 |

**Full suite (28 cases, mixed complexity): ~$0.10–0.15 per run.**

Key factors that make eval expensive relative to naive token estimates:
- System prompt (25KB / ~6,400 tokens) is resent on **every** LLM round-trip
- Tool schemas (~300 tokens each × 5–9 tools) are resent on every round-trip
- Multi-tool cases compound: each round adds all prior messages to the context
- The judge call is cheap (~$0.0001) but there are 28 of them
