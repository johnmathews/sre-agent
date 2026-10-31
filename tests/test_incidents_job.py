"""Tests for the explanation job and startup recovery (real SQLite file; evidence, model and Pushover stubbed)."""

import asyncio
import logging
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from prometheus_client import REGISTRY

from src.api import incidents as incidents_api
from src.incidents import job, store
from src.incidents.models import Incident, Target
from src.incidents.pushover import PushoverResult
from src.incidents.verdict import VerdictError
from tests.incidents_helpers import make_bundle, make_verdict

SENT = PushoverResult(ok=True, request_id="req-1", detail="sent (request req-1)")


@pytest.fixture(autouse=True)
def _env(mock_settings: Any, tmp_path: Path) -> None:
    mock_settings.incident_db_path = str(tmp_path / "incidents.db")
    mock_settings.incident_webhook_token = "test-token"
    mock_settings.user_timezone = "Europe/Amsterdam"


def _db(mock_settings: Any) -> Any:
    return store.connect(mock_settings.incident_db_path)


def _resolved(mock_settings: Any, now: datetime | None = None, key: str = "apps_wiki") -> Incident:
    conn = _db(mock_settings)
    try:
        incident, new = store.resolve_incident(
            conn,
            endpoint_key=key,
            group="apps",
            name="Wiki",
            url="http://wiki.example.test",
            target=Target(host="apps", container="wiki"),
            now=now or datetime.now(UTC),
        )
    finally:
        conn.close()
    assert new
    return incident


def _row(mock_settings: Any, incident_id: int) -> Incident:
    conn = _db(mock_settings)
    try:
        incident = store.get_incident(conn, incident_id)
    finally:
        conn.close()
    assert incident is not None
    return incident


def _count(outcome: str) -> float:
    return REGISTRY.get_sample_value("sre_assistant_incident_explanations_total", {"outcome": outcome}) or 0.0


class Stubs:
    def __init__(self, collect: AsyncMock, judge: AsyncMock, send: AsyncMock) -> None:
        self.collect, self.judge, self.send = collect, judge, send


@pytest.fixture
def stubs() -> Iterator[Stubs]:
    collect = AsyncMock(return_value=make_bundle())
    judge = AsyncMock(return_value=make_verdict())
    send = AsyncMock(return_value=SENT)
    with (
        patch.object(job.evidence, "collect", collect),
        patch("src.incidents.verdict.judge", judge),
        patch("src.incidents.pushover.send", send),
    ):
        yield Stubs(collect, judge, send)


async def test_happy_path_sends_and_records_explained(mock_settings: Any, stubs: Stubs) -> None:
    incident = _resolved(mock_settings)
    before = _count("explained")
    await job.explain(incident.id)
    row = _row(mock_settings, incident.id)
    assert (row.state, row.outcome, row.detail) == ("explained", "explained", "sent (request req-1)")
    assert _count("explained") == before + 1
    claimed = stubs.collect.call_args.args[0]
    assert claimed.id == incident.id and claimed.state == "explaining"
    title, body, url, url_title = stubs.send.call_args.args
    assert title == "Why Wiki went down"
    assert body.startswith("Down 11:06–11:09 CET (3 min)\nCause: ")
    assert url == "https://status.example.com/endpoints/apps_wiki"
    assert url_title == "Gatus history"


async def test_waits_for_the_settle_time(mock_settings: Any, stubs: Stubs) -> None:
    mock_settings.incident_settle_seconds = 30.0
    incident = _resolved(mock_settings)
    with patch("src.incidents.job.asyncio.sleep", new=AsyncMock()) as sleep:
        await job.explain(incident.id)
    sleep.assert_awaited_once_with(30.0)


async def test_claim_false_sends_nothing(mock_settings: Any, stubs: Stubs, caplog: pytest.LogCaptureFixture) -> None:
    incident = _resolved(mock_settings)
    conn = _db(mock_settings)
    assert store.claim_for_explanation(conn, incident.id)
    conn.close()
    with caplog.at_level(logging.INFO, logger="src.incidents.job"):
        await job.explain(incident.id)
    assert stubs.collect.await_count == 0 and stubs.send.await_count == 0
    assert "already claimed" in caplog.text
    assert _row(mock_settings, incident.id).state == "explaining"


async def test_runs_once_per_incident(mock_settings: Any, stubs: Stubs) -> None:
    incident = _resolved(mock_settings)
    await asyncio.gather(job.explain(incident.id), job.explain(incident.id), job.explain(incident.id))
    assert stubs.send.await_count == 1


