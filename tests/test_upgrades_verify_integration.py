"""Integration tests for post-deploy verification (mocked WUD, readiness, Loki, PBS and SMTP)."""

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import httpx
import pytest
import respx

from src.upgrades import store
from src.upgrades.verify import verify_deploy, verify_pending

WUD = "http://10.0.0.15:3010"
READY = "http://10.0.0.15:9090/-/ready"
LOKI = "http://loki.test:3100"
LOKI_QUERY = f"{LOKI}/loki/api/v1/query"

ALLOWLIST = f"""\
containers:
  - name: prometheus
    host: prometheus
    wud_url: {WUD}
    registry: registry-1.docker.io
    repository: prom/prometheus
    tag: v3
    github_repo: prometheus/prometheus
    readiness_url: {READY}
    pbs_guest: CT 115
    context_files: []
"""


@pytest.fixture(autouse=True)
def _env(mock_settings: Any, tmp_path: Path) -> None:
    allow = tmp_path / "allow.yml"
    allow.write_text(ALLOWLIST)
    mock_settings.upgrade_advisor_config_path = str(allow)
    mock_settings.upgrade_advisor_db_path = str(tmp_path / "advisor.db")
    mock_settings.upgrade_advisor_wud_username = "advisor"
    mock_settings.upgrade_advisor_wud_password = "s3cret"
    mock_settings.upgrade_advisor_verify_delay_seconds = 300


@pytest.fixture
def email() -> Iterator[MagicMock]:
    with (
        patch("src.upgrades.verify.is_email_configured", return_value=True),
        patch("src.upgrades.verify.send_report_email", return_value=True) as send,
    ):
        yield send


def _seed(settings: Any, *, started: datetime | None = None, container: str = "prometheus") -> int:
    conn = store.connect(settings.upgrade_advisor_db_path)
    vid = store.save_verdict(
        conn,
        container=container,
        running_digest="sha256:old",
        candidate_digest="sha256:new",
        running_version="v3.1.0",
        candidate_version="v3.1.1",
        verdict="upgrade",
        summary="ok",
        details={},
        now=datetime.now(UTC),
    )
    did = store.save_deploy(
        conn,
        verdict_id=vid,
        now=started or datetime.now(UTC),
        outcome="pending_verify",
        running_digest=None,
        detail="",
    )
    conn.close()
    return did


def _wud(digest: str = "sha256:new") -> None:
    record = {
        "id": "abcdef123456",
        "name": "prometheus",
        "image": {"digest": {"value": digest}},
        "result": {"digest": "sha256:new"},
        "updateAvailable": False,
    }
    respx.get(f"{WUD}/api/containers").mock(return_value=httpx.Response(200, json=[record]))


def _loki_value(value: str | None) -> httpx.Response:
    result = [] if value is None else [{"metric": {}, "value": [1, value]}]
    return httpx.Response(200, json={"status": "success", "data": {"resultType": "vector", "result": result}})


def _loki(current: str, baseline: str) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        at = int(request.url.params["time"])
        now_ns = int(datetime.now(UTC).timestamp() * 1e9)
        return _loki_value(baseline if now_ns - at > 30 * 60 * 1_000_000_000 else current)

    respx.get(LOKI_QUERY).mock(side_effect=handler)


@pytest.mark.integration
@respx.mock
async def test_verify_ok_when_digest_ready_and_quiet(mock_settings: Any, email: MagicMock) -> None:
    did = _seed(mock_settings)
    _wud()
    respx.get(READY).mock(return_value=httpx.Response(200))
    _loki("2", "0")
    result = await verify_deploy(did)
    assert result.outcome == "ok"
    assert result.running_digest == "sha256:new"
    email.assert_not_called()
    query = respx.calls.last.request.url.params["query"]
    assert query == 'sum(count_over_time({container="prometheus"} |~ "(?i)(error|panic|fatal)" [5m]))'


@pytest.mark.integration
@respx.mock
async def test_digest_mismatch_emails_at_once(mock_settings: Any, email: MagicMock) -> None:
    did = _seed(mock_settings)
    _wud("sha256:old")
    result = await verify_deploy(did)
    assert result.outcome == "digest_mismatch"
    assert result.running_digest == "sha256:old"
    email.assert_called_once()
    text, html_body, subject = email.call_args.args
    assert subject == "Upgrade digest mismatch: prometheus on prometheus"
    assert "CT 115" in text
    assert "restore from the latest PBS backup" in text
    assert "<h2>" in html_body


