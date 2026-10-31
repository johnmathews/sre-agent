"""Integration tests for post-deploy verification (mocked WUD, readiness, Loki, PBS and SMTP)."""

import asyncio
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
import respx
from prometheus_client import REGISTRY

from src.upgrades import store
from src.upgrades.models import DeployOutcome, TriggerState
from src.upgrades.verify import INTERRUPTED_IN_FLIGHT, REAP_GRACE_SECONDS, verify_deploy, verify_pending
from src.upgrades.wud import TRIGGER_TIMEOUT_SECONDS

OLD = "sha256:" + "a" * 64
NEW = "sha256:" + "b" * 64
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
    pbs_guest: CT 901
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


def _seed(
    settings: Any,
    *,
    version: str = "v3.1.1",
    started: datetime | None = None,
    container: str = "prometheus",
    detail: str = "",
    running_digest: str | None = None,
    outcome: DeployOutcome = "pending_verify",
    trigger_state: TriggerState | None = None,
    settled: datetime | None = None,
) -> int:
    conn = store.connect(settings.upgrade_advisor_db_path)
    vid = store.save_verdict(
        conn,
        container=container,
        running_digest=OLD,
        candidate_digest=NEW,
        running_version="v3.1.0",
        candidate_version=version,
        verdict="upgrade",
        summary="ok",
        details={},
        now=datetime.now(UTC),
    )
    did = store.save_deploy(
        conn,
        verdict_id=vid,
        now=started or datetime.now(UTC),
        outcome="triggering" if settled else outcome,
        running_digest=running_digest,
        detail=detail,
        trigger_state="triggering" if settled else trigger_state,
    )
    if settled:  # a trigger that ended at ``settled``, the way deploy._trigger records it
        assert store.settle_trigger(conn, did, trigger_state or "triggered", detail, now=settled)
    conn.close()
    return did