@pytest.mark.parametrize(
    ("exc", "reason"),
    [
        (VerdictError("no JSON object in the answer"), "no JSON object in the answer"),
        (TimeoutError(), "the model did not answer in time"),
        (RuntimeError("boom"), "model call failed (RuntimeError)"),
    ],
)
async def test_judge_failure_sends_fallback(
    mock_settings: Any, stubs: Stubs, caplog: pytest.LogCaptureFixture, exc: Exception, reason: str
) -> None:
    stubs.judge.side_effect = exc
    incident = _resolved(mock_settings)
    before = _count("fallback")
    with caplog.at_level(logging.WARNING, logger="src.incidents.job"):
        await job.explain(incident.id)
    body = stubs.send.call_args.args[1]
    assert body.split("\n")[1] == f"Summary unavailable: {reason}"
    assert "• 11:06:12 kill signal=15" in body
    row = _row(mock_settings, incident.id)
    assert (row.state, row.outcome) == ("explained", "fallback")
    assert row.detail == f"fallback ({reason}); sent (request req-1)"
    assert _count("fallback") == before + 1
    assert reason in caplog.text


async def test_pushover_failure_records_not_sent(
    mock_settings: Any, stubs: Stubs, caplog: pytest.LogCaptureFixture
) -> None:
    stubs.send.return_value = PushoverResult(ok=False, detail="Pushover not configured")
    incident = _resolved(mock_settings)
    before = _count("not_sent")
    with caplog.at_level(logging.WARNING, logger="src.incidents.job"):
        await job.explain(incident.id)
    row = _row(mock_settings, incident.id)
    assert (row.state, row.outcome, row.detail) == ("failed", "not_sent", "not sent: Pushover not configured")
    assert _count("not_sent") == before + 1
    assert "not sent: Pushover not configured" in caplog.text


async def test_unexpected_exception_records_error(
    mock_settings: Any, stubs: Stubs, caplog: pytest.LogCaptureFixture
) -> None:
    stubs.collect.side_effect = KeyError("surprise")
    incident = _resolved(mock_settings)
    before = _count("error")
    with caplog.at_level(logging.ERROR, logger="src.incidents.job"):
        await job.explain(incident.id)  # does not raise
    row = _row(mock_settings, incident.id)
    assert (row.state, row.outcome, row.detail) == ("failed", "error", "KeyError")
    assert _count("error") == before + 1
    [record] = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert record.exc_info is not None
    assert stubs.send.await_count == 0


async def test_error_while_recording_the_error_is_logged(
    mock_settings: Any, stubs: Stubs, caplog: pytest.LogCaptureFixture
) -> None:
    stubs.collect.side_effect = KeyError("surprise")
    incident = _resolved(mock_settings)
    with (
        patch("src.incidents.store.record_outcome", side_effect=RuntimeError("disk full")),
        caplog.at_level(logging.ERROR, logger="src.incidents.job"),
    ):
        await job.explain(incident.id)  # does not raise
    assert "recording the error outcome failed" in caplog.text


async def test_store_error_on_claim_is_logged_and_counted(
    mock_settings: Any, stubs: Stubs, caplog: pytest.LogCaptureFixture
) -> None:
    before = _count("error")
    with (
        patch("src.incidents.store.claim_for_explanation", side_effect=RuntimeError("locked")),
        caplog.at_level(logging.ERROR, logger="src.incidents.job"),
    ):
        await job.explain(1)
    assert "claiming failed" in caplog.text
    assert _count("error") == before + 1
    assert stubs.collect.await_count == 0


async def test_incident_missing_after_claim_is_an_error(mock_settings: Any, stubs: Stubs) -> None:
    incident = _resolved(mock_settings)
    with patch("src.incidents.store.get_incident", return_value=None):
        await job.explain(incident.id)
    row = _row(mock_settings, incident.id)
    assert (row.state, row.outcome, row.detail) == ("failed", "error", "LookupError")


# ---------------------------------------------------------------------------
# Startup recovery
# ---------------------------------------------------------------------------


async def test_recovery_marks_explaining_rows_failed(mock_settings: Any, caplog: pytest.LogCaptureFixture) -> None:
    now = datetime.now(UTC)
    incident = _resolved(mock_settings, now - timedelta(minutes=2))
    conn = _db(mock_settings)
    assert store.claim_for_explanation(conn, incident.id)
    conn.close()
    scheduled: list[int] = []
    before = _count("error")
    with caplog.at_level(logging.WARNING, logger="src.incidents.job"):
        await job.recover_unfinished(now, scheduled.append)
    row = _row(mock_settings, incident.id)
    assert (row.state, row.outcome, row.detail) == ("failed", "error", "interrupted by a restart")
    assert scheduled == []
    assert _count("error") == before + 1
    assert "interrupted by a restart" in caplog.text


