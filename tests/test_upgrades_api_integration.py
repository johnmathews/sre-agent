"""Integration tests for the approval page and the guarded deploy (JWKS mocked with respx, WUD patched)."""

import ast
import asyncio
import logging
import sqlite3
import subprocess
import sys
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import httpx
import pytest
import respx
from fastapi import FastAPI
from prometheus_client import REGISTRY

from src.api.upgrades import router
from src.upgrades import deploy, store, verify, wud
from src.upgrades.models import Verdict
from src.upgrades.wud import WudContainer, WudError
from tests.upgrades_access_helpers import AUD, CERTS_URL, EMAIL, TEAM, Signer, jwks

REPO = Path(__file__).resolve().parent.parent
OLD = "sha256:" + "a" * 64
NEW = "sha256:" + "b" * 64
NEWER = "sha256:" + "c" * 64
WUD_URL = "http://10.0.0.15:3010"
SIGNER = Signer("k1")
HEADER = "Cf-Access-Jwt-Assertion"
FORM = {"Content-Type": "application/x-www-form-urlencoded"}

ALLOWLIST = "containers:\n" + "".join(
    f"""\
  - name: {name}
    host: {name}
    wud_url: {WUD_URL}
    registry: registry-1.docker.io
    repository: example/{name}
    tag: v3
    github_repo: example/{name}
    readiness_url: http://10.0.0.15:9090/-/ready
    pbs_guest: CT 115
    context_files: []
"""
    for name in ("prometheus", "openwebui", "grafana")
)


@pytest.fixture(autouse=True)
def _env(mock_settings: Any, tmp_path: Path) -> Iterator[None]:
    allow = tmp_path / "allow.yml"
    allow.write_text(ALLOWLIST)
    mock_settings.upgrade_advisor_config_path = str(allow)
    mock_settings.upgrade_advisor_db_path = str(tmp_path / "advisor.db")
    mock_settings.upgrade_advisor_wud_username = "advisor"
    mock_settings.upgrade_advisor_wud_password = "s3cret"
    mock_settings.upgrade_advisor_public_base_url = "https://sre.example.com/api"
    mock_settings.upgrade_advisor_verify_delay_seconds = 0
    mock_settings.cf_access_team_domain = TEAM
    mock_settings.cf_access_aud = AUD
    from src.upgrades import access

    access._jwks_cache.clear()
    access._logged_config_errors.clear()
    with respx.mock(assert_all_called=False) as mock:
        mock.get(CERTS_URL).mock(return_value=httpx.Response(200, json=jwks(SIGNER)))
        yield
    access._jwks_cache.clear()


class FakeWud:
    """Stands in for WUD: per-container state, and a log of deploy triggers."""

    def __init__(self) -> None:
        self.state: dict[str, tuple[str | None, str | None]] = {}
        self.triggered: list[str] = []
        self.fail_trigger: set[str] = set()

    def set(self, name: str, running: str | None = OLD, candidate: str | None = NEW) -> None:
        self.state[name] = (running, candidate)

    async def get_container(self, c: Any) -> WudContainer:
        if c.name not in self.state:
            raise WudError(f"WUD does not list container {c.name}")
        running, candidate = self.state[c.name]
        await asyncio.sleep(0)
        return WudContainer(
            id="abcdef123456",
            name=c.name,
            running_digest=running,
            candidate_digest=candidate,
            update_available=running != candidate,
            update_kind="digest",
        )

    async def trigger_deploy(self, c: Any, wud_id: str) -> None:
        await asyncio.sleep(0)
        if c.name in self.fail_trigger:
            raise WudError(f"WUD deploy failed for container {c.name}: HTTP 500 <secret detail>")
        self.triggered.append(c.name)


@pytest.fixture
def fake_wud() -> Iterator[FakeWud]:
    fake = FakeWud()
    for name in ("prometheus", "openwebui", "grafana"):
        fake.set(name)
    with (
        patch.object(wud, "get_container", side_effect=fake.get_container),
        patch.object(wud, "trigger_deploy", side_effect=fake.trigger_deploy),
    ):
        yield fake


@pytest.fixture
def verify_mock() -> Iterator[AsyncMock]:
    with patch.object(verify, "verify_deploy", new_callable=AsyncMock) as mocked:
        yield mocked


@pytest.fixture
async def client() -> AsyncIterator[httpx.AsyncClient]:
    app = FastAPI()
    app.include_router(router)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as c:
        yield c


def _conn(settings: Any) -> Any:
    return store.connect(settings.upgrade_advisor_db_path)


def _verdict(
    settings: Any,
    container: str = "prometheus",
    verdict: Verdict = "upgrade",
    summary: str = "patch release",
    details: dict[str, object] | None = None,
    candidate: str = NEW,
) -> int:
    conn = _conn(settings)
    try:
        return store.save_verdict(
            conn,
            container=container,
            running_digest=OLD,
            candidate_digest=candidate,
            running_version="v3.1.0",
            candidate_version="v3.1.1",
            verdict=verdict,
            summary=summary,
            details=details
            if details is not None
            else {
                "verdict": verdict,
                "summary": summary,
                "relevant_changes": ["fixes a crash"],
                "risks": ["none known"],
                "links": ["https://github.com/example/prometheus/releases/tag/v3.1.1"],
            },
            now=datetime.now(UTC),
        )
    finally:
        conn.close()


def _batch(settings: Any, ids: list[int], *, now: datetime | None = None) -> tuple[str, str]:
    conn = _conn(settings)
    try:
        return store.create_batch(conn, ids, now or datetime.now(UTC), timedelta(hours=48))
    finally:
        conn.close()


