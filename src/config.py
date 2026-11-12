import os
import re
from functools import lru_cache
from typing import ClassVar, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# A client name as it appears in logs and metric labels (MCP_CLIENT_NAMES).
CLIENT_NAME_RE = re.compile(r"[a-z0-9][a-z0-9_.-]{0,31}")


def parse_client_names(raw: str) -> dict[str, str]:
    """Parse ``MCP_CLIENT_NAMES`` (``identity=name`` pairs, comma-separated) into lowercased identity -> name.

    Whitespace around pairs, identities and names is trimmed, and empty items (a trailing comma) are skipped. A
    malformed pair raises ``ValueError``. The message gives the pair's position only, never its text: an identity is a
    service token's Client ID or a person's email.
    """
    names: dict[str, str] = {}
    for position, item in enumerate(raw.split(","), start=1):
        pair = item.strip()
        if not pair:
            continue
        identity, sep, name = pair.rpartition("=")
        identity, name = identity.strip(), name.strip()
        if not sep or not identity:
            raise ValueError(f"MCP_CLIENT_NAMES pair {position} is not identity=name")
        if not CLIENT_NAME_RE.fullmatch(name):
            raise ValueError(f"MCP_CLIENT_NAMES pair {position}: the name must match {CLIENT_NAME_RE.pattern}")
        key = identity.lower()
        if key in names:
            raise ValueError(f"MCP_CLIENT_NAMES pair {position} repeats an identity named earlier")
        names[key] = name
    return names


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
    # How often the background task spends the OAuth refresh token when due,
    # so a dead login is caught without waiting for a question. 0 disables it.
    claude_credentials_check_interval_s: float = 900.0

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
    # The server checks the runbook index at startup and every interval (0 = startup only), and rebuilds it when it
    # is out of date with the runbooks in the image, unless auto-rebuild is off. See src/agent/retrieval/index.py.
    runbook_index_auto_rebuild: bool = True
    runbook_index_check_interval_s: float = 600.0

    # Conversation history — always writes to /app/conversations in Docker.
    # Host path is configured via bind mount in docker-compose, not here.
    conversation_history_dir: str = "/app/conversations"

    # Agent memory store (optional — empty string means disabled)
    memory_db_path: str = ""

    # Request timeout (seconds) for the /ask endpoint.  0 = no timeout.
    request_timeout_seconds: int = 120

    # Documentation MCP server (optional — empty string means not configured)
    # Provides semantic search over indexed git repos (disk-status-exporter, etc.)
    # SDK/Anthropic path only — adds as an external MCP server alongside SRE tools.
    documentation_mcp_url: str = ""

    # User's local timezone (IANA name, e.g. "Europe/Madrid", "America/Los_Angeles").
    # Used by the system prompt and the get_current_time tool so the agent can render
    # "now" in the user's local clock without having to guess the offset.
    user_timezone: str = "UTC"

    # Level for the app's own loggers (src.*): DEBUG, INFO, WARNING, ERROR or CRITICAL. Library loggers stay at
    # WARNING. src/observability/logs.py.
    log_level: str = "INFO"

    # Proxmox Backup Server API (optional — empty string means not configured)
    pbs_url: str = ""
    pbs_api_token: str = ""
    pbs_verify_ssl: bool = False
    pbs_ca_cert: str = ""
    pbs_node: str = "localhost"
    pbs_default_datastore: str = ""

    # Upgrade Advisor (optional — empty config path / DB path / credentials = disabled)
    upgrade_advisor_config_path: str = ""
    upgrade_advisor_db_path: str = ""
    upgrade_advisor_cron: str = ""
    upgrade_advisor_min_age_hours: int = 72
    upgrade_advisor_public_base_url: str = ""
    upgrade_advisor_verify_delay_seconds: int = 300
    upgrade_advisor_wud_username: str = ""
    upgrade_advisor_wud_password: str = ""
    cf_access_team_domain: str = ""
    cf_access_aud: str = ""
    # Comma-separated Cloudflare Access emails allowed to approve (case-insensitive); empty = approval disabled
    upgrade_advisor_approver_emails: str = ""
    upgrade_advisor_github_token: str = ""  # optional; empty = anonymous GitHub API

    # Incident explainer (optional — empty webhook token or DB path = POST /incidents/gatus answers 503)
    # Gatus posts every alert transition; each resolved outage gets one Pushover push explaining it.
    incident_webhook_token: str = ""  # bearer token Gatus sends in the Authorization header
    incident_db_path: str = ""  # SQLite file for incidents
    incident_model: str = "claude-sonnet-5-5"  # model that writes the verdict (no tools)
    incident_settle_seconds: float = 30.0  # wait after RESOLVED so the last logs and metrics land first
    gatus_url: str = ""  # Gatus API base URL, read for the failing results
    gatus_public_url: str = ""  # Gatus URL the push links to
    pushover_app_token: str = ""  # the explainer's own Pushover application token
    pushover_user_key: str = ""  # the Pushover user (or group) key to notify

    # Cloudflare Access check on /mcp (src/api/mcp_identity.py). The team domain is CF_ACCESS_TEAM_DOMAIN.
    # off: no check. log: verify and name the client, never reject. enforce: reject a request without a valid JWT.
    mcp_access_mode: Literal["off", "log", "enforce"] = "log"
    # Comma-separated AUD tags of the Access application(s) in front of /mcp. No fallback to CF_ACCESS_AUD.
    mcp_cf_access_aud: str = ""
    # identity=name pairs, comma-separated: a service token's Client ID or a user's email, mapped to the client name
    # used in logs and metrics. Names match [a-z0-9][a-z0-9_.-]{0,31}. Unmapped callers get a generic label.
    mcp_client_names: str = ""

    @field_validator("upgrade_advisor_public_base_url")
    @classmethod
    def _validate_advisor_base_url(cls, value: str) -> str:
        """The approve link carries a bearer token, so it must be https."""
        if value and not value.startswith("https://"):
            raise ValueError("UPGRADE_ADVISOR_PUBLIC_BASE_URL must start with https://")
        return value

    @field_validator("mcp_client_names")
    @classmethod
    def _validate_mcp_client_names(cls, value: str) -> str:
        """A malformed pair fails at load; the error never quotes the value (see ``hide_input_in_errors``)."""
        parse_client_names(value)
        return value

    @field_validator("user_timezone")
    @classmethod
    def _validate_user_timezone(cls, value: str) -> str:
        """Reject non-IANA values like 'CEST' so DST is handled automatically."""
        try:
            ZoneInfo(value)
        except ZoneInfoNotFoundError as e:
            raise ValueError(
                f"USER_TIMEZONE={value!r} is not a valid IANA timezone. "
                "Use a continent/city name like 'Europe/Amsterdam' or 'Asia/Seoul', "
                "not an abbreviation like 'CEST' or a fixed offset. See "
                "https://en.wikipedia.org/wiki/List_of_tz_database_time_zones"
            ) from e
        return value

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

    # hide_input_in_errors: a ValidationError would otherwise quote the raw input (input_value=...), which for a
    # model validator is the dict of every setting, secrets included, and for MCP_CLIENT_NAMES is the client IDs.
    model_config: ClassVar[SettingsConfigDict] = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore", hide_input_in_errors=True
    )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Lazily load and cache settings. Fails at first call, not at import time."""
    return Settings()  # type: ignore[call-arg]  # pyright: ignore[reportCallIssue] — fields loaded from env
