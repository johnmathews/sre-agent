"""Tests for LLM provider selection and OpenAI proxy support."""

import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.config import Settings
from src.observability.metrics import COST_PER_TOKEN

# ---------------------------------------------------------------------------
# Unit tests — no mocks, no IO
# ---------------------------------------------------------------------------


class TestCostTableClaudeModels:
    """Verify Claude model entries exist in the cost table."""

    def test_claude_sonnet_4_in_cost_table(self) -> None:
        assert "claude-sonnet-4" in COST_PER_TOKEN
        entry = COST_PER_TOKEN["claude-sonnet-4"]
        assert "prompt" in entry
        assert "completion" in entry

    def test_claude_opus_4_in_cost_table(self) -> None:
        assert "claude-opus-4" in COST_PER_TOKEN
        entry = COST_PER_TOKEN["claude-opus-4"]
        assert "prompt" in entry
        assert "completion" in entry

    def test_claude_haiku_4_in_cost_table(self) -> None:
        assert "claude-haiku-4" in COST_PER_TOKEN
        entry = COST_PER_TOKEN["claude-haiku-4"]
        assert "prompt" in entry
        assert "completion" in entry


class TestCallbackClaudeModelMatching:
    """Verify prefix matching finds Claude models in the cost table."""

    @pytest.mark.parametrize(
        "model_name,expected_prefix",
        [
            ("claude-sonnet-4-20250514", "claude-sonnet-4"),
            ("claude-opus-4-20250514", "claude-opus-4"),
            ("claude-haiku-4-20251001", "claude-haiku-4"),
        ],
    )
    def test_prefix_matching_finds_claude_model(self, model_name: str, expected_prefix: str) -> None:
        """The callback's prefix-matching loop should match versioned Claude model names."""
        matched_prefix: str | None = None
        for prefix in COST_PER_TOKEN:
            if model_name.startswith(prefix):
                matched_prefix = prefix
                break
        assert matched_prefix == expected_prefix


class TestBaseUrlConversion:
    """Verify the empty-string-to-None conversion pattern."""

    def test_empty_string_converts_to_none(self) -> None:
        base_url = ""
        assert (base_url or None) is None

    def test_non_empty_string_passes_through(self) -> None:
        base_url = "http://localhost:3456/v1"
        assert (base_url or None) == "http://localhost:3456/v1"