def _auth(token: str | None = None) -> dict[str, str]:
    return {HEADER: token or SIGNER.token()}


async def _post(
    client: httpx.AsyncClient, batch_id: str, token: str, ids: list[int | str], headers: dict[str, str] | None = None
) -> httpx.Response:
    body = [("token", token)] + [("verdict_id", str(i)) for i in ids]
    resp = await client.post(
        f"/upgrades/batches/{batch_id}",
        content=str(httpx.QueryParams(body)),
        headers={**FORM, **(headers if headers is not None else _auth())},
    )
    await _drain()
    return resp


async def _drain() -> None:
    """Wait for the background deploys (and the delayed verifications they schedule) to finish."""
    while deploy._background_tasks:
        await asyncio.gather(*list(deploy._background_tasks))
        await asyncio.sleep(0)  # let the done-callbacks remove finished tasks from the set


def _usable(settings: Any, batch_id: str, token: str) -> bool:
    conn = _conn(settings)
    try:
        return store.check_batch(conn, batch_id, token, datetime.now(UTC)) == "ok"
    finally:
        conn.close()


def _deploys(settings: Any) -> list[Any]:
    conn = _conn(settings)
    try:
        return store.deploys_since(conn, datetime(2000, 1, 1, tzinfo=UTC))
    finally:
        conn.close()


def _decisions(settings: Any) -> dict[int, bool]:
    conn = _conn(settings)
    try:
        return {r["verdict_id"]: bool(r["approved"]) for r in conn.execute("SELECT * FROM decisions")}
    finally:
        conn.close()


def _metric(outcome: str) -> float:
    return REGISTRY.get_sample_value("sre_assistant_upgrade_deploys_total", {"outcome": outcome}) or 0.0


def _assert_security_headers(resp: httpx.Response) -> None:
    assert resp.headers["Cache-Control"] == "no-store"
    assert resp.headers["X-Frame-Options"] == "DENY"
    assert resp.headers["Content-Security-Policy"] == (
        "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; base-uri 'none'; frame-ancestors 'none'"
    )
    assert resp.headers["Referrer-Policy"] == "no-referrer"


async def _settle() -> None:
    for _ in range(5):
        await asyncio.sleep(0)


# ---------------------------------------------------------------------------
# GET
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_get_without_access_header_is_403(
    client: httpx.AsyncClient, mock_settings: Any, fake_wud: FakeWud
) -> None:
    bid, token = _batch(mock_settings, [_verdict(mock_settings)])
    resp = await client.get(f"/upgrades/batches/{bid}", params={"token": token})
    assert resp.status_code == 403
    _assert_security_headers(resp)
    assert "patch release" not in resp.text
    conn = _conn(mock_settings)
    assert store.check_batch(conn, bid, token, datetime.now(UTC)) == "ok"
    conn.close()


@pytest.mark.integration
async def test_get_with_invalid_jwt_is_403(client: httpx.AsyncClient, mock_settings: Any) -> None:
    bid, token = _batch(mock_settings, [_verdict(mock_settings)])
    resp = await client.get(
        f"/upgrades/batches/{bid}", params={"token": token}, headers=_auth(SIGNER.token(aud=["other"]))
    )
    assert resp.status_code == 403


@pytest.mark.integration
async def test_get_renders_the_review_form(client: httpx.AsyncClient, mock_settings: Any) -> None:
    up = _verdict(mock_settings, "prometheus", "upgrade")
    need = _verdict(mock_settings, "openwebui", "needs_you", summary="major version")
    bid, token = _batch(mock_settings, [up, need])
    resp = await client.get(f"/upgrades/batches/{bid}", params={"token": token}, headers=_auth())
    assert resp.status_code == 200
    _assert_security_headers(resp)
    page = resp.text
    assert f'<input type="checkbox" name="verdict_id" value="{up}" checked>' in page
    assert f'<input type="checkbox" name="verdict_id" value="{need}">' in page
    assert f'<form method="post" action="{bid}">' in page
    assert f'<input type="hidden" name="token" value="{token}">' in page
    assert "fixes a crash" in page and "none known" in page and "major version" in page
    assert '<a href="https://github.com/example/prometheus/releases/tag/v3.1.1" rel="noreferrer noopener">' in page
    assert "<button" in page


@pytest.mark.integration
async def test_get_marks_superseded_verdicts_unticked(client: httpx.AsyncClient, mock_settings: Any) -> None:
    old = _verdict(mock_settings, "prometheus", "upgrade")
    current = _verdict(mock_settings, "openwebui", "upgrade")
    bid, token = _batch(mock_settings, [old, current])
    _verdict(mock_settings, "prometheus", "hold", summary="regression reported", details={})  # a newer review
    resp = await client.get(f"/upgrades/batches/{bid}", params={"token": token}, headers=_auth())
    assert resp.status_code == 200
    page = resp.text
    assert f'<input type="checkbox" name="verdict_id" value="{old}">' in page  # unticked
    assert f'<input type="checkbox" name="verdict_id" value="{current}" checked>' in page
    assert page.count("superseded by a newer email") == 1
    old_row = page[page.index(f'value="{old}"') :].split("</li>", 1)[0]
    assert "superseded by a newer email" in old_row


@pytest.mark.integration
async def test_get_twice_changes_nothing(
    client: httpx.AsyncClient, mock_settings: Any, fake_wud: FakeWud, verify_mock: AsyncMock
) -> None:
    bid, token = _batch(mock_settings, [_verdict(mock_settings)])
    for _ in range(2):
        resp = await client.get(f"/upgrades/batches/{bid}", params={"token": token}, headers=_auth())
        assert resp.status_code == 200
    assert fake_wud.triggered == []
    assert _deploys(mock_settings) == []
    assert _decisions(mock_settings) == {}
    conn = _conn(mock_settings)
    try:
        assert store.consume_batch(conn, bid, token, datetime.now(UTC)) == "ok"
    finally:
        conn.close()


