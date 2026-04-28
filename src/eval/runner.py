"""Core eval runner — patches settings, mocks HTTP, invokes agent, scores results."""

import asyncio
import contextlib
import logging
import os
import sys
import tempfile
import time
from collections.abc import Coroutine
from datetime import UTC, datetime
from typing import Any
from unittest.mock import patch

import httpx
import respx
from langchain_core.messages import AIMessage

from src.eval.judge import judge_answer
from src.eval.models import EvalCase, EvalResult, JudgeScore, MemorySeed, ToolScore

logger = logging.getLogger(__name__)


async def _with_progress[T](label: str, coro: Coroutine[Any, Any, T]) -> T:
    """Await a coroutine while printing elapsed seconds to stderr."""
    task = asyncio.create_task(coro)
    start = time.monotonic()
    while not task.done():
        elapsed = time.monotonic() - start
        print(f"\r  {label} ({elapsed:.0f}s)...", end="", file=sys.stderr, flush=True)
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=1.0)
        except TimeoutError:
            continue
        except Exception:
            break
    elapsed = time.monotonic() - start
    print(f"\r  {label} ({elapsed:.0f}s)    ", file=sys.stderr, flush=True)
    return task.result()


_MAX_RETRIES = 3
_RETRY_BACKOFF = [30, 60, 120]  # seconds to wait before each retry

# Use a different model for the judge to avoid self-evaluation bias.
# The judge task (structured rubric assessment) is simpler than the agent task,
# so a smaller/cheaper model works well.
_JUDGE_MODELS: dict[str, str] = {
    "openai": "gpt-4o-mini",
    "anthropic": "claude-haiku-4-5-20251001",
}


def _is_rate_limit_error(exc: BaseException) -> bool:
    """Check if an exception is a rate limit (429) error."""
    msg = str(exc).lower()
    return "429" in msg or "rate_limit" in msg or "rate limit" in msg


async def _invoke_with_retry(
    agent: Any,
    inputs: dict[str, Any],
    config: dict[str, Any],
) -> dict[str, Any]:
    """Invoke the agent with retry on rate limit errors."""
    for attempt in range(_MAX_RETRIES + 1):
        try:
            return await _with_progress(
                "Agent thinking",
                agent.ainvoke(inputs, config=config),
            )
        except Exception as exc:
            if _is_rate_limit_error(exc) and attempt < _MAX_RETRIES:
                wait = _RETRY_BACKOFF[attempt]
                print(
                    f"\n  Rate limited — waiting {wait}s before retry ({attempt + 1}/{_MAX_RETRIES})...",
                    file=sys.stderr,
                    flush=True,
                )
                await asyncio.sleep(wait)
                continue
            raise
    raise RuntimeError("Unreachable")  # pragma: no cover


# Every module that imports get_settings needs to be patched so the cached
# reference is overridden.  Same pattern as tests/conftest.py mock_settings.
_SETTINGS_PATCH_SITES = [
    "src.config.get_settings",
    "src.agent.tools.prometheus.get_settings",
    "src.agent.tools.grafana_alerts.get_settings",
    "src.agent.tools.proxmox.get_settings",
    "src.agent.tools.pbs.get_settings",
    "src.agent.tools.loki.get_settings",
    "src.agent.tools.truenas.get_settings",
    "src.agent.tools.disk_status.get_settings",
    "src.agent.tools.clock.get_settings",
    "src.agent.agent.get_settings",
    "src.agent.retrieval.embeddings.get_settings",
    "src.api.main.get_settings",
    "src.memory.store.get_settings",
    "src.memory.baselines.get_settings",
]

# Service name → settings attribute that controls conditional tool registration
_SERVICE_URL_ATTRS: dict[str, str] = {
    "proxmox": "proxmox_url",
    "pbs": "pbs_url",
    "loki": "loki_url",
    "truenas": "truenas_url",
}