class TestSettingsValidation:
    """Tests for Settings _validate_provider_keys validator."""

    _BASE_KWARGS: dict[str, str | int | bool] = {
        "prometheus_url": "http://prom:9090",
        "grafana_url": "http://graf:3000",
        "grafana_service_account_token": "glsa_test",
    }

    def test_anthropic_provider_requires_anthropic_key(self) -> None:
        with pytest.raises(ValueError, match="ANTHROPIC_API_KEY is required"):
            Settings(
                **self._BASE_KWARGS,  # type: ignore[arg-type]
                llm_provider="anthropic",
                anthropic_api_key="",
            )

    def test_openai_provider_requires_openai_key(self) -> None:
        with pytest.raises(ValueError, match="OPENAI_API_KEY is required"):
            Settings(
                **self._BASE_KWARGS,  # type: ignore[arg-type]
                llm_provider="openai",
                openai_api_key="",
            )

    def test_anthropic_provider_does_not_require_openai_key(self) -> None:
        s = Settings(
            **self._BASE_KWARGS,  # type: ignore[arg-type]
            llm_provider="anthropic",
            anthropic_api_key="sk-ant-test",
            openai_api_key="",
        )
        assert s.llm_provider == "anthropic"
        assert s.openai_api_key == ""

    def test_openai_provider_does_not_require_anthropic_key(self) -> None:
        s = Settings(
            **self._BASE_KWARGS,  # type: ignore[arg-type]
            llm_provider="openai",
            openai_api_key="sk-test",
            anthropic_api_key="",
        )
        assert s.llm_provider == "openai"
        assert s.anthropic_api_key == ""

    def test_both_keys_can_be_set(self) -> None:
        s = Settings(
            **self._BASE_KWARGS,  # type: ignore[arg-type]
            llm_provider="openai",
            openai_api_key="sk-test",
            anthropic_api_key="sk-ant-test",
        )
        assert s.openai_api_key == "sk-test"
        assert s.anthropic_api_key == "sk-ant-test"

    def test_empty_openai_base_url_env_var_cleaned_up(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """When docker-compose sets OPENAI_BASE_URL= (empty), the validator removes it."""
        monkeypatch.setenv("OPENAI_BASE_URL", "")
        Settings(
            **self._BASE_KWARGS,  # type: ignore[arg-type]
            llm_provider="openai",
            openai_api_key="sk-test",
            openai_base_url="",
        )
        assert os.environ.get("OPENAI_BASE_URL") is None

    def test_non_empty_openai_base_url_not_removed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A real OPENAI_BASE_URL value is left in the environment."""
        monkeypatch.setenv("OPENAI_BASE_URL", "http://proxy:3456/v1")
        Settings(
            **self._BASE_KWARGS,  # type: ignore[arg-type]
            llm_provider="openai",
            openai_api_key="sk-test",
            openai_base_url="http://proxy:3456/v1",
        )
        assert os.environ.get("OPENAI_BASE_URL") == "http://proxy:3456/v1"


class TestCreateLlmFactory:
    """Unit tests for the create_llm factory function."""

    def test_returns_chat_openai_for_openai_provider(self) -> None:
        from langchain_openai import ChatOpenAI

        from src.agent.llm import create_llm

        settings = MagicMock()
        settings.llm_provider = "openai"
        settings.openai_api_key = "sk-test"
        settings.openai_model = "gpt-4o-mini"
        settings.openai_base_url = ""

        llm = create_llm(settings)
        assert isinstance(llm, ChatOpenAI)

    def test_returns_chat_anthropic_for_anthropic_provider(self) -> None:
        from langchain_anthropic import ChatAnthropic

        from src.agent.llm import create_llm

        settings = MagicMock()
        settings.llm_provider = "anthropic"
        settings.anthropic_api_key = "sk-ant-test"
        settings.anthropic_model = "claude-sonnet-4-20250514"

        llm = create_llm(settings)
        assert isinstance(llm, ChatAnthropic)

    def test_model_override_used_for_openai(self) -> None:
        from src.agent.llm import create_llm

        settings = MagicMock()
        settings.llm_provider = "openai"
        settings.openai_api_key = "sk-test"
        settings.openai_model = "gpt-4o-mini"
        settings.openai_base_url = ""

        llm = create_llm(settings, model_override="gpt-4o")
        assert llm.model_name == "gpt-4o"

    def test_oauth_token_uses_bearer_header(self) -> None:
        from anthropic._base_client import _merge_mappings

        from src.agent.llm import create_anthropic_chat

        llm = create_anthropic_chat(
            api_key="sk-ant-oat01-test-oauth-token",
            model="claude-sonnet-4-20250514",
            temperature=0.0,
            max_tokens=4096,
        )
        # Check headers on the underlying client
        merged = _merge_mappings(llm._client.default_headers, {})
        # Bearer auth
        assert merged.get("Authorization") == "Bearer sk-ant-oat01-test-oauth-token"
        # X-Api-Key should be omitted (stripped by _merge_mappings)
        assert "X-Api-Key" not in merged
        # OAuth beta and Claude Code identity headers
        assert "oauth-2025-04-20" in merged.get("anthropic-beta", "")
        assert "claude-code-20250219" in merged.get("anthropic-beta", "")
        assert merged.get("user-agent") == "claude-cli/2.1.62"
        assert merged.get("x-app") == "cli"

    def test_regular_api_key_uses_api_key_param(self) -> None:
        from src.agent.llm import create_anthropic_chat

        llm = create_anthropic_chat(
            api_key="sk-ant-api03-regular-key",
            model="claude-sonnet-4-20250514",
            temperature=0.0,
            max_tokens=4096,
        )
        assert llm._client.api_key == "sk-ant-api03-regular-key"

    def test_model_override_used_for_anthropic(self) -> None:
        from src.agent.llm import create_llm

        settings = MagicMock()
        settings.llm_provider = "anthropic"
        settings.anthropic_api_key = "sk-ant-test"
        settings.anthropic_model = "claude-sonnet-4-20250514"

        llm = create_llm(settings, model_override="claude-haiku-4-20251001")
        assert llm.model == "claude-haiku-4-20251001"


# ---------------------------------------------------------------------------
# Integration tests — mocked LLM constructors, no real LLM calls
# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestAgentBuilderBaseUrl:
    """Verify build_agent passes base_url to ChatOpenAI."""

    def test_base_url_passed_when_configured(self, mock_settings: MagicMock) -> None:
        mock_settings.openai_base_url = "http://localhost:3456/v1"
        with (
            patch("src.agent.llm.ChatOpenAI") as mock_llm_cls,
            patch("src.agent.agent.create_agent") as mock_create,
        ):
            mock_create.return_value = MagicMock()
            from src.agent.agent import build_agent

            build_agent()
            mock_llm_cls.assert_called_once()
            call_kwargs = mock_llm_cls.call_args.kwargs
            assert call_kwargs["base_url"] == "http://localhost:3456/v1"

    def test_base_url_none_when_empty(self, mock_settings: MagicMock) -> None:
        mock_settings.openai_base_url = ""
        with (
            patch("src.agent.llm.ChatOpenAI") as mock_llm_cls,
            patch("src.agent.agent.create_agent") as mock_create,
        ):
            mock_create.return_value = MagicMock()
            from src.agent.agent import build_agent

            build_agent()
            mock_llm_cls.assert_called_once()
            call_kwargs = mock_llm_cls.call_args.kwargs
            assert call_kwargs["base_url"] is None


@pytest.mark.integration
class TestAgentBuilderAnthropicProvider:
    """Verify build_agent uses SDK path when llm_provider=anthropic."""

    def test_anthropic_provider_creates_sdk_agent(self, mock_settings: MagicMock) -> None:
        mock_settings.llm_provider = "anthropic"
        mock_settings.anthropic_api_key = "sk-ant-test"
        mock_settings.anthropic_model = "claude-sonnet-4-20250514"
        with patch("src.agent.sdk_agent.build_mcp_server") as mock_mcp:
            mock_mcp.return_value = MagicMock()
            from src.agent.agent import build_agent

            agent = build_agent()
            assert agent.provider == "anthropic"
            assert agent.sdk_options is not None
            assert agent.langgraph_agent is None


@pytest.mark.integration
class TestReportGeneratorBaseUrl:
    """Verify _generate_narrative passes base_url to ChatOpenAI."""

    @pytest.mark.asyncio
    async def test_base_url_passed_when_configured(self, mock_settings: MagicMock) -> None:
        mock_settings.openai_base_url = "http://localhost:3456/v1"
        with patch("src.agent.llm.ChatOpenAI") as mock_llm_cls:
            mock_llm = MagicMock()
            mock_llm.ainvoke = AsyncMock(return_value=MagicMock(content="test narrative"))
            mock_llm_cls.return_value = mock_llm

            from src.report.generator import _generate_narrative

            await _generate_narrative({"alerts": None})
            mock_llm_cls.assert_called_once()
            call_kwargs = mock_llm_cls.call_args.kwargs
            assert call_kwargs["base_url"] == "http://localhost:3456/v1"

    @pytest.mark.asyncio
    async def test_base_url_none_when_empty(self, mock_settings: MagicMock) -> None:
        mock_settings.openai_base_url = ""
        with patch("src.agent.llm.ChatOpenAI") as mock_llm_cls:
            mock_llm = MagicMock()
            mock_llm.ainvoke = AsyncMock(return_value=MagicMock(content="test narrative"))
            mock_llm_cls.return_value = mock_llm

            from src.report.generator import _generate_narrative

            await _generate_narrative({"alerts": None})
            mock_llm_cls.assert_called_once()
            call_kwargs = mock_llm_cls.call_args.kwargs
            assert call_kwargs["base_url"] is None


@pytest.mark.integration
class TestReportGeneratorAnthropicProvider:
    """Verify _generate_narrative uses ChatAnthropic when llm_provider=anthropic."""

    @pytest.mark.asyncio
    async def test_anthropic_provider_creates_chat_anthropic(self, mock_settings: MagicMock) -> None:
        mock_settings.llm_provider = "anthropic"
        mock_settings.anthropic_api_key = "sk-ant-test"
        mock_settings.anthropic_model = "claude-sonnet-4-20250514"
        with patch("src.agent.llm.ChatAnthropic") as mock_llm_cls:
            mock_llm = MagicMock()
            mock_llm.ainvoke = AsyncMock(return_value=MagicMock(content="test narrative"))
            mock_llm_cls.return_value = mock_llm

            from src.report.generator import _generate_narrative

            await _generate_narrative({"alerts": None})
            mock_llm_cls.assert_called_once()


@pytest.mark.integration
class TestInvokeAgentModelName:
    """Verify invoke_agent passes the correct model name to save_conversation."""

    @pytest.mark.asyncio
    async def test_openai_model_saved_in_conversation_history(self, mock_settings: MagicMock) -> None:
        mock_settings.llm_provider = "openai"
        mock_settings.openai_model = "gpt-4o-mini"
        mock_settings.conversation_history_dir = "/tmp/test-convos"

        from langchain_core.messages import AIMessage

        fake_result = {"messages": [AIMessage(content="Test response")]}
        fake_agent = MagicMock()
        fake_agent.ainvoke = AsyncMock(return_value=fake_result)

        with patch("src.agent.agent.save_conversation") as mock_save:
            from src.agent.agent import _invoke_langgraph_agent

            await _invoke_langgraph_agent(fake_agent, "hello", session_id="test-session")
            mock_save.assert_called_once()
            _, _, _, model_arg = mock_save.call_args.args
            assert model_arg == "gpt-4o-mini"


@pytest.mark.integration
class TestJudgeBaseUrl:
    """Verify judge_answer passes base_url to ChatOpenAI."""

    @pytest.mark.asyncio
    async def test_base_url_passed_when_provided(self) -> None:
        with patch("src.eval.judge.ChatOpenAI") as mock_llm_cls:
            mock_llm = MagicMock()
            mock_llm.ainvoke = AsyncMock(return_value=MagicMock(content='{"passed": true, "explanation": "ok"}'))
            mock_llm_cls.return_value = mock_llm

            from src.eval.judge import judge_answer

            await judge_answer(
                question="test?",
                answer="test answer",
                rubric="test rubric",
                openai_api_key="sk-test",
                base_url="http://localhost:3456/v1",
            )
            mock_llm_cls.assert_called_once()
            call_kwargs = mock_llm_cls.call_args.kwargs
            assert call_kwargs["base_url"] == "http://localhost:3456/v1"

    @pytest.mark.asyncio
    async def test_base_url_none_by_default(self) -> None:
        with patch("src.eval.judge.ChatOpenAI") as mock_llm_cls:
            mock_llm = MagicMock()
            mock_llm.ainvoke = AsyncMock(return_value=MagicMock(content='{"passed": true, "explanation": "ok"}'))
            mock_llm_cls.return_value = mock_llm

            from src.eval.judge import judge_answer

            await judge_answer(
                question="test?",
                answer="test answer",
                rubric="test rubric",
                openai_api_key="sk-test",
            )
            mock_llm_cls.assert_called_once()
            call_kwargs = mock_llm_cls.call_args.kwargs
            assert call_kwargs["base_url"] is None


@pytest.mark.integration
class TestJudgeAnthropicProvider:
    """Verify judge_answer uses ChatAnthropic when llm_provider=anthropic."""

    @pytest.mark.asyncio
    async def test_anthropic_provider_creates_chat_anthropic(self) -> None:
        with patch("src.eval.judge.create_anthropic_chat") as mock_create:
            mock_llm = MagicMock()
            mock_llm.ainvoke = AsyncMock(return_value=MagicMock(content='{"passed": true, "explanation": "ok"}'))
            mock_create.return_value = mock_llm

            from src.eval.judge import judge_answer

            await judge_answer(
                question="test?",
                answer="test answer",
                rubric="test rubric",
                llm_provider="anthropic",
                anthropic_api_key="sk-ant-test",
                model="claude-sonnet-4-20250514",
            )
            mock_create.assert_called_once()
            call_kwargs = mock_create.call_args.kwargs
            assert call_kwargs["max_tokens"] == 1024


@pytest.mark.integration
class TestEvalRunnerBaseUrl:
    """Verify _build_fake_settings includes openai_base_url."""

    def test_base_url_set_when_provided(self) -> None:
        from src.eval.models import EvalCase, ExpectedTools, MockResponse

        case = EvalCase(
            id="test",
            description="test case",
            question="test?",
            expected_tools=ExpectedTools(must_call=[], must_not_call=[]),
            rubric="test",
            mocks=[MockResponse(method="GET", url="http://test.test", status=200, body="{}")],
        )
        from src.eval.runner import _build_fake_settings

        settings = _build_fake_settings(case, "sk-test", "gpt-4o-mini", "http://localhost:3456/v1")
        assert settings.openai_base_url == "http://localhost:3456/v1"  # type: ignore[union-attr]

    def test_base_url_empty_by_default(self) -> None:
        from src.eval.models import EvalCase, ExpectedTools, MockResponse

        case = EvalCase(
            id="test",
            description="test case",
            question="test?",
            expected_tools=ExpectedTools(must_call=[], must_not_call=[]),
            rubric="test",
            mocks=[MockResponse(method="GET", url="http://test.test", status=200, body="{}")],
        )
        from src.eval.runner import _build_fake_settings

        settings = _build_fake_settings(case, "sk-test", "gpt-4o-mini")
        assert settings.openai_base_url == ""  # type: ignore[union-attr]


@pytest.mark.integration
class TestEvalRunnerProviderConfig:
    """Verify _build_fake_settings includes provider config."""

    def test_anthropic_provider_config(self) -> None:
        from src.eval.models import EvalCase, ExpectedTools, MockResponse

        case = EvalCase(
            id="test",
            description="test case",
            question="test?",
            expected_tools=ExpectedTools(must_call=[], must_not_call=[]),
            rubric="test",
            mocks=[MockResponse(method="GET", url="http://test.test", status=200, body="{}")],
        )
        from src.eval.runner import _build_fake_settings

        settings = _build_fake_settings(
            case,
            "sk-test",
            "gpt-4o-mini",
            llm_provider="anthropic",
            anthropic_api_key="sk-ant-test",
            anthropic_model="claude-sonnet-4-20250514",
        )
        assert settings.llm_provider == "anthropic"  # type: ignore[union-attr]
        assert settings.anthropic_api_key == "sk-ant-test"  # type: ignore[union-attr]
        assert settings.anthropic_model == "claude-sonnet-4-20250514"  # type: ignore[union-attr]