@pytest.mark.integration
async def test_get_hold_only_batch_has_no_form_button(client: httpx.AsyncClient, mock_settings: Any) -> None:
    held = _verdict(mock_settings, verdict="hold", summary="unreleased build", details={})
    bid, token = _batch(mock_settings, [held])
    resp = await client.get(f"/upgrades/batches/{bid}", params={"token": token}, headers=_auth())
    assert resp.status_code == 200
    assert "unreleased build" in resp.text
    assert "<form" not in resp.text and "<button" not in resp.text and 'type="checkbox"' not in resp.text


@pytest.mark.integration
async def test_get_wrong_token_is_403(client: httpx.AsyncClient, mock_settings: Any) -> None:
    bid, _ = _batch(mock_settings, [_verdict(mock_settings)])
    resp = await client.get(f"/upgrades/batches/{bid}", params={"token": "guess"}, headers=_auth())
    assert resp.status_code == 403
    _assert_security_headers(resp)
    resp = await client.get("/upgrades/batches/unknown-batch", params={"token": "guess"}, headers=_auth())
    assert resp.status_code == 403


@pytest.mark.integration
async def test_get_used_or_expired_batch_is_410(client: httpx.AsyncClient, mock_settings: Any) -> None:
    bid, token = _batch(mock_settings, [_verdict(mock_settings)])
    conn = _conn(mock_settings)
    store.consume_batch(conn, bid, token, datetime.now(UTC))
    conn.close()
    resp = await client.get(f"/upgrades/batches/{bid}", params={"token": token}, headers=_auth())
    assert resp.status_code == 410
    _assert_security_headers(resp)

    old_bid, old_token = _batch(
        mock_settings, [_verdict(mock_settings, "openwebui")], now=datetime.now(UTC) - timedelta(hours=49)
    )
    resp = await client.get(f"/upgrades/batches/{old_bid}", params={"token": old_token}, headers=_auth())
    assert resp.status_code == 410


@pytest.mark.integration
async def test_page_escapes_model_text(client: httpx.AsyncClient, mock_settings: Any) -> None:
    evil = "<script>alert(1)</script>"
    vid = _verdict(
        mock_settings,
        summary=evil,
        details={
            "summary": evil,
            "relevant_changes": ['<img src=x onerror="alert(1)">'],
            "risks": ["</li><b>bold</b>"],
            "links": ["javascript:alert(1)", 'https://example.com/"><script>x</script>', "http://example.com/plain"],
        },
    )
    bid, token = _batch(mock_settings, [vid])
    resp = await client.get(f"/upgrades/batches/{bid}", params={"token": token}, headers=_auth())
    page = resp.text
    assert "<script>" not in page
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in page
    assert "<img" not in page and "<b>bold</b>" not in page
    assert 'href="javascript:' not in page and 'href="http://example.com/plain"' not in page
    assert "javascript:alert(1)" in page  # shown as text, not a link
    assert 'href="https://example.com/&quot;&gt;&lt;script&gt;x&lt;/script&gt;"' in page


# ---------------------------------------------------------------------------
# POST
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_post_deploys_ticked_items_only(
    client: httpx.AsyncClient, mock_settings: Any, fake_wud: FakeWud, verify_mock: AsyncMock
) -> None:
    up = _verdict(mock_settings, "prometheus", "upgrade")
    need = _verdict(mock_settings, "openwebui", "needs_you")
    bid, token = _batch(mock_settings, [up, need])
    resp = await _post(client, bid, token, [up])
    assert resp.status_code == 200
    _assert_security_headers(resp)
    assert f"Approved by {EMAIL}. Deploying 1 update(s); results arrive by email." in resp.text
    assert "prometheus" in resp.text and "openwebui" not in resp.text
    assert fake_wud.triggered == ["prometheus"]
    assert _decisions(mock_settings) == {up: True, need: False}
    deploys = _deploys(mock_settings)
    assert [(d.verdict_id, d.outcome) for d in deploys] == [(up, "pending_verify")]
    verify_mock.assert_awaited_once_with(deploys[0].id)
    assert not deploy._background_tasks  # finished tasks are released


@pytest.mark.integration
async def test_post_returns_before_the_deploys_finish(
    client: httpx.AsyncClient, mock_settings: Any, verify_mock: AsyncMock
) -> None:
    up = _verdict(mock_settings)
    bid, token = _batch(mock_settings, [up])
    release = asyncio.Event()
    fake = FakeWud()
    fake.set("prometheus")

    async def blocking_trigger(c: Any, wud_id: str) -> None:
        await release.wait()
        fake.triggered.append(c.name)

    with (
        patch.object(wud, "get_container", side_effect=fake.get_container),
        patch.object(wud, "trigger_deploy", side_effect=blocking_trigger),
    ):
        resp = await client.post(
            f"/upgrades/batches/{bid}",
            content=str(httpx.QueryParams([("token", token), ("verdict_id", str(up))])),
            headers={**FORM, **_auth()},
        )
        assert resp.status_code == 200
        assert fake.triggered == [] and _decisions(mock_settings) == {up: True}
        release.set()
        await _drain()
    assert fake.triggered == ["prometheus"]
    assert [d.outcome for d in _deploys(mock_settings)] == ["pending_verify"]