def _build_fake_settings(
    case: EvalCase,
    openai_api_key: str,
    openai_model: str,
    openai_base_url: str = "",
    llm_provider: str = "openai",
    anthropic_api_key: str = "",
    anthropic_model: str = "claude-sonnet-4-20250514",
) -> object:
    """Build a FakeSettings object with real LLM creds and fake infra URLs.

    Only services listed in case.required_services get non-empty URLs;
    the rest get empty strings so their tools are not registered.
    """
    attrs: dict[str, Any] = {
        "llm_provider": llm_provider,
        "openai_api_key": openai_api_key,
        "openai_model": openai_model,
        "openai_base_url": openai_base_url,
        "anthropic_api_key": anthropic_api_key,
        "anthropic_model": anthropic_model,
        "extra_docs_dirs": "",
        # Always-required services
        "prometheus_url": "http://prometheus.test:9090",
        "grafana_url": "http://grafana.test:3000",
        "grafana_service_account_token": "glsa_eval_fake",
        # Defaults for optional services (empty = disabled)
        "proxmox_url": "",
        "proxmox_api_token": "",
        "proxmox_verify_ssl": False,
        "proxmox_ca_cert": "",
        "proxmox_node": "proxmox",
        "pbs_url": "",
        "pbs_api_token": "",
        "pbs_verify_ssl": False,
        "pbs_ca_cert": "",
        "pbs_node": "localhost",
        "pbs_default_datastore": "backups",
        "loki_url": "",
        "truenas_url": "",
        "truenas_api_key": "",
        "truenas_verify_ssl": False,
        "truenas_ca_cert": "",
        "memory_db_path": "",
        "documentation_mcp_url": "",
        "user_timezone": "UTC",
    }

    # Enable services that the case needs
    for svc in case.required_services:
        attr = _SERVICE_URL_ATTRS.get(svc)
        if attr:
            # Use the test URL from the mock definitions
            attrs[attr] = f"http://{svc}.test"
        # Also set auth tokens for services that need them
        if svc == "proxmox":
            attrs["proxmox_url"] = "https://proxmox.test:8006"
            attrs["proxmox_api_token"] = "eval@pam!eval=fake-token"
        elif svc == "pbs":
            attrs["pbs_url"] = "https://pbs.test:8007"
            attrs["pbs_api_token"] = "eval@pbs!eval=fake-token"
        elif svc == "truenas":
            attrs["truenas_url"] = "https://truenas.test"
            attrs["truenas_api_key"] = "1-fake-eval-truenas-key"
        elif svc == "loki":
            attrs["loki_url"] = "http://loki.test:3100"

    return type("FakeSettings", (), attrs)()


def _seed_memory_db(db_path: str, seed: MemorySeed) -> None:
    """Create the memory DB schema and seed it with test data."""
    from src.memory.store import get_connection, init_schema, save_baselines, save_incident, save_report

    conn = get_connection(db_path)
    try:
        init_schema(conn)
        now = datetime.now(UTC).isoformat()

        for baseline in seed.baselines:
            save_baselines(
                conn,
                [
                    {
                        "id": 0,
                        "metric_name": baseline.metric_name,
                        "labels": baseline.labels,
                        "avg_value": baseline.avg_value,
                        "p95_value": baseline.p95_value,
                        "min_value": baseline.min_value,
                        "max_value": baseline.max_value,
                        "sample_count": baseline.sample_count,
                        "window_days": baseline.window_days,
                        "computed_at": now,
                    }
                ],
            )

        for incident in seed.incidents:
            save_incident(
                conn,
                title=incident.title,
                description=incident.description,
                alert_name=incident.alert_name,
                root_cause=incident.root_cause,
                resolution=incident.resolution,
                severity=incident.severity,
                services=incident.services,
            )

        for report in seed.reports:
            save_report(
                conn,
                generated_at=now,
                lookback_days=report.lookback_days,
                report_markdown=report.report_markdown,
                report_data=report.report_data,
                active_alerts=report.active_alerts,
                slo_failures=report.slo_failures,
                total_log_errors=report.total_log_errors,
                estimated_cost=report.estimated_cost,
            )
    finally:
        conn.close()