def _wud(digest: object = NEW) -> None:
    record = {
        "id": "abcdef123456",
        "name": "prometheus",
        "image": {"digest": {"value": digest}},
        "result": {"digest": NEW},
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
    assert result.running_digest == NEW
    email.assert_not_called()
    query = respx.calls.last.request.url.params["query"]
    assert query == 'sum(count_over_time({container="prometheus"} |~ "(?i)(error|panic|fatal)" [5m]))'


@pytest.mark.integration
@respx.mock
async def test_digest_mismatch_emails_at_once(mock_settings: Any, email: MagicMock) -> None:
    did = _seed(mock_settings)
    _wud(OLD)
    result = await verify_deploy(did)
    assert result.outcome == "digest_mismatch"
    assert result.running_digest == OLD
    email.assert_called_once()
    text, html_body, subject = email.call_args.args
    assert subject == "Upgrade digest mismatch: prometheus on prometheus"
    assert "CT 901" in text
    assert "restore from the latest PBS backup" in text
    assert "<h2>" in html_body


@pytest.mark.integration
@respx.mock
async def test_interrupted_deploy_still_on_the_old_image_is_not_deployed_without_restore_advice(
    mock_settings: Any, email: MagicMock
) -> None:
    did = _seed(mock_settings, detail=INTERRUPTED_IN_FLIGHT, running_digest=OLD, trigger_state="interrupted")
    _wud(OLD)
    with patch("src.upgrades.verify.pbs_latest_backup_time", new_callable=AsyncMock) as pbs:
        result = await verify_deploy(did)
    assert result.outcome == "failed"
    assert result.detail == "not deployed (interrupted by shutdown); approve again from the next email"
    assert result.running_digest == OLD
    pbs.assert_not_awaited()
    text, html_body, subject = email.call_args.args
    assert subject == "Upgrade not deployed: prometheus on prometheus"
    assert "approve the update again from the next advisor email" in text
    assert "PBS" not in text and "restore" not in text and "restore" not in html_body


@pytest.mark.integration
@respx.mock
async def test_interrupted_deploy_on_an_unexpected_image_is_a_digest_mismatch(
    mock_settings: Any, email: MagicMock
) -> None:
    other = "sha256:" + "c" * 64
    did = _seed(mock_settings, detail=INTERRUPTED_IN_FLIGHT, running_digest=OLD, trigger_state="interrupted")
    _wud(other)
    result = await verify_deploy(did)
    assert result.outcome == "digest_mismatch"
    text, _, subject = email.call_args.args
    assert subject == "Upgrade digest mismatch: prometheus on prometheus"
    assert "restore from the latest PBS backup" in text


@pytest.mark.integration
@respx.mock
async def test_interrupted_deploy_that_completed_verifies_normally(mock_settings: Any, email: MagicMock) -> None:
    did = _seed(mock_settings, detail=INTERRUPTED_IN_FLIGHT, running_digest=OLD, trigger_state="interrupted")
    _wud(NEW)
    respx.get(READY).mock(return_value=httpx.Response(200))
    _loki("0", "0")
    result = await verify_deploy(did)
    assert result.outcome == "ok"
    email.assert_not_called()


@pytest.mark.integration
@respx.mock
async def test_readiness_failure_emails_with_pbs_guest(mock_settings: Any, email: MagicMock) -> None:
    did = _seed(mock_settings)
    _wud()
    respx.get(READY).mock(return_value=httpx.Response(503))
    respx.get(f"{mock_settings.pbs_url}/api2/json/admin/datastore/main/groups").mock(
        return_value=httpx.Response(
            200, json={"data": [{"backup-type": "ct", "backup-id": "901", "last-backup": 1_790_000_000}]}
        )
    )
    mock_settings.pbs_default_datastore = "main"
    result = await verify_deploy(did)
    assert result.outcome == "failed"
    assert "HTTP 503" in result.detail
    text, _, subject = email.call_args.args
    assert subject == "Upgrade FAILED: prometheus on prometheus"
    assert "CT 901" in text
    assert "restore from the latest PBS backup of CT 901 (taken 2026-" in text


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
    assert "CT 901" in text


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
    # The email splits detail on "; ", so a warning must not contain it or its second half is dropped.
    assert "warning: Loki not configured, error-log check skipped" in result.detail.split("; ")


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
    assert "no longer in the allowlist" in result.detail
    email.assert_called_once()


@pytest.mark.integration
@respx.mock
async def test_email_html_escapes_values(mock_settings: Any, email: MagicMock) -> None:
    did = _seed(mock_settings, version="<script>alert(1)</script>")
    _wud(OLD)
    await verify_deploy(did)
    text, html_body, _ = email.call_args.args
    assert "<script>" in text
    assert "<script>" not in html_body
    assert "&lt;script&gt;" in html_body


@pytest.mark.integration
@respx.mock
@pytest.mark.parametrize("bad", [None, "", "sha256:short", "sha256:<script>alert(1)</script>"])
async def test_invalid_wud_digest_fails_without_echoing_it(mock_settings: Any, email: MagicMock, bad: object) -> None:
    did = _seed(mock_settings)
    _wud(bad)
    result = await verify_deploy(did)
    assert result.outcome == "failed"
    assert result.detail == "WUD reported no valid running digest"
    assert result.running_digest is None
    assert "script" not in email.call_args.args[0]


@pytest.mark.integration
@respx.mock
async def test_exception_text_never_reaches_detail_or_email(mock_settings: Any, email: MagicMock) -> None:
    did = _seed(mock_settings)
    _wud()
    respx.get(READY).mock(side_effect=httpx.ConnectError("secret-host-detail"))
    mock_settings.pbs_default_datastore = "main"
    respx.get(f"{mock_settings.pbs_url}/api2/json/admin/datastore/main/groups").mock(
        side_effect=httpx.ConnectError("secret-pbs-detail")
    )
    result = await verify_deploy(did)
    assert result.outcome == "failed"
    assert "ConnectError" in result.detail
    assert "secret" not in result.detail
    assert "secret" not in email.call_args.args[0] + email.call_args.args[1]


@pytest.mark.integration
@respx.mock
async def test_unconfigured_email_logs_error(mock_settings: Any, caplog: pytest.LogCaptureFixture) -> None:
    did = _seed(mock_settings)
    _wud(OLD)
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


@pytest.mark.integration
@respx.mock
async def test_concurrent_verifications_email_and_count_once(mock_settings: Any, email: MagicMock) -> None:
    did = _seed(mock_settings)
    _wud(OLD)
    labels = {"outcome": "digest_mismatch"}
    before = REGISTRY.get_sample_value("sre_assistant_upgrade_deploys_total", labels) or 0.0
    first, second = await asyncio.gather(verify_deploy(did), verify_deploy(did))
    assert first.outcome == second.outcome == "digest_mismatch"
    assert email.call_count == 1
    after = REGISTRY.get_sample_value("sre_assistant_upgrade_deploys_total", labels) or 0.0
    assert after - before == 1


@pytest.mark.integration
@respx.mock
async def test_loki_baseline_failure_is_named_in_the_warning(mock_settings: Any, email: MagicMock) -> None:
    did = _seed(mock_settings)
    _wud()
    respx.get(READY).mock(return_value=httpx.Response(200))

    def handler(request: httpx.Request) -> httpx.Response:
        at = int(request.url.params["time"])
        if int(datetime.now(UTC).timestamp() * 1e9) - at > 30 * 60 * 1_000_000_000:
            return httpx.Response(500)
        return _loki_value("0")

    respx.get(LOKI_QUERY).mock(side_effect=handler)
    result = await verify_deploy(did)
    assert result.outcome == "ok"
    assert "Loki baseline query failed" in result.detail


@pytest.mark.integration
@respx.mock
async def test_unreadable_allowlist_is_distinguished(mock_settings: Any, email: MagicMock) -> None:
    did = _seed(mock_settings)
    Path(mock_settings.upgrade_advisor_config_path).write_text("not: [valid")
    result = await verify_deploy(did)
    assert result.outcome == "failed"
    assert "allowlist unreadable" in result.detail
    email.assert_called_once()


@pytest.mark.integration
@respx.mock
async def test_the_interrupted_decision_is_structured_not_the_detail_text(mock_settings: Any, email: MagicMock) -> None:
    did = _seed(mock_settings, detail=INTERRUPTED_IN_FLIGHT, running_digest=OLD, trigger_state="triggered")
    _wud(OLD)
    result = await verify_deploy(did)
    assert result.outcome == "digest_mismatch"  # a completed trigger still on the old image is a mismatch


@pytest.mark.integration
@respx.mock
async def test_trigger_error_still_on_the_old_image_is_not_deployed(mock_settings: Any, email: MagicMock) -> None:
    did = _seed(mock_settings, detail="WUD trigger failed: WudError", running_digest=OLD, trigger_state="trigger_error")
    _wud(OLD)
    with patch("src.upgrades.verify.pbs_latest_backup_time", new_callable=AsyncMock) as pbs:
        result = await verify_deploy(did)
    assert (result.outcome, result.detail) == ("failed", "not deployed (WUD trigger failed: WudError)")
    pbs.assert_not_awaited()
    text, html_body, subject = email.call_args.args
    assert subject == "Upgrade not deployed: prometheus on prometheus"
    assert "approve the update again from the next advisor email" in text
    assert "restore" not in text and "PBS" not in text and "restore" not in html_body


@pytest.mark.integration
@respx.mock
async def test_trigger_error_but_new_image_running_verifies_normally(mock_settings: Any, email: MagicMock) -> None:
    did = _seed(mock_settings, detail="WUD trigger failed: WudError", running_digest=OLD, trigger_state="trigger_error")
    _wud(NEW)
    respx.get(READY).mock(return_value=httpx.Response(200))
    _loki("0", "0")
    result = await verify_deploy(did)
    assert result.outcome == "ok"
    email.assert_not_called()


@pytest.mark.integration
@respx.mock
async def test_verify_pending_settles_a_trigger_left_by_a_hard_kill(mock_settings: Any, email: MagicMock) -> None:
    # reaped (past the trigger timeout plus grace) and due (its settle is past the 300 s verify delay)
    long_ago = datetime.now(UTC) - timedelta(seconds=TRIGGER_TIMEOUT_SECONDS + 300 + 60)
    killed = _seed(
        mock_settings,
        started=long_ago,
        outcome="triggering",
        detail="deploy triggering",
        running_digest=OLD,
        trigger_state="triggering",
    )
    in_flight = _seed(
        mock_settings,
        started=datetime.now(UTC)
        - timedelta(seconds=TRIGGER_TIMEOUT_SECONDS + REAP_GRACE_SECONDS - 30),  # inside the margin
        outcome="triggering",
        detail="deploy triggering",
        running_digest=OLD,
        trigger_state="triggering",
    )
    _wud(OLD)  # the kill landed before WUD switched the image
    results = await verify_pending()
    assert [(r.id, r.outcome, r.trigger_state, r.detail) for r in results] == [
        (killed, "failed", "interrupted", "not deployed (interrupted by shutdown); approve again from the next email")
    ]
    conn = store.connect(mock_settings.upgrade_advisor_db_path)
    try:
        assert [d.id for d in store.triggering_deploys(conn)] == [in_flight]  # within the trigger timeout plus grace
    finally:
        conn.close()
    assert email.call_args.args[2] == "Upgrade not deployed: prometheus on prometheus"


@pytest.mark.integration
@respx.mock
async def test_the_verify_delay_runs_from_when_the_trigger_returned(mock_settings: Any, email: MagicMock) -> None:
    """A slow trigger started long ago but returned a minute ago: it is not verified until the delay has passed."""
    now = datetime.now(UTC)
    two_hours_ago = now - timedelta(hours=2)
    just_returned = _seed(mock_settings, started=two_hours_ago, settled=now - timedelta(minutes=1))
    returned_earlier = _seed(mock_settings, started=two_hours_ago, settled=now - timedelta(minutes=10))
    _wud()
    respx.get(READY).mock(return_value=httpx.Response(200))
    _loki("0", "0")
    results = await verify_pending()
    assert [r.id for r in results] == [returned_earlier]
    conn = store.connect(mock_settings.upgrade_advisor_db_path)
    try:
        assert [d.id for d in store.pending_verifications(conn)] == [just_returned]
    finally:
        conn.close()


@pytest.mark.integration
async def test_a_reaped_trigger_is_settled_at_the_latest_moment_it_could_have_run(
    mock_settings: Any, email: MagicMock
) -> None:
    """The reaper cannot know when a killed trigger ended, only that the hard deadline bounds it."""
    started = datetime.now(UTC) - timedelta(seconds=TRIGGER_TIMEOUT_SECONDS + REAP_GRACE_SECONDS + 60)
    killed = _seed(mock_settings, started=started, outcome="triggering", running_digest=OLD, trigger_state="triggering")
    with patch("src.upgrades.verify.verify_deploy", new_callable=AsyncMock):
        await verify_pending()
    conn = store.connect(mock_settings.upgrade_advisor_db_path)
    try:
        deploy = store.get_deploy(conn, killed)
    finally:
        conn.close()
    assert deploy is not None
    assert deploy.trigger_state == "interrupted"
    assert deploy.settled_at == deploy.started_at + timedelta(seconds=TRIGGER_TIMEOUT_SECONDS)


@pytest.mark.integration
@respx.mock
async def test_a_reaper_failure_does_not_stop_other_rows_or_pending_verifications(
    mock_settings: Any, email: MagicMock, caplog: pytest.LogCaptureFixture
) -> None:
    # reaped (past the trigger timeout plus grace) and due (its settle is past the 300 s verify delay)
    long_ago = datetime.now(UTC) - timedelta(seconds=TRIGGER_TIMEOUT_SECONDS + 300 + 60)
    kw: dict[str, Any] = {"outcome": "triggering", "detail": "deploy triggering", "running_digest": OLD}
    broken = _seed(mock_settings, started=long_ago, trigger_state="triggering", **kw)
    reaped = _seed(mock_settings, started=long_ago, trigger_state="triggering", **kw)
    pending = _seed(mock_settings, started=datetime.now(UTC) - timedelta(hours=2))
    _wud(NEW)
    respx.get(READY).mock(return_value=httpx.Response(200))
    _loki("0", "0")
    real = store.settle_trigger

    def flaky(conn: Any, deploy_id: int, *args: Any, **kwargs: Any) -> bool:
        if deploy_id == broken:
            raise RuntimeError("database is locked")
        return real(conn, deploy_id, *args, **kwargs)

    with patch.object(store, "settle_trigger", side_effect=flaky):
        results = await verify_pending()
    assert sorted(r.id for r in results) == sorted([reaped, pending])
    assert any(f"Could not settle deploy {broken}" in r.getMessage() for r in caplog.records)


@pytest.mark.integration
async def test_a_reaper_listing_failure_still_verifies_pending(mock_settings: Any, email: MagicMock) -> None:
    pending = _seed(mock_settings, started=datetime.now(UTC) - timedelta(hours=2))
    with (
        patch.object(store, "triggering_deploys", side_effect=RuntimeError("boom")),
        patch("src.upgrades.verify.verify_deploy", new_callable=AsyncMock) as verified,
    ):
        await verify_pending()
    verified.assert_awaited_once_with(pending)