@pytest.mark.integration
async def test_post_logs_the_approver_not_the_token(
    client: httpx.AsyncClient,
    mock_settings: Any,
    fake_wud: FakeWud,
    verify_mock: AsyncMock,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level("INFO")
    up = _verdict(mock_settings)
    bid, token = _batch(mock_settings, [up])
    jwt_token = SIGNER.token()
    await _post(client, bid, token, [up], headers=_auth(jwt_token))
    messages = [r.getMessage() for r in caplog.records]
    assert any(bid in m and EMAIL in m for m in messages)
    assert all(token not in m and jwt_token not in m for m in messages)


@pytest.mark.integration
async def test_post_second_time_is_410_and_deploys_nothing(
    client: httpx.AsyncClient, mock_settings: Any, fake_wud: FakeWud, verify_mock: AsyncMock
) -> None:
    up = _verdict(mock_settings)
    bid, token = _batch(mock_settings, [up])
    assert (await _post(client, bid, token, [up])).status_code == 200
    resp = await _post(client, bid, token, [up])
    assert resp.status_code == 410
    _assert_security_headers(resp)
    assert fake_wud.triggered == ["prometheus"]
    assert len(_deploys(mock_settings)) == 1


@pytest.mark.integration
async def test_post_expired_token_is_410(
    client: httpx.AsyncClient, mock_settings: Any, fake_wud: FakeWud, verify_mock: AsyncMock
) -> None:
    up = _verdict(mock_settings)
    bid, token = _batch(mock_settings, [up], now=datetime.now(UTC) - timedelta(hours=49))
    resp = await _post(client, bid, token, [up])
    assert resp.status_code == 410
    assert fake_wud.triggered == []
    assert _deploys(mock_settings) == [] and _decisions(mock_settings) == {}


@pytest.mark.integration
async def test_post_wrong_token_is_403_and_link_stays_usable(
    client: httpx.AsyncClient, mock_settings: Any, fake_wud: FakeWud, verify_mock: AsyncMock
) -> None:
    up = _verdict(mock_settings)
    bid, token = _batch(mock_settings, [up])
    assert (await _post(client, bid, "guess", [up])).status_code == 403
    assert fake_wud.triggered == []
    assert (await _post(client, bid, token, [up])).status_code == 200


@pytest.mark.integration
async def test_post_without_jwt_is_403_and_link_stays_usable(
    client: httpx.AsyncClient, mock_settings: Any, fake_wud: FakeWud, verify_mock: AsyncMock
) -> None:
    up = _verdict(mock_settings)
    bid, token = _batch(mock_settings, [up])
    resp = await _post(client, bid, token, [up], headers={})
    assert resp.status_code == 403
    _assert_security_headers(resp)
    resp = await _post(client, bid, token, [up], headers=_auth(SIGNER.token(iss="https://other.example.com")))
    assert resp.status_code == 403
    assert fake_wud.triggered == []
    conn = _conn(mock_settings)
    assert store.check_batch(conn, bid, token, datetime.now(UTC)) == "ok"
    conn.close()


@pytest.mark.integration
async def test_post_skips_item_when_upstream_moved(
    client: httpx.AsyncClient, mock_settings: Any, fake_wud: FakeWud, verify_mock: AsyncMock
) -> None:
    moved = _verdict(mock_settings, "prometheus")
    fine = _verdict(mock_settings, "openwebui")
    bid, token = _batch(mock_settings, [moved, fine])
    fake_wud.set("prometheus", candidate=NEWER)
    before = _metric("skipped")
    resp = await _post(client, bid, token, [moved, fine])
    assert resp.status_code == 200
    assert fake_wud.triggered == ["openwebui"]
    outcomes = {d.verdict_id: (d.outcome, d.detail) for d in _deploys(mock_settings)}
    assert outcomes[moved] == ("skipped", "upstream moved since review; re-reviewed next run")
    assert outcomes[fine][0] == "pending_verify"
    assert _metric("skipped") == before + 1


@pytest.mark.integration
async def test_post_skips_superseded_verdict(
    client: httpx.AsyncClient, mock_settings: Any, fake_wud: FakeWud, verify_mock: AsyncMock
) -> None:
    old = _verdict(mock_settings, "prometheus")
    bid, token = _batch(mock_settings, [old])
    newer = _verdict(mock_settings, "prometheus")  # today's run reviewed the same candidate again
    _batch(mock_settings, [newer])
    resp = await _post(client, bid, token, [old])
    assert resp.status_code == 200
    assert fake_wud.triggered == []
    [d] = _deploys(mock_settings)
    assert (d.verdict_id, d.outcome, d.detail) == (old, "skipped", "superseded by a newer review")


@pytest.mark.integration
async def test_a_later_hold_supersedes_an_earlier_link(
    client: httpx.AsyncClient, mock_settings: Any, fake_wud: FakeWud, verify_mock: AsyncMock
) -> None:
    yesterday = _verdict(mock_settings, "prometheus")
    bid, token = _batch(mock_settings, [yesterday])
    _verdict(
        mock_settings, "prometheus", "hold", summary="regression reported upstream", details={}
    )  # today, unbatched
    resp = await _post(client, bid, token, [yesterday])
    assert resp.status_code == 200
    assert fake_wud.triggered == []
    [d] = _deploys(mock_settings)
    assert (d.verdict_id, d.outcome, d.detail) == (yesterday, "skipped", "superseded by a newer review")


@pytest.mark.integration
async def test_post_skips_when_already_running(
    client: httpx.AsyncClient, mock_settings: Any, fake_wud: FakeWud, verify_mock: AsyncMock
) -> None:
    vid = _verdict(mock_settings, "prometheus")
    bid, token = _batch(mock_settings, [vid])
    fake_wud.set("prometheus", running=NEW, candidate=NEW)
    resp = await _post(client, bid, token, [vid])
    assert resp.status_code == 200
    assert fake_wud.triggered == []
    [d] = _deploys(mock_settings)
    assert (d.outcome, d.detail, d.running_digest) == ("skipped", "already running", NEW)


@pytest.mark.integration
async def test_post_trigger_failure_is_failed_and_others_continue(
    client: httpx.AsyncClient, mock_settings: Any, fake_wud: FakeWud, verify_mock: AsyncMock
) -> None:
    bad = _verdict(mock_settings, "prometheus")
    good = _verdict(mock_settings, "openwebui")
    bid, token = _batch(mock_settings, [bad, good])
    fake_wud.fail_trigger.add("prometheus")
    before = _metric("failed")
    resp = await _post(client, bid, token, [bad, good])
    assert resp.status_code == 200
    outcomes = {d.verdict_id: (d.outcome, d.detail) for d in _deploys(mock_settings)}
    assert outcomes[bad] == ("failed", "WUD deploy trigger failed (WudError)")
    assert outcomes[good][0] == "pending_verify"
    assert _metric("failed") == before + 1
    assert verify_mock.await_count == 1  # only the triggered deploy is verified


@pytest.mark.integration
async def test_unexpected_crash_in_one_item_does_not_stop_the_next(
    client: httpx.AsyncClient, mock_settings: Any, fake_wud: FakeWud, verify_mock: AsyncMock
) -> None:
    bad = _verdict(mock_settings, "prometheus")
    good = _verdict(mock_settings, "openwebui")
    bid, token = _batch(mock_settings, [bad, good])
    real = fake_wud.trigger_deploy

    async def trigger(c: Any, wud_id: str) -> None:
        if c.name == "prometheus":
            raise RuntimeError("bug")
        await real(c, wud_id)

    with patch.object(wud, "trigger_deploy", side_effect=trigger):
        await _post(client, bid, token, [bad, good])
    outcomes = {d.verdict_id: (d.outcome, d.detail) for d in _deploys(mock_settings)}
    assert outcomes[bad] == ("failed", "deploy crashed (RuntimeError)")
    assert outcomes[good][0] == "pending_verify"
    assert fake_wud.triggered == ["openwebui"]


@pytest.mark.integration
async def test_record_failure_after_a_trigger_leaves_a_late_failed_row(
    client: httpx.AsyncClient, mock_settings: Any, fake_wud: FakeWud, verify_mock: AsyncMock
) -> None:
    vid = _verdict(mock_settings, "prometheus")
    bid, token = _batch(mock_settings, [vid])
    real_save = store.save_deploy

    def flaky_save(conn: Any, **kwargs: Any) -> int:
        if kwargs["outcome"] == "pending_verify":
            raise sqlite3.OperationalError("database is locked")
        return real_save(conn, **kwargs)

    with patch.object(store, "save_deploy", side_effect=flaky_save):
        await _post(client, bid, token, [vid])
    assert fake_wud.triggered == ["prometheus"]
    [d] = _deploys(mock_settings)
    assert (d.outcome, d.detail) == ("failed", "deploy recorded late (OperationalError)")
    verify_mock.assert_not_awaited()


@pytest.mark.integration
async def test_background_crash_leaves_a_failed_row_per_ticked_verdict(
    client: httpx.AsyncClient, mock_settings: Any, fake_wud: FakeWud, verify_mock: AsyncMock
) -> None:
    a = _verdict(mock_settings, "prometheus")
    b = _verdict(mock_settings, "openwebui")
    c = _verdict(mock_settings, "grafana", "needs_you")
    bid, token = _batch(mock_settings, [a, b, c])
    before = _metric("failed")
    with patch.object(deploy, "_load_containers", side_effect=RuntimeError("boom")):
        resp = await _post(client, bid, token, [a, b])
    assert resp.status_code == 200
    assert fake_wud.triggered == []
    rows = sorted((d.verdict_id, d.outcome, d.detail) for d in _deploys(mock_settings))
    assert rows == [
        (a, "failed", "deploy run crashed (RuntimeError)"),
        (b, "failed", "deploy run crashed (RuntimeError)"),
    ]
    assert _metric("failed") == before + 2
    assert not _usable(mock_settings, bid, token)


@pytest.mark.integration
async def test_crash_after_consume_leaves_a_failed_row_per_ticked_verdict(
    client: httpx.AsyncClient, mock_settings: Any, fake_wud: FakeWud, verify_mock: AsyncMock
) -> None:
    a = _verdict(mock_settings, "prometheus")
    b = _verdict(mock_settings, "openwebui")
    bid, token = _batch(mock_settings, [a, b])
    with patch.object(deploy, "record_decisions", side_effect=RuntimeError("boom")):
        resp = await _post(client, bid, token, [a, b])
    assert resp.status_code == 500
    _assert_security_headers(resp)
    assert fake_wud.triggered == []
    rows = sorted((d.verdict_id, d.outcome, d.detail) for d in _deploys(mock_settings))
    assert rows == [(a, "failed", "approval crashed (RuntimeError)"), (b, "failed", "approval crashed (RuntimeError)")]


@pytest.mark.integration
async def test_post_wud_unreadable_is_failed(
    client: httpx.AsyncClient, mock_settings: Any, fake_wud: FakeWud, verify_mock: AsyncMock
) -> None:
    vid = _verdict(mock_settings, "prometheus")
    bid, token = _batch(mock_settings, [vid])
    del fake_wud.state["prometheus"]
    await _post(client, bid, token, [vid])
    [d] = _deploys(mock_settings)
    assert (d.outcome, d.detail) == ("failed", "could not read WUD state (WudError)")
    assert fake_wud.triggered == []


@pytest.mark.integration
async def test_post_container_no_longer_allowlisted_is_failed(
    client: httpx.AsyncClient, mock_settings: Any, fake_wud: FakeWud, verify_mock: AsyncMock
) -> None:
    vid = _verdict(mock_settings, "removed")
    bid, token = _batch(mock_settings, [vid])
    await _post(client, bid, token, [vid])
    [d] = _deploys(mock_settings)
    assert (d.outcome, d.detail) == ("failed", "container removed is no longer in the allowlist")
    assert fake_wud.triggered == []


@pytest.mark.integration
async def test_post_rejects_verdict_id_from_another_batch(
    client: httpx.AsyncClient, mock_settings: Any, fake_wud: FakeWud, verify_mock: AsyncMock
) -> None:
    mine = _verdict(mock_settings, "prometheus")
    theirs = _verdict(mock_settings, "openwebui")
    bid, token = _batch(mock_settings, [mine])
    _batch(mock_settings, [theirs])
    resp = await _post(client, bid, token, [mine, theirs])
    assert resp.status_code == 400
    _assert_security_headers(resp)
    assert fake_wud.triggered == []
    assert _deploys(mock_settings) == [] and _decisions(mock_settings) == {}
    assert _usable(mock_settings, bid, token)  # a rejected request does not spend the link


@pytest.mark.integration
async def test_post_rejects_a_hold_verdict(
    client: httpx.AsyncClient, mock_settings: Any, fake_wud: FakeWud, verify_mock: AsyncMock
) -> None:
    up = _verdict(mock_settings, "prometheus")
    held = _verdict(mock_settings, "openwebui", "hold", details={})
    bid, token = _batch(mock_settings, [up, held])
    resp = await _post(client, bid, token, [up, held])
    assert resp.status_code == 400
    assert fake_wud.triggered == []
    assert _deploys(mock_settings) == [] and _decisions(mock_settings) == {}
    assert _usable(mock_settings, bid, token)


@pytest.mark.integration
async def test_post_wrong_token_is_403_even_with_bad_ids(
    client: httpx.AsyncClient, mock_settings: Any, fake_wud: FakeWud, verify_mock: AsyncMock
) -> None:
    up = _verdict(mock_settings)
    bid, token = _batch(mock_settings, [up])
    assert (await _post(client, bid, "guess", ["abc", 99999])).status_code == 403
    assert _usable(mock_settings, bid, token)


@pytest.mark.integration
@pytest.mark.parametrize("bad", ["abc", "1.5", "-1", "", " 1", "１"])
async def test_post_rejects_non_integer_ids_before_consuming(
    client: httpx.AsyncClient, mock_settings: Any, fake_wud: FakeWud, verify_mock: AsyncMock, bad: str
) -> None:
    up = _verdict(mock_settings)
    bid, token = _batch(mock_settings, [up])
    resp = await _post(client, bid, token, [up, bad])
    assert resp.status_code == 400
    assert fake_wud.triggered == []
    assert _usable(mock_settings, bid, token)


@pytest.mark.integration
async def test_post_rejects_duplicate_ids_and_non_form_bodies(
    client: httpx.AsyncClient, mock_settings: Any, fake_wud: FakeWud, verify_mock: AsyncMock
) -> None:
    up = _verdict(mock_settings)
    bid, token = _batch(mock_settings, [up])
    assert (await _post(client, bid, token, [up, up])).status_code == 400
    resp = await client.post(
        f"/upgrades/batches/{bid}",
        json={"token": token, "verdict_id": [up]},
        headers=_auth(),
    )
    assert resp.status_code == 415
    resp = await client.post(f"/upgrades/batches/{bid}", content="token=" + "x" * 20_000, headers={**FORM, **_auth()})
    assert resp.status_code == 413
    assert fake_wud.triggered == []


@pytest.mark.integration
async def test_post_with_nothing_ticked_records_rejections_only(
    client: httpx.AsyncClient, mock_settings: Any, fake_wud: FakeWud, verify_mock: AsyncMock
) -> None:
    up = _verdict(mock_settings)
    bid, token = _batch(mock_settings, [up])
    resp = await _post(client, bid, token, [])
    assert resp.status_code == 200
    assert "No updates were selected; nothing was deployed." in resp.text
    assert "Deploying" not in resp.text
    assert _decisions(mock_settings) == {up: False}
    assert _deploys(mock_settings) == [] and fake_wud.triggered == []


@pytest.mark.integration
async def test_racing_posts_deploy_once(
    client: httpx.AsyncClient, mock_settings: Any, fake_wud: FakeWud, verify_mock: AsyncMock
) -> None:
    up = _verdict(mock_settings)
    bid, token = _batch(mock_settings, [up])
    results = await asyncio.gather(*(_post(client, bid, token, [up]) for _ in range(4)))
    assert sorted(r.status_code for r in results) == [200, 410, 410, 410]
    assert fake_wud.triggered == ["prometheus"]
    assert len(_deploys(mock_settings)) == 1


@pytest.mark.integration
async def test_deploys_from_two_batches_never_overlap(mock_settings: Any, verify_mock: AsyncMock) -> None:
    a = _verdict(mock_settings, "prometheus")
    b = _verdict(mock_settings, "openwebui")
    bid_a, _ = _batch(mock_settings, [a])
    bid_b, _ = _batch(mock_settings, [b])
    active = 0
    peak = 0

    async def slow_trigger(c: Any, wud_id: str) -> None:
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.01)
        active -= 1

    fake = FakeWud()
    fake.set("prometheus")
    fake.set("openwebui")
    with (
        patch.object(wud, "get_container", side_effect=fake.get_container),
        patch.object(wud, "trigger_deploy", side_effect=slow_trigger),
    ):
        await asyncio.gather(deploy.deploy_approved(bid_a, [a]), deploy.deploy_approved(bid_b, [b]))
    assert peak == 1