async def test_recovery_schedules_recent_resolved_rows(mock_settings: Any) -> None:
    now = datetime.now(UTC)
    recent = _resolved(mock_settings, now - timedelta(minutes=29), key="apps_recent")
    scheduled: list[int] = []
    await job.recover_unfinished(now, scheduled.append)
    assert scheduled == [recent.id]
    assert _row(mock_settings, recent.id).state == "resolved"


async def test_recovery_gives_up_on_old_resolved_rows(mock_settings: Any, caplog: pytest.LogCaptureFixture) -> None:
    now = datetime.now(UTC)
    old = _resolved(mock_settings, now - timedelta(minutes=31), key="apps_old")
    scheduled: list[int] = []
    before = _count("not_sent")
    with caplog.at_level(logging.WARNING, logger="src.incidents.job"):
        await job.recover_unfinished(now, scheduled.append)
    row = _row(mock_settings, old.id)
    assert (row.state, row.outcome, row.detail) == ("failed", "not_sent", "not explained before a restart")
    assert scheduled == []
    assert _count("not_sent") == before + 1
    assert "not explained before a restart" in caplog.text


async def test_recovery_with_nothing_unfinished_does_nothing(mock_settings: Any) -> None:
    scheduled: list[int] = []
    await job.recover_unfinished(datetime.now(UTC), scheduled.append)
    assert scheduled == []


async def test_recovery_continues_after_one_row_fails(mock_settings: Any, caplog: pytest.LogCaptureFixture) -> None:
    now = datetime.now(UTC)
    first = _resolved(mock_settings, now - timedelta(minutes=5), key="apps_first")
    second = _resolved(mock_settings, now - timedelta(minutes=5), key="apps_second")
    calls: list[int] = []

    def schedule(incident_id: int) -> None:
        calls.append(incident_id)
        if incident_id == first.id:
            raise RuntimeError("no loop")

    with caplog.at_level(logging.ERROR, logger="src.incidents.job"):
        await job.recover_unfinished(now, schedule)
    assert calls == [first.id, second.id]
    assert f"recovering incident {first.id} failed" in caplog.text


async def test_recovery_store_unreadable_is_logged(mock_settings: Any, caplog: pytest.LogCaptureFixture) -> None:
    with (
        patch("src.incidents.store.list_unfinished", side_effect=RuntimeError("locked")),
        caplog.at_level(logging.ERROR, logger="src.incidents.job"),
    ):
        await job.recover_unfinished(datetime.now(UTC), lambda _id: None)
    assert "reading unfinished incidents failed" in caplog.text


async def test_start_incident_recovery_schedules_through_the_router(mock_settings: Any, stubs: Stubs) -> None:
    incident = _resolved(mock_settings, datetime.now(UTC) - timedelta(minutes=1))
    task = incidents_api.start_incident_recovery()
    await asyncio.wait_for(task, timeout=5)
    pending = list(incidents_api._explain_tasks)
    assert pending, "recovery scheduled no explanation task"
    await asyncio.wait(pending, timeout=5)
    assert stubs.send.await_count == 1
    assert _row(mock_settings, incident.id).state == "explained"


async def test_start_incident_recovery_logs_a_crash(mock_settings: Any, caplog: pytest.LogCaptureFixture) -> None:
    with (
        patch.object(incidents_api.job, "recover_unfinished", AsyncMock(side_effect=RuntimeError("bad"))),
        caplog.at_level(logging.ERROR, logger="src.api.incidents"),
    ):
        task = incidents_api.start_incident_recovery()
        await asyncio.wait([task], timeout=5)
        await asyncio.sleep(0)
    assert "incident recovery failed" in caplog.text


@pytest.mark.integration
@pytest.mark.parametrize("configured", [True, False])
def test_lifespan_starts_recovery_only_when_configured(mock_settings: Any, configured: bool) -> None:
    from unittest.mock import MagicMock

    from fastapi.testclient import TestClient

    if not configured:
        mock_settings.incident_webhook_token = ""
    with (
        patch("src.api.main.build_agent", return_value=MagicMock(name="fake_agent")),
        patch("src.api.main.start_incident_recovery") as start,
    ):
        from src.api.main import app

        with TestClient(app):
            pass
    assert start.call_count == (1 if configured else 0)
