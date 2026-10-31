"""LLM factory — creates the correct chat model based on LLM_PROVIDER setting — and the shared no-tools call
``ask_text`` used by the Upgrade Advisor review and the incident verdict."""

import asyncio
import logging
import time
from typing import Any

import httpx
from langchain_anthropic import ChatAnthropic
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI
from pydantic import SecretStr

from src.config import Settings, get_settings

logger = logging.getLogger(__name__)

# OAuth tokens from `claude setup-token` (Claude Max/Pro subscriptions)
# require Bearer auth plus specific beta/UA headers that identify the
# request as coming from Claude Code.
_OAUTH_TOKEN_PREFIX = "sk-ant-oat"

# Headers required by the Anthropic API for OAuth token authentication.
_OAUTH_BETA = "claude-code-20250219,oauth-2025-04-20"
_OAUTH_USER_AGENT = "claude-cli/2.1.62"

# An OAuth token is only accepted when the system prompt starts with this line.
CLAUDE_CODE_SYSTEM_PREFIX = "You are Claude Code, Anthropic's official CLI for Claude.\n\n"

# ask_text: total time for all attempts, first backoff delay (doubled per retry), and the floor on one call's timeout.
DEFAULT_RETRY_BUDGET_SECONDS = 120.0
RETRY_INITIAL_DELAY = 15.0
MIN_CALL_TIMEOUT = 10.0


def _is_oauth_token(api_key: str) -> bool:
    """Check if an API key is an Anthropic OAuth token."""
    return api_key.startswith(_OAUTH_TOKEN_PREFIX)


def create_anthropic_chat(
    api_key: str,
    model: str,
    temperature: float,
    max_tokens: int,
) -> ChatAnthropic:
    """Create a ChatAnthropic instance, handling OAuth vs regular API keys.

    OAuth tokens (sk-ant-oat*) require:
      - ``Authorization: Bearer`` instead of ``x-api-key``
      - ``anthropic-beta`` header with ``claude-code-20250219,oauth-2025-04-20``
      - ``user-agent`` matching Claude Code (``claude-cli/{version}``)
      - ``x-app: cli``

    ChatAnthropic doesn't expose the SDK's ``auth_token`` param, so we set
    the required headers via ``default_headers`` and suppress the placeholder
    ``x-api-key`` header with the SDK's ``Omit`` sentinel.
    """
    kwargs: dict[str, Any] = {
        "model": model,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    is_oauth = _is_oauth_token(api_key)
    if is_oauth:
        kwargs["api_key"] = SecretStr("placeholder-for-oauth")
        kwargs["default_headers"] = {
            "Authorization": f"Bearer {api_key}",
            "anthropic-beta": _OAUTH_BETA,
            "user-agent": _OAUTH_USER_AGENT,
            "x-app": "cli",
        }
    else:
        kwargs["api_key"] = SecretStr(api_key)

    llm = ChatAnthropic(**kwargs)  # pyright: ignore[reportCallIssue]

    if is_oauth:
        # Suppress the placeholder x-api-key header.  The SDK's
        # default_headers property merges auth_headers (X-Api-Key from
        # api_key) with _custom_headers (our default_headers).  Injecting
        # Omit() into _custom_headers makes _merge_mappings strip the
        # X-Api-Key entry so only the Authorization: Bearer header is sent.
        from anthropic._types import Omit  # pyright: ignore[reportPrivateUsage]

        omit: Any = Omit()
        for client in (llm._client, llm._async_client):
            client._custom_headers["X-Api-Key"] = omit  # type: ignore[index]  # pyright: ignore[reportPrivateUsage]

    return llm


def create_llm(
    settings: Settings,
    temperature: float = 0.0,
    model_override: str | None = None,
) -> BaseChatModel:
    """Create a chat model instance based on the configured provider.

    Args:
        settings: Application settings (provider, keys, model names).
        temperature: LLM temperature (0.0 for deterministic tool-calling).
        model_override: Override model name from settings (e.g. build_agent's model_name param).

    Returns:
        A ChatAnthropic or ChatOpenAI instance.
    """
    if settings.llm_provider == "anthropic":
        model = model_override or settings.anthropic_model
        return create_anthropic_chat(
            api_key=settings.anthropic_api_key,
            model=model,
            temperature=temperature,
            max_tokens=4096,
        )

    model = model_override or settings.openai_model
    return ChatOpenAI(
        model=model,
        temperature=temperature,
        api_key=SecretStr(settings.openai_api_key),
        base_url=settings.openai_base_url or None,
    )


def _is_retryable_llm_error(exc: Exception) -> bool:
    """Return True if *exc* is a transient LLM error worth retrying.

    Retryable errors:
      - HTTP 429 (rate limit) from Anthropic or OpenAI SDKs
      - HTTP 5xx (server-side) from Anthropic or OpenAI SDKs
      - Network / connection errors (httpx, OSError)

    Non-retryable: auth errors (401/403), validation errors (400/422), etc.
    """
    # Both anthropic and openai SDKs expose a .status_code on their API errors.
    status_code = getattr(exc, "status_code", None)
    if isinstance(status_code, int):
        return status_code == 429 or status_code >= 500

    # Network-level failures (connection refused, DNS, timeout, etc.)
    return isinstance(exc, (httpx.TransportError, OSError, ConnectionError, TimeoutError))


def response_text(content: object) -> str:
    """The text of a chat model response: a plain string, or the joined text of its content blocks."""
    if isinstance(content, list):
        return "".join(str(block.get("text", "")) if isinstance(block, dict) else str(block) for block in content)
    return str(content)


async def ask_text(
    system: str,
    prompt: str,
    *,
    model_override: str | None = None,
    budget_s: float = DEFAULT_RETRY_BUDGET_SECONDS,
) -> str:
    """One no-tools model call at temperature 0; returns the answer's text.

    With an Anthropic OAuth token the system text gets the Claude Code prefix the API requires. Rate limits, 5xx,
    network errors and timeouts are retried with doubling backoff until ``budget_s`` runs out; then, or on any other
    error, the last exception is raised. Each attempt times out after the remaining budget (at least
    ``MIN_CALL_TIMEOUT``).
    """
    settings = get_settings()
    system_text = system
    if settings.llm_provider == "anthropic" and _is_oauth_token(settings.anthropic_api_key):
        system_text = CLAUDE_CODE_SYSTEM_PREFIX + system
    messages = [SystemMessage(content=system_text), HumanMessage(content=prompt)]
    deadline = time.monotonic() + budget_s
    delay = RETRY_INITIAL_DELAY
    attempt = 0
    while True:
        remaining = deadline - time.monotonic()
        attempt += 1
        try:
            llm = create_llm(settings, temperature=0, model_override=model_override)
            response = await asyncio.wait_for(llm.ainvoke(messages), timeout=max(remaining, MIN_CALL_TIMEOUT))
            return response_text(response.content)
        except Exception as exc:
            remaining = deadline - time.monotonic()
            if (_is_retryable_llm_error(exc) or isinstance(exc, TimeoutError)) and remaining > 0:
                sleep_for = min(delay, remaining)
                logger.warning(
                    "No-tools LLM call failed (attempt %d, %s), retrying in %.0fs",
                    attempt,
                    type(exc).__name__,
                    sleep_for,
                )
                await asyncio.sleep(sleep_for)
                delay *= 2
                if deadline - time.monotonic() <= 0:
                    raise
                continue
            raise