def _summarize_available_data(case: EvalCase) -> str:
    """Build a concise summary of mock data available to the agent for judge context."""
    parts: list[str] = []

    for mock in case.mocks:
        body = mock.body
        if isinstance(body, str):
            parts.append(f"- {mock.method} {mock.url}: {body[:200]}")
        else:
            import json

            body_str = json.dumps(body, indent=None, default=str)
            if len(body_str) > 2000:
                body_str = body_str[:2000] + "..."
            parts.append(f"- {mock.method} {mock.url}: {body_str}")

    if case.memory_seed:
        seed = case.memory_seed
        if seed.baselines:
            for b in seed.baselines:
                parts.append(
                    f"- Baseline: {b.metric_name} labels={b.labels} "
                    f"avg={b.avg_value} p95={b.p95_value} min={b.min_value} max={b.max_value}"
                )
        if seed.incidents:
            for inc in seed.incidents:
                parts.append(
                    f"- Incident: alert={inc.alert_name} title={inc.title!r} "
                    f"root_cause={inc.root_cause!r} resolution={inc.resolution!r}"
                )
        if seed.reports:
            for rpt in seed.reports:
                preview = rpt.report_markdown[:300].replace("\n", " ")
                parts.append(f"- Archived report: {preview}...")

    return "\n".join(parts) if parts else "No mock data configured for this case."


def _extract_tool_calls(messages: list[Any]) -> list[str]:
    """Extract tool names from AIMessage.tool_calls in the message history."""
    tool_names: list[str] = []
    for msg in messages:
        if isinstance(msg, AIMessage) and msg.tool_calls:
            for tc in msg.tool_calls:
                name: str = tc["name"]
                tool_names.append(name)
    return tool_names


def _extract_answer(messages: list[Any]) -> str:
    """Extract the final text answer from the message history."""
    for msg in reversed(messages):
        if isinstance(msg, AIMessage) and isinstance(msg.content, str) and msg.content:
            return msg.content
    return "No response generated."


def _score_tools(case: EvalCase, called_tools: list[str]) -> ToolScore:
    """Score tool selection deterministically against expected_tools."""
    called_set = set(called_tools)
    must_call = set(case.expected_tools.must_call)
    must_not_call = set(case.expected_tools.must_not_call)

    missing = sorted(must_call - called_set)
    forbidden = sorted(must_not_call & called_set)

    return ToolScore(
        passed=len(missing) == 0 and len(forbidden) == 0,
        called_tools=called_tools,
        missing_tools=missing,
        forbidden_tools=forbidden,
    )