def _patched_wud(fake: FakeWud, **overrides: Any) -> Any:
    from contextlib import ExitStack

    stack = ExitStack()
    stack.enter_context(
        patch.object(wud, "get_container", side_effect=overrides.get("get_container", fake.get_container))
    )
    stack.enter_context(
        patch.object(wud, "trigger_deploy", side_effect=overrides.get("trigger_deploy", fake.trigger_deploy))
    )
    return stack


@pytest.mark.integration
async def test_shutdown_during_a_trigger_records_the_in_flight_item_and_the_rest(
    mock_settings: Any, verify_mock: AsyncMock
) -> None:
    a, b, c = (_verdict(mock_settings, n) for n in ("prometheus", "openwebui", "grafana"))
    bid, _ = _batch(mock_settings, [a, b, c])
    fake = FakeWud()
    for name in ("prometheus", "openwebui", "grafana"):
        fake.set(name)
    entered = asyncio.Event()

    async def slow_trigger(c: Any, wud_id: str) -> None:
        entered.set()
        await asyncio.sleep(3600)

    before = _metric("failed")
    with _patched_wud(fake, trigger_deploy=slow_trigger):
        task = deploy.start_deploy(bid, [a, b, c])
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    rows = {d.verdict_id: (d.outcome, d.detail, d.running_digest) for d in _deploys(mock_settings)}
    assert rows == {
        a: ("pending_verify", "interrupted during deploy (shutdown); verified next run", OLD),
        b: ("failed", "deploy interrupted (shutdown)", None),
        c: ("failed", "deploy interrupted (shutdown)", None),
    }
    assert _metric("failed") == before + 2
    await _drain()
    verify_mock.assert_not_awaited()  # the next advisor run's verify_pending settles the in-flight one
    conn = _conn(mock_settings)
    try:
        assert [d.verdict_id for d in store.pending_verifications(conn)] == [a]
    finally:
        conn.close()