@pytest.mark.integration
@respx.mock
async def test_readiness_failure_emails_with_pbs_guest(mock_settings: Any, email: MagicMock) -> None:
    did = _seed(mock_settings)
    _wud()
    respx.get(READY).mock(return_value=httpx.Response(503))
    respx.get(f"{mock_settings.pbs_url}/api2/json/admin/datastore/main/groups").mock(
        return_value=httpx.Response(
            200, json={"data": [{"backup-type": "ct", "backup-id": "115", "last-backup": 1_790_000_000}]}
        )
    )
    mock_settings.pbs_default_datastore = "main"
    result = await verify_deploy(did)
    assert result.outcome == "failed"
    assert "HTTP 503" in result.detail
    text, _, subject = email.call_args.args
    assert subject == "Upgrade FAILED: prometheus on prometheus"
    assert "CT 115" in text
    assert "restore from the latest PBS backup of CT 115 (taken 2026-" in text


@pytest.mark.integration
@respx.mock
async def test_pbs_error_is_a_warning_not_a_failure(mock_settings: Any, email: MagicMock) -> None:
    did = _seed(mock_settings)
    _wud()
    respx.get(READY).mock(return_value=httpx.Response(500))
    mock_settings.pbs_default_datastore = "main"
    respx.get(f"{mock_settings.pbs_url}/api2/json/admin/datastore/main/groups").mock(return_value=httpx.Response(500))
    result = await verify_deploy(did)
    assert result.outcome == "failed"
    text = email.call_args.args[0]
    assert "warning: PBS lookup failed" in text
    assert "CT 115" in text


@pytest.mark.integration
@respx.mock
async def test_loki_down_is_not_a_failure(mock_settings: Any, email: MagicMock) -> None:
    did = _seed(mock_settings)
    _wud()
    respx.get(READY).mock(return_value=httpx.Response(200))
    respx.get(LOKI_QUERY).mock(side_effect=httpx.ConnectError("down"))
    result = await verify_deploy(did)
    assert result.outcome == "ok"
    assert "warning: Loki error-log check skipped" in result.detail
    email.assert_not_called()


@pytest.mark.integration
@respx.mock
async def test_loki_unconfigured_is_a_warning(mock_settings: Any, email: MagicMock) -> None:
    mock_settings.loki_url = ""
    did = _seed(mock_settings)
    _wud()
    respx.get(READY).mock(return_value=httpx.Response(200))
    result = await verify_deploy(did)
    assert result.outcome == "ok"
    assert "Loki not configured" in result.detail


@pytest.mark.integration
@respx.mock
async def test_noisy_logs_fail_but_baseline_raises_the_limit(mock_settings: Any, email: MagicMock) -> None:
    did = _seed(mock_settings)
    _wud()
    respx.get(READY).mock(return_value=httpx.Response(200))
    _loki("31", "10")  # limit = max(10, 30) = 30
    assert (await verify_deploy(did)).outcome == "failed"
    did2 = _seed(mock_settings)
    _loki("30", "10")
    assert (await verify_deploy(did2)).outcome == "ok"
    did3 = _seed(mock_settings)
    _loki("11", "0")  # floor 10
    assert (await verify_deploy(did3)).outcome == "failed"
    email.assert_called()


@pytest.mark.integration
@respx.mock
async def test_missing_container_fails_and_emails(mock_settings: Any, email: MagicMock) -> None:
    did = _seed(mock_settings, container="gone")
    result = await verify_deploy(did)
    assert result.outcome == "failed"
    assert "can no longer be found" in result.detail
    email.assert_called_once()


@pytest.mark.integration
@respx.mock
async def test_email_html_escapes_values(mock_settings: Any, email: MagicMock) -> None:
    did = _seed(mock_settings)
    _wud("sha256:<script>alert(1)</script>")
    await verify_deploy(did)
    text, html_body, _ = email.call_args.args
    assert "<script>" in text
    assert "<script>" not in html_body
    assert "&lt;script&gt;" in html_body


@pytest.mark.integration
@respx.mock
async def test_unconfigured_email_logs_error(mock_settings: Any, caplog: pytest.LogCaptureFixture) -> None:
    did = _seed(mock_settings)
    _wud("sha256:old")
    with patch("src.upgrades.verify.is_email_configured", return_value=False):
        await verify_deploy(did)
    assert any(r.levelname == "ERROR" and "Email not configured" in r.message for r in caplog.records)


@pytest.mark.integration
@respx.mock
async def test_verify_pending_picks_up_deploys_lost_to_a_restart(mock_settings: Any, email: MagicMock) -> None:
    old = _seed(mock_settings, started=datetime.now(UTC) - timedelta(hours=2))
    fresh = _seed(mock_settings, started=datetime.now(UTC))
    _wud()
    respx.get(READY).mock(return_value=httpx.Response(200))
    _loki("0", "0")
    results = await verify_pending()
    assert [r.id for r in results] == [old]
    conn = store.connect(mock_settings.upgrade_advisor_db_path)
    assert [d.id for d in store.pending_verifications(conn)] == [fresh]
    conn.close()