async def run_eval_case(
    case: EvalCase,
    openai_api_key: str,
    openai_model: str,
    openai_base_url: str = "",
    llm_provider: str = "openai",
    anthropic_api_key: str = "",
    anthropic_model: str = "claude-sonnet-4-20250514",
) -> EvalResult:
    """Run a single eval case: mock HTTP, invoke agent, score results.

    Args:
        case: The eval case definition.
        openai_api_key: Real OpenAI API key (from .env).
        openai_model: OpenAI model name for the agent.
        openai_base_url: Optional OpenAI-compatible proxy URL.
        llm_provider: "openai" or "anthropic".
        anthropic_api_key: Anthropic API key (when llm_provider=anthropic).
        anthropic_model: Anthropic model name (when llm_provider=anthropic).

    Returns:
        EvalResult with tool score, judge score, and agent answer.
    """
    fake_settings = _build_fake_settings(
        case,
        openai_api_key,
        openai_model,
        openai_base_url,
        llm_provider=llm_provider,
        anthropic_api_key=anthropic_api_key,
        anthropic_model=anthropic_model,
    )

    # Set up memory DB if the case provides seed data
    memory_db_fd: int | None = None
    memory_db_path: str | None = None
    if case.memory_seed is not None:
        memory_db_fd, memory_db_path = tempfile.mkstemp(suffix=".db", prefix=f"eval-memory-{case.id}-")
        os.close(memory_db_fd)
        _seed_memory_db(memory_db_path, case.memory_seed)
        fake_settings.memory_db_path = memory_db_path  # type: ignore[attr-defined]

    # Stack all settings patches
    patches = [patch(site, return_value=fake_settings) for site in _SETTINGS_PATCH_SITES]

    # Prevent runbook_search from loading — it requires a vector store on disk.
    # RAG quality is tested separately; eval focuses on tool selection + answer quality.
    # Setting the module to None in sys.modules makes `from ... import` raise
    # ImportError, which _get_tools() catches gracefully (tool never registered).
    runbook_patch = patch.dict(sys.modules, {"src.agent.retrieval.runbooks": None})

    try:
        for p in patches:
            p.start()
        runbook_patch.start()

        # Import agent builder inside the patch context so it sees fake settings
        from src.agent.agent import build_agent

        # Set up respx mocks
        with respx.mock(assert_all_called=False) as router:
            for mock_def in case.mocks:
                method_fn = getattr(router, mock_def.method.lower())
                if isinstance(mock_def.body, str):
                    response = httpx.Response(mock_def.status, text=mock_def.body)
                else:
                    response = httpx.Response(
                        mock_def.status,
                        json=mock_def.body,  # pyright: ignore[reportArgumentType]
                    )
                method_fn(url=mock_def.url).mock(return_value=response)
            # Let real LLM traffic pass through (evals use a real LLM)
            router.route(host="api.openai.com").pass_through()
            router.route(host="api.anthropic.com").pass_through()
            if openai_base_url:
                from urllib.parse import urlparse

                proxy_host = urlparse(openai_base_url).hostname
                if proxy_host:
                    router.route(host=proxy_host).pass_through()
            # Catch-all: unmocked infra routes return 503
            router.route().mock(return_value=httpx.Response(503, json={"error": "unmocked"}))

            # Suppress the "Runbook search tool unavailable" warning — we intentionally
            # disabled it via sys.modules patch, so the warning is expected noise.
            agent_logger = logging.getLogger("src.agent.agent")
            prev_level = agent_logger.level
            agent_logger.setLevel(logging.ERROR)
            agent = build_agent()
            agent_logger.setLevel(prev_level)

            from langchain_core.messages import HumanMessage

            result: dict[str, Any] = await _invoke_with_retry(
                agent,
                {"messages": [HumanMessage(content=case.question)]},
                {"configurable": {"thread_id": f"eval-{case.id}"}},
            )

        messages: list[Any] = result.get("messages", [])
        called_tools = _extract_tool_calls(messages)
        answer = _extract_answer(messages)
        tool_score = _score_tools(case, called_tools)

    finally:
        runbook_patch.stop()
        for p in reversed(patches):
            p.stop()
        # Clean up temp memory DB
        if memory_db_path is not None:
            with contextlib.suppress(OSError):
                os.unlink(memory_db_path)

    # LLM-as-judge scoring
    judge_score: JudgeScore
    available_data = _summarize_available_data(case)
    try:
        judge_model = _JUDGE_MODELS.get(llm_provider, "gpt-4o-mini")
        judge_score = await _with_progress(
            "Judge scoring",
            judge_answer(
                question=case.question,
                answer=answer,
                rubric=case.rubric,
                openai_api_key=openai_api_key,
                model=judge_model,
                base_url=openai_base_url or None,
                llm_provider=llm_provider,
                anthropic_api_key=anthropic_api_key,
                available_data=available_data,
            ),
        )
    except Exception as exc:
        logger.warning("Judge scoring failed for %s: %s", case.id, exc)
        judge_score = JudgeScore(passed=False, explanation=f"Judge error: {exc}")

    return EvalResult(
        case_id=case.id,
        description=case.description,
        tool_score=tool_score,
        judge_score=judge_score,
        agent_answer=answer,
        passed=tool_score.passed and judge_score.passed,
    )