@pytest.mark.integration
async def test_shutdown_before_the_trigger_records_the_item_as_interrupted(
    mock_settings: Any, verify_mock: AsyncMock
) -> None:
    a = _verdict(mock_settings, "prometheus")
    bid, _ = _batch(mock_settings, [a])
    fake = FakeWud()
    entered = asyncio.Event()

    async def slow_get(c: Any) -> WudContainer:
        entered.set()
        await asyncio.sleep(3600)
        raise AssertionError("unreachable")

    with _patched_wud(fake, get_container=slow_get):
        task = deploy.start_deploy(bid, [a])
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert fake.triggered == []
    assert [(d.outcome, d.detail) for d in _deploys(mock_settings)] == [("failed", "deploy interrupted (shutdown)")]


@pytest.mark.integration
async def test_wait_for_deploys_returns_at_once_when_idle() -> None:
    assert not deploy._deploy_runs
    started = asyncio.get_running_loop().time()
    assert await deploy.wait_for_deploys(30) == 0
    assert asyncio.get_running_loop().time() - started < 1


@pytest.mark.integration
async def test_wait_for_deploys_is_bounded_and_reports_stragglers(
    mock_settings: Any, verify_mock: AsyncMock, caplog: pytest.LogCaptureFixture
) -> None:
    a = _verdict(mock_settings, "prometheus")
    bid, _ = _batch(mock_settings, [a])
    fake = FakeWud()
    fake.set("prometheus")
    release = asyncio.Event()

    async def blocking_trigger(c: Any, wud_id: str) -> None:
        await release.wait()

    with _patched_wud(fake, trigger_deploy=blocking_trigger):
        deploy.start_deploy(bid, [a])
        await asyncio.sleep(0)
        assert await deploy.wait_for_deploys(0.05) == 1
        assert any("still running" in r.getMessage() for r in caplog.records)
        release.set()
        assert await deploy.wait_for_deploys(5) == 0
        await _drain()
    assert [d.outcome for d in _deploys(mock_settings)] == ["pending_verify"]


