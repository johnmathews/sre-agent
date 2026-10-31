"""Integration tests for the Gatus incident webhook (ASGI transport, real SQLite file, ``explain`` recorded)."""

import asyncio
import logging
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any
from unittest.mock import patch

import httpx
import pytest
from fastapi import FastAPI
from prometheus_client import REGISTRY

from src.api import incidents as incidents_api
from src.api.incidents import router
from src.incidents import store

TOKEN = "test-incident-token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}
URL = "/incidents/gatus"


def _body(event: str, name: str = "Dozzle", description: str = "host=infra;container=dozzle") -> dict[str, str]:
    return {
        "event": event,
        "group": "monitoring",
        "name": name,
        "url": "http://dozzle.example.test",
        "description": description,
    }


class Recorder:
    """Stands in for ``job.explain``: records each incident id it is called with."""

    def __init__(self) -> None:
        self.calls: list[int] = []
        self.fail = False

    async def __call__(self, incident_id: int) -> None:
        self.calls.append(incident_id)
        if self.fail:
            raise RuntimeError("explanation blew up")


@pytest.fixture(autouse=True)
def _env(mock_settings: Any, tmp_path: Path) -> None:
    mock_settings.incident_webhook_token = TOKEN
    mock_settings.incident_db_path = str(tmp_path / "incidents.db")


@pytest.fixture
def recorder() -> Iterator[Recorder]:
    rec = Recorder()
    with patch.object(incidents_api.job, "explain", rec):
        yield rec


@pytest.fixture
async def client() -> AsyncIterator[httpx.AsyncClient]:
    app = FastAPI()
    app.include_router(router)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as c:
        yield c


async def _drain() -> None:
    """Let every scheduled explanation task run to completion."""
    pending = list(incidents_api._explain_tasks)
    if pending:
        await asyncio.wait(pending, timeout=5)
    await asyncio.sleep(0)  # let done-callbacks run


def _count(event: str, outcome: str) -> float:
    return REGISTRY.get_sample_value("sre_assistant_incident_events_total", {"event": event, "outcome": outcome}) or 0.0


@pytest.mark.integration
async def test_missing_token_is_401(client: httpx.AsyncClient, recorder: Recorder) -> None:
    before = _count("unknown", "unauthorized")
    resp = await client.post(URL, json=_body("TRIGGERED"))
    assert resp.status_code == 401
    assert resp.json() == {"detail": "unauthorized"}
    assert _count("unknown", "unauthorized") == before + 1


@pytest.mark.integration
@pytest.mark.parametrize(
    "header",
    [
        "Bearer wrong-token",
        f"Basic {TOKEN}",
        TOKEN,
        "Bearer ",
        f"Bearer {TOKEN}x",
        "Bearer t\u00f6ken".encode(),  # non-ASCII must be a clean 401, not a 500 from compare_digest
    ],
)
async def test_wrong_token_is_401(client: httpx.AsyncClient, recorder: Recorder, header: str | bytes) -> None:
    resp = await client.post(URL, json=_body("RESOLVED"), headers={"Authorization": header})
    assert resp.status_code == 401
    assert resp.json() == {"detail": "unauthorized"}
    assert recorder.calls == []


@pytest.mark.integration
async def test_bearer_scheme_is_case_insensitive(client: httpx.AsyncClient, recorder: Recorder) -> None:
    resp = await client.post(URL, json=_body("TRIGGERED"), headers={"Authorization": f"bearer {TOKEN}"})
    assert resp.status_code == 202


@pytest.mark.integration
@pytest.mark.parametrize("missing", ["incident_webhook_token", "incident_db_path"])
async def test_unconfigured_is_503_even_without_a_token(
    client: httpx.AsyncClient, recorder: Recorder, mock_settings: Any, missing: str
) -> None:
    setattr(mock_settings, missing, "")
    before = _count("unknown", "not_configured")
    resp = await client.post(URL, json=_body("TRIGGERED"))
    assert resp.status_code == 503
    assert resp.json() == {"detail": "incident explainer not configured"}
    assert _count("unknown", "not_configured") == before + 1


@pytest.mark.integration
@pytest.mark.parametrize(
    "body",
    [
        {"event": "FLAPPING", "group": "g", "name": "n"},
        {"event": "TRIGGERED", "group": "g"},
        {"event": "TRIGGERED", "group": "g", "name": "n" * 513},
        {"event": "TRIGGERED", "group": "g", "name": "n", "description": "d" * 513},
        {"event": "TRIGGERED", "group": "g", "name": 5},
    ],
)
async def test_bad_body_is_422(client: httpx.AsyncClient, recorder: Recorder, body: dict[str, Any]) -> None:
    before = _count("unknown", "invalid")
    resp = await client.post(URL, json=body, headers=AUTH)
    assert resp.status_code == 422
    assert _count("unknown", "invalid") == before + 1


@pytest.mark.integration
async def test_non_json_body_is_422(client: httpx.AsyncClient, recorder: Recorder) -> None:
    resp = await client.post(URL, content=b"[RESULT_ERRORS] not json", headers=AUTH)
    assert resp.status_code == 422


@pytest.mark.integration
async def test_oversized_body_is_413(client: httpx.AsyncClient, recorder: Recorder) -> None:
    resp = await client.post(URL, content=b"{" + b" " * 70_000 + b"}", headers=AUTH)
    assert resp.status_code == 413


