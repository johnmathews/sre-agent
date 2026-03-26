import os
from functools import lru_cache
from typing import ClassVar

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application settings loaded from environment variables / .env file."""

    # LLM provider selection: "openai" or "anthropic"
    llm_provider: str = "openai"

    openai_api_key: str = ""
    openai_model: str = "gpt-4o-mini"
    openai_base_url: str = ""  # Optional OpenAI-compatible proxy URL

    # Anthropic API (optional — only needed when LLM_PROVIDER=anthropic)
    anthropic_api_key: str = ""
    anthropic_model: str = "claude-opus-4-6"

    prometheus_url: str
    grafana_url: str
    grafana_service_account_token: str

    # Proxmox VE API (optional — empty string means not configured)
    proxmox_url: str = ""
    proxmox_api_token: str = ""
    proxmox_verify_ssl: bool = False
    proxmox_ca_cert: str = ""
    proxmox_node: str = "proxmox"

    # TrueNAS SCALE API (optional — empty string means not configured)
    truenas_url: str = ""
    truenas_api_key: str = ""
    truenas_verify_ssl: bool = False
    truenas_ca_cert: str = ""

    # Loki API (optional — empty string means not configured)
    loki_url: str = ""

    # SMTP / Email (optional — empty = email disabled)
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_username: str = ""
    smtp_password: str = ""
    report_recipient_email: str = ""

    # Report schedule (optional — empty = scheduler disabled)
    report_schedule_cron: str = ""  # e.g. "0 8 * * 1" (Monday 8am)
    report_lookback_days: int = 7

    # Extra document directories for RAG ingestion (comma-separated absolute paths)
    extra_docs_dirs: str = ""

    # Conversation history — always writes to /app/conversations in Docker.
    # Host path is configured via bind mount in docker-compose, not here.
    conversation_history_dir: str = "/app/conversations"

    # Agent memory store (optional — empty string means disabled)
    memory_db_path: str = ""

    # Request timeout (seconds) for the /ask endpoint.  0 = no timeout.
    request_timeout_seconds: int = 120

    # Proxmox Backup Server API (optional — empty string means not configured)
    pbs_url: str = ""
    pbs_api_token: str = ""
    pbs_verify_ssl: bool = False
    pbs_ca_cert: str = ""
    pbs_node: str = "localhost"
    pbs_default_datastore: str = ""

    @model_validator(mode="after")
    def _validate_provider_keys(self) -> "Settings":
        """Require the API key for the selected LLM provider."""
        if self.llm_provider == "anthropic" and not self.anthropic_api_key:
            raise ValueError("ANTHROPIC_API_KEY is required when LLM_PROVIDER=anthropic")
        if self.llm_provider == "openai" and not self.openai_api_key:
            raise ValueError("OPENAI_API_KEY is required when LLM_PROVIDER=openai")
        # docker-compose env_file turns "OPENAI_BASE_URL=" into an empty-string
        # env var.  The OpenAI Python SDK reads this and tries to use "" as a
        # base URL, causing "Request URL is missing protocol".  Remove it so
        # the SDK falls back to its default (https://api.openai.com/v1).
        if not self.openai_base_url and os.environ.get("OPENAI_BASE_URL") == "":
            del os.environ["OPENAI_BASE_URL"]
        return self

    model_config: ClassVar[SettingsConfigDict] = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Lazily load and cache settings. Fails at first call, not at import time."""
    return Settings()  # type: ignore[call-arg]  # pyright: ignore[reportCallIssue] — fields loaded from env