@pytest.mark.integration
def test_app_shutdown_waits_for_deploys(mock_settings: Any) -> None:
    from unittest.mock import MagicMock

    from fastapi.testclient import TestClient

    from src.api import main

    with (
        patch.object(main, "build_agent", return_value=MagicMock()),
        patch.object(main, "wait_for_deploys", new_callable=AsyncMock, return_value=0) as waited,
    ):
        with TestClient(main.app):
            waited.assert_not_awaited()
        waited.assert_awaited_once_with(main.SHUTDOWN_DEPLOY_WAIT_SECONDS)
    assert main.SHUTDOWN_DEPLOY_WAIT_SECONDS <= 5.0  # Docker's default stop timeout is 10 s


@pytest.mark.integration
async def test_routes_404_when_advisor_not_configured(
    client: httpx.AsyncClient, mock_settings: Any, fake_wud: FakeWud
) -> None:
    up = _verdict(mock_settings)
    bid, token = _batch(mock_settings, [up])
    mock_settings.upgrade_advisor_public_base_url = ""
    resp = await client.get(f"/upgrades/batches/{bid}", params={"token": token}, headers=_auth())
    assert resp.status_code == 404
    _assert_security_headers(resp)
    resp = await _post(client, bid, token, [up])
    assert resp.status_code == 404
    assert fake_wud.triggered == []