@pytest.mark.integration
async def test_triggered_then_resolved_schedules_one_explanation(
    client: httpx.AsyncClient, recorder: Recorder, mock_settings: Any
) -> None:
    accepted_before = _count("resolved", "accepted")
    trig = await client.post(URL, json=_body("TRIGGERED"), headers=AUTH)
    assert trig.status_code == 202
    incident_id = trig.json()["incident_id"]
    assert trig.json() == {"incident_id": incident_id, "state": "open"}

    again = await client.post(URL, json=_body("TRIGGERED"), headers=AUTH)
    assert again.status_code == 202
    assert again.json()["incident_id"] == incident_id

    res = await client.post(URL, json=_body("RESOLVED"), headers=AUTH)
    assert res.status_code == 202
    assert res.json() == {"incident_id": incident_id, "state": "resolved"}
    await _drain()
    assert recorder.calls == [incident_id]
    assert _count("resolved", "accepted") == accepted_before + 1

    conn = store.connect(mock_settings.incident_db_path)
    try:
        incident = store.get_incident(conn, incident_id)
    finally:
        conn.close()
    assert incident is not None
    assert (incident.host, incident.container) == ("infra", "dozzle")
    assert incident.endpoint_key == "monitoring_dozzle"


@pytest.mark.integration
async def test_duplicate_resolved_schedules_none(client: httpx.AsyncClient, recorder: Recorder) -> None:
    await client.post(URL, json=_body("TRIGGERED"), headers=AUTH)
    first = await client.post(URL, json=_body("RESOLVED"), headers=AUTH)
    dup_before = _count("resolved", "duplicate")
    dup = await client.post(URL, json=_body("RESOLVED"), headers=AUTH)
    assert dup.status_code == 202
    assert dup.json()["incident_id"] == first.json()["incident_id"]
    await _drain()
    assert recorder.calls == [first.json()["incident_id"]]
    assert _count("resolved", "duplicate") == dup_before + 1


@pytest.mark.integration
async def test_resolved_without_triggered_schedules_one(client: httpx.AsyncClient, recorder: Recorder) -> None:
    res = await client.post(URL, json=_body("RESOLVED", description=""), headers=AUTH)
    assert res.status_code == 202
    assert res.json()["state"] == "resolved"
    await _drain()
    assert recorder.calls == [res.json()["incident_id"]]


@pytest.mark.integration
async def test_explanation_failure_is_logged_not_lost(
    client: httpx.AsyncClient, recorder: Recorder, caplog: pytest.LogCaptureFixture
) -> None:
    recorder.fail = True
    caplog.set_level(logging.ERROR, logger="src.api.incidents")
    res = await client.post(URL, json=_body("RESOLVED"), headers=AUTH)
    assert res.status_code == 202
    await _drain()
    [record] = [r for r in caplog.records if "explanation" in r.getMessage()]
    assert str(res.json()["incident_id"]) in record.getMessage()
    assert record.exc_info is not None
    assert incidents_api._explain_tasks == set()


@pytest.mark.integration
async def test_store_failure_is_500_and_counted(
    client: httpx.AsyncClient, recorder: Recorder, mock_settings: Any, tmp_path: Path
) -> None:
    mock_settings.incident_db_path = str(tmp_path / "missing-dir" / "incidents.db")
    before = _count("triggered", "error")
    resp = await client.post(URL, json=_body("TRIGGERED"), headers=AUTH)
    assert resp.status_code == 500
    assert resp.json() == {"detail": "incident store error"}
    assert _count("triggered", "error") == before + 1


@pytest.mark.integration
def test_startup_log_names_missing_settings(mock_settings: Any, caplog: pytest.LogCaptureFixture) -> None:
    mock_settings.incident_webhook_token = ""
    mock_settings.incident_db_path = ""
    caplog.set_level(logging.INFO, logger="src.api.incidents")
    incidents_api.log_incident_explainer_status()
    [record] = caplog.records
    assert record.levelno == logging.WARNING
    assert "INCIDENT_WEBHOOK_TOKEN" in record.getMessage()
    assert "INCIDENT_DB_PATH" in record.getMessage()
    assert TOKEN not in record.getMessage()


@pytest.mark.integration
def test_startup_log_warns_about_missing_delivery_settings(
    mock_settings: Any, caplog: pytest.LogCaptureFixture
) -> None:
    mock_settings.gatus_url = ""
    mock_settings.pushover_app_token = ""
    mock_settings.pushover_user_key = "u-fake"
    caplog.set_level(logging.INFO, logger="src.api.incidents")
    incidents_api.log_incident_explainer_status()
    [record] = caplog.records
    assert record.levelno == logging.WARNING
    assert "GATUS_URL" in record.getMessage()
    assert "PUSHOVER_APP_TOKEN" in record.getMessage()
    assert "PUSHOVER_USER_KEY" not in record.getMessage()


@pytest.mark.integration
def test_startup_log_reports_enabled(mock_settings: Any, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger="src.api.incidents")
    incidents_api.log_incident_explainer_status()
    [record] = caplog.records
    assert record.levelno == logging.INFO
    assert "incident explainer enabled" in record.getMessage()
    assert mock_settings.incident_model in record.getMessage()
