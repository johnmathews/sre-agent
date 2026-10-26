"""Tests for the Upgrade Advisor email rendering and the scheduler hook."""

from datetime import UTC, datetime
from typing import Any

import src.report.scheduler as sched_mod
from src.upgrades.email import render, render_abort
from src.upgrades.job import ItemResult, RunSummary
from src.upgrades.models import DeployRecord
from src.upgrades.review import Review

NOW = datetime(2026, 10, 26, 6, 0, tzinfo=UTC)


def _deploy(did: int, outcome: str, detail: str) -> DeployRecord:
    return DeployRecord.model_validate(
        {"id": did, "verdict_id": did, "started_at": NOW, "outcome": outcome, "running_digest": None, "detail": detail}
    )


def _summary() -> RunSummary:
    return RunSummary(
        items=[
            ItemResult(
                container="alpha", verdict="upgrade", review=Review(verdict="upgrade", summary="Safe <b>bump</b>")
            ),
            ItemResult(container="beta", verdict="needs_you", review=Review(verdict="needs_you", summary="Major")),
            ItemResult(container="gamma", verdict="hold", hold_reason="unreleased build"),
            ItemResult(container="delta", error="WudError"),
        ],
        approve_url="https://example.com/upgrades/batches/b1#token=T%2Bx",
        outcomes=[_deploy(1, "ok", "digest matches; warning: loki skipped <x>"), _deploy(2, "failed", "readiness")],
        outcome_containers={1: "alpha", 2: "beta"},
    )


def test_email_subject_counts_and_lists_hold_reasons() -> None:
    subject, markdown, html_body = render(_summary())
    assert subject == "Upgrades: 1 recommended, 1 need you"
    assert "gamma" in markdown
    assert "unreleased build" in markdown
    assert "WudError" in markdown
    assert "https://example.com/upgrades/batches/b1#token=T%2Bx" in markdown
    assert "unreleased build" in html_body
    assert "Held" in html_body


def test_email_escapes_html_and_flags_warnings() -> None:
    _, markdown, html_body = render(_summary())
    assert "<b>bump</b>" not in html_body
    assert "&lt;b&gt;bump&lt;/b&gt;" in html_body
    assert "&lt;x&gt;" in html_body
    assert "alpha: verified with warnings" in markdown
    assert "Warning: loki skipped <x>" in markdown
    assert "beta: failed" in markdown


def test_abort_email_names_only_the_exception_class() -> None:
    subject, markdown, html_body = render_abort("ValidationError")
    assert subject == "Upgrade advisor could not run: ValidationError"
    assert "ValidationError" in markdown
    assert "ValidationError" in html_body


async def test_scheduler_registers_advisor_job_only_when_configured(mock_settings: Any) -> None:
    mock_settings.report_schedule_cron = ""
    mock_settings.upgrade_advisor_cron = "0 6 * * *"
    sched_mod.stop_scheduler()
    sched_mod.start_scheduler()
    assert sched_mod._scheduler is None  # cron set but advisor not configured

    mock_settings.upgrade_advisor_config_path = "/etc/allow.yml"
    mock_settings.upgrade_advisor_db_path = "/data/advisor.db"
    mock_settings.upgrade_advisor_wud_username = "u"
    mock_settings.upgrade_advisor_wud_password = "p"
    mock_settings.upgrade_advisor_public_base_url = "https://example.com"
    sched_mod.start_scheduler()
    try:
        assert sched_mod._scheduler is not None
        assert sched_mod._scheduler.get_job("upgrade_advisor") is not None
        assert sched_mod._scheduler.get_job("weekly_report") is None
    finally:
        sched_mod.stop_scheduler()

    mock_settings.upgrade_advisor_cron = ""
    mock_settings.report_schedule_cron = "0 8 * * 1"
    sched_mod.start_scheduler()
    try:
        assert sched_mod._scheduler is not None
        assert sched_mod._scheduler.get_job("upgrade_advisor") is None
        assert sched_mod._scheduler.get_job("weekly_report") is not None
    finally:
        sched_mod.stop_scheduler()


def _configure_advisor(mock_settings: Any, db_path: str) -> None:
    mock_settings.upgrade_advisor_config_path = "/etc/allow.yml"
    mock_settings.upgrade_advisor_db_path = db_path
    mock_settings.upgrade_advisor_wud_username = "u"
    mock_settings.upgrade_advisor_wud_password = "p"
    mock_settings.upgrade_advisor_public_base_url = "https://example.com"


async def test_start_scheduler_seeds_last_success_from_the_store(mock_settings: Any, tmp_path: Any) -> None:
    from datetime import UTC, datetime

    from prometheus_client import REGISTRY

    from src.observability.metrics import UPGRADE_LAST_SUCCESS
    from src.upgrades import store

    db = str(tmp_path / "advisor.db")
    conn = store.connect(db)
    store.set_last_success(conn, datetime(2026, 10, 25, 7, 0, tzinfo=UTC))
    conn.close()
    UPGRADE_LAST_SUCCESS.set(0)
    mock_settings.report_schedule_cron = ""
    mock_settings.upgrade_advisor_cron = ""  # seeding does not depend on the schedule
    sched_mod.start_scheduler()  # not configured: nothing seeded
    assert REGISTRY.get_sample_value("sre_assistant_upgrade_last_success_timestamp") == 0
    _configure_advisor(mock_settings, db)
    sched_mod.start_scheduler()
    expected = datetime(2026, 10, 25, 7, 0, tzinfo=UTC).timestamp()
    assert REGISTRY.get_sample_value("sre_assistant_upgrade_last_success_timestamp") == expected


async def test_seeding_failure_is_logged_not_raised(
    mock_settings: Any, tmp_path: Any, caplog: "pytest.LogCaptureFixture"
) -> None:
    mock_settings.report_schedule_cron = ""
    mock_settings.upgrade_advisor_cron = ""
    _configure_advisor(mock_settings, str(tmp_path / "missing-dir" / "advisor.db"))
    sched_mod.start_scheduler()
    assert any(r.levelname == "ERROR" and "last successful run" in r.getMessage() for r in caplog.records)


async def test_scheduled_advisor_job_swallows_and_counts_errors() -> None:
    from unittest.mock import AsyncMock, patch

    from prometheus_client import REGISTRY

    labels = {"status": "error"}
    before = REGISTRY.get_sample_value("sre_assistant_upgrade_runs_total", labels) or 0.0
    with patch("src.upgrades.job.run_advisor", AsyncMock(side_effect=RuntimeError("boom"))):
        await sched_mod._scheduled_advisor_job()  # must not raise
    assert REGISTRY.get_sample_value("sre_assistant_upgrade_runs_total", labels) == before + 1


def test_public_base_url_must_be_https() -> None:
    import pytest
    from pydantic import ValidationError

    from src.config import Settings

    base = {
        "openai_api_key": "k",
        "user_timezone": "UTC",
        "prometheus_url": "http://prom.test",
        "grafana_url": "http://grafana.test",
        "grafana_service_account_token": "t",
    }
    with pytest.raises(ValidationError, match="must start with https://"):
        Settings(_env_file=None, upgrade_advisor_public_base_url="http://example.com", **base)  # type: ignore[call-arg]
    ok = Settings(_env_file=None, upgrade_advisor_public_base_url="https://example.com", **base)  # type: ignore[call-arg]
    assert ok.upgrade_advisor_public_base_url == "https://example.com"
    empty = Settings(_env_file=None, upgrade_advisor_public_base_url="", **base)  # type: ignore[call-arg]
    assert empty.upgrade_advisor_public_base_url == ""