@pytest.mark.integration
def test_main_app_serves_the_upgrade_routes() -> None:
    from src.api.main import app

    paths = {(getattr(r, "path", ""), m) for r in app.routes for m in getattr(r, "methods", set())}
    assert ("/upgrades/batches/{batch_id}", "GET") in paths
    assert ("/upgrades/batches/{batch_id}", "POST") in paths


# ---------------------------------------------------------------------------
# The deploy capability is never reachable from the agent or MCP
# ---------------------------------------------------------------------------

FORBIDDEN = {"src.upgrades.deploy", "src.upgrades.wud", "src.upgrades.verify", "src.upgrades.job", "src.api.upgrades"}


def _package_of(path: Path) -> list[str]:
    """The package a module's relative imports resolve against (for ``__init__.py``, the package itself)."""
    return list(path.relative_to(REPO).with_suffix("").parts)[:-1]


def _imported_modules(path: Path) -> set[str]:
    """Every module an AST import names, with relative imports resolved against the file's package."""
    tree = ast.parse(path.read_text(), filename=str(path))
    package = _package_of(path)
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            base = package[: len(package) - node.level + 1] if node.level else []
            module = ".".join([*base, *(node.module.split(".") if node.module else [])])
            if module:
                found.add(module)
            found.update(f"{module}.{alias.name}" if module else alias.name for alias in node.names)
    return found


def test_relative_imports_are_resolved() -> None:
    pkg = REPO / "src" / "agent" / "tools"
    fake = pkg / "_probe_does_not_exist.py"
    tree_text = "from ...upgrades import deploy\nfrom ..upgrades_x import y\nfrom . import z\n"
    with patch.object(Path, "read_text", return_value=tree_text):
        found = _imported_modules(fake)
    assert "src.upgrades.deploy" in found
    assert "src.agent.upgrades_x.y" in found
    assert "src.agent.tools.z" in found


AGENT_SURFACE = [
    *sorted((REPO / "src" / "agent").rglob("*.py")),
    REPO / "src" / "api" / "mcp_server.py",
    REPO / "src" / "memory" / "tools.py",
]


def test_deploy_is_not_an_agent_or_mcp_tool() -> None:
    sources = AGENT_SURFACE
    assert len(sources) > 5 and all(p.exists() for p in sources)
    for path in sources:
        hits = _imported_modules(path) & FORBIDDEN
        assert not hits, f"{path.relative_to(REPO)} imports {sorted(hits)}"
        text = path.read_text()
        for module in FORBIDDEN:
            assert module not in text, f"{path.relative_to(REPO)} mentions {module}"
        assert "deploy_approved" not in text and "trigger_deploy" not in text


def test_agent_and_mcp_do_not_load_the_deploy_stack_transitively() -> None:
    code = (
        "import importlib, pkgutil, sys\n"
        "import src.agent.tools as tools\n"
        "for m in pkgutil.iter_modules(tools.__path__): importlib.import_module(f'src.agent.tools.{m.name}')\n"
        "import src.agent.mcp_tools, src.api.mcp_server, src.memory.tools\n"
        f"bad = sorted(set(sys.modules) & set({sorted(FORBIDDEN)!r}))\n"
        "print(bad)\n"
        "sys.exit(1 if bad else 0)\n"
    )
    result = subprocess.run([sys.executable, "-c", code], cwd=REPO, capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stdout + result.stderr


# ---------------------------------------------------------------------------
# The link token never reaches uvicorn's access log
# ---------------------------------------------------------------------------


def _access_record(path: str) -> logging.LogRecord:
    return logging.LogRecord(
        "uvicorn.access",
        logging.INFO,
        __file__,
        1,
        '%s - "%s %s HTTP/%s" %d',
        ("10.0.0.15:53412", "GET", path, "1.1", 200),
        None,
    )


def test_access_log_redacts_the_approval_token() -> None:
    from src.api.upgrades import RedactApprovalToken

    record = _access_record("/upgrades/batches/AbC-123_x?token=s3cr3t-T0ken_value&x=1")
    assert RedactApprovalToken().filter(record) is True  # redact, never drop
    message = record.getMessage()
    assert "s3cr3t" not in message
    assert message == '10.0.0.15:53412 - "GET /upgrades/batches/AbC-123_x?token=REDACTED&x=1 HTTP/1.1" 200'

    record = _access_record("/upgrades/batches/AbC?x=1&token=abc")
    RedactApprovalToken().filter(record)
    assert record.getMessage().endswith('"GET /upgrades/batches/AbC?x=1&token=REDACTED HTTP/1.1" 200')

    other = _access_record("/conversations?token=keep")
    RedactApprovalToken().filter(other)
    assert "token=keep" in other.getMessage()


def test_access_log_filter_is_installed_once() -> None:
    from src.api.upgrades import RedactApprovalToken, install_access_log_redaction

    install_access_log_redaction()
    install_access_log_redaction()
    filters = [f for f in logging.getLogger("uvicorn.access").filters if isinstance(f, RedactApprovalToken)]
    assert len(filters) == 1


def test_uvicorn_logging_config_keeps_the_filter() -> None:
    import logging.config

    from uvicorn.config import LOGGING_CONFIG

    from src.api.upgrades import RedactApprovalToken

    logging.config.dictConfig(LOGGING_CONFIG)
    assert any(isinstance(f, RedactApprovalToken) for f in logging.getLogger("uvicorn.access").filters)
