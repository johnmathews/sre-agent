"""Tests for the daily Upgrade Advisor job (wud, releases and review patched at module level)."""

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from urllib.parse import parse_qs, urlsplit

import pytest
from prometheus_client import REGISTRY

from src.upgrades import store
from src.upgrades.job import run_advisor
from src.upgrades.releases import Release
from src.upgrades.review import Review
from src.upgrades.wud import WudContainer, WudError

NOW = datetime(2026, 10, 26, 6, 0, tzinfo=UTC)
OLD = "sha256:" + "a" * 64
NEW = "sha256:" + "b" * 64


def _allow(names: list[str]) -> str:
    entries = "".join(
        f"""  - name: {n}
    host: h{n}
    wud_url: http://10.0.0.15:3010
    registry: registry-1.docker.io
    repository: org/{n}
    tag: v1
    github_repo: org/{n}
    readiness_url: http://10.0.0.15:9090/ready
    pbs_guest: CT 115
    context_files: []
"""
        for n in names
    )
    return "containers:\n" + entries


def _release(tag: str, version: tuple[int, ...], age_days: int) -> Release:
    return Release(
        tag=tag, version=version, published_at=NOW - timedelta(days=age_days), body="", url="https://example.com"
    )


def _wud(kind: str | None = "digest", available: bool = True, cand: str = NEW) -> WudContainer:  # noqa: FBT001, FBT002
    return WudContainer(
        id="abcdef123456",
        name="x",
        running_digest=OLD,
        candidate_digest=cand,
        update_available=available,
        update_kind=kind,
    )


def _metric(name: str, labels: dict[str, str] | None = None) -> float:
    return REGISTRY.get_sample_value(name, labels or {}) or 0.0


@pytest.fixture
def env(mock_settings: Any, tmp_path: Path) -> Any:
    allow = tmp_path / "allow.yml"
    allow.write_text(_allow(["alpha"]))
    mock_settings.upgrade_advisor_config_path = str(allow)
    mock_settings.upgrade_advisor_db_path = str(tmp_path / "advisor.db")
    mock_settings.upgrade_advisor_public_base_url = "https://advisor.example.com/"
    mock_settings.upgrade_advisor_min_age_hours = 72
    mock_settings.upgrade_advisor_wud_username = "u"
    mock_settings.upgrade_advisor_wud_password = "p"
    return mock_settings


@pytest.fixture
def mocks() -> Iterator[dict[str, MagicMock]]:
    rel_old, rel_new = _release("v1.0.0", (1, 0, 0), 30), _release("v1.1.0", (1, 1, 0), 10)

    async def for_digest(c: Any, digest: str, rels: list[Release]) -> Release:
        return rel_new if digest == NEW else rel_old

    with (
        patch("src.upgrades.job.wud.get_container", AsyncMock(return_value=_wud())) as get,
        patch("src.upgrades.job.releases.list_releases", AsyncMock(return_value=[rel_new, rel_old])) as lst,
        patch("src.upgrades.job.releases.release_for_digest", AsyncMock(side_effect=for_digest)),
        patch("src.upgrades.job.releases.regression_issues", AsyncMock(return_value=[])),
        patch(
            "src.upgrades.job.review.review",
            AsyncMock(return_value=Review(verdict="upgrade", summary="fine")),
        ) as rev,
        patch("src.upgrades.job.verify.verify_pending", AsyncMock(return_value=[])),
        patch("src.upgrades.job.is_email_configured", return_value=True),
        patch("src.upgrades.job.send_report_email", return_value=True) as send,
    ):
        yield {"get": get, "list": lst, "review": rev, "send": send}


async def test_nothing_pending_sends_no_email_but_sets_last_success(env: Any, mocks: dict[str, MagicMock]) -> None:
    mocks["get"].return_value = _wud(available=False)
    before = _metric("sre_assistant_upgrade_runs_total", {"status": "success"})
    summary = await run_advisor(now=NOW)
    assert summary.items == []
    mocks["send"].assert_not_called()
    mocks["review"].assert_not_called()
    assert _metric("sre_assistant_upgrade_runs_total", {"status": "success"}) == before + 1
    assert _metric("sre_assistant_upgrade_last_success_timestamp") > 0


async def test_one_wud_host_down_reports_error_and_reviews_the_other(env: Any, mocks: dict[str, MagicMock]) -> None:
    Path(env.upgrade_advisor_config_path).write_text(_allow(["alpha", "beta"]))
    mocks["get"].side_effect = [WudError("secret detail"), _wud()]
    # beta is old enough (first seen long ago)
    conn = store.connect(env.upgrade_advisor_db_path)
    store.first_seen(conn, "beta", NEW, NOW - timedelta(days=5))
    conn.close()
    summary = await run_advisor(now=NOW)
    by = {i.container: i for i in summary.items}
    assert by["alpha"].error == "WudError"
    assert by["beta"].verdict == "upgrade"
    _, body, _ = mocks["send"].call_args.args
    assert "secret detail" not in body
    assert mocks["send"].call_args.args[2] == "Upgrades: 1 recommended, 0 need you"


async def test_batch_contains_upgrade_and_needs_you_but_not_hold(env: Any, mocks: dict[str, MagicMock]) -> None:
    Path(env.upgrade_advisor_config_path).write_text(_allow(["alpha", "beta", "gamma"]))
    mocks["get"].side_effect = [_wud(), _wud(), _wud(kind="tag")]
    mocks["review"].side_effect = [Review(verdict="upgrade", summary="a"), Review(verdict="needs_you", summary="b")]
    conn = store.connect(env.upgrade_advisor_db_path)
    for n in ("alpha", "beta"):
        store.first_seen(conn, n, NEW, NOW - timedelta(days=5))
    conn.close()
    summary = await run_advisor(now=NOW)
    assert summary.batch_id is not None
    by = {i.container: i for i in summary.items}
    assert by["gamma"].verdict == "hold"
    assert by["gamma"].hold_reason == "tag change offered by WUD; digest-only policy"
    conn = store.connect(env.upgrade_advisor_db_path)
    members = {v.container for v in store.get_batch_verdicts(conn, summary.batch_id)}
    conn.close()
    assert members == {"alpha", "beta"}
    assert mocks["review"].await_count == 2  # the tag-change hold was never reviewed


async def test_approve_url_uses_public_base_and_token(env: Any, mocks: dict[str, MagicMock]) -> None:
    conn = store.connect(env.upgrade_advisor_db_path)
    store.first_seen(conn, "alpha", NEW, NOW - timedelta(days=5))
    conn.close()
    summary = await run_advisor(now=NOW)
    assert summary.approve_url is not None
    parts = urlsplit(summary.approve_url)
    assert f"{parts.scheme}://{parts.netloc}" == "https://advisor.example.com"
    assert parts.path == f"/upgrades/batches/{summary.batch_id}"
    token = parse_qs(parts.query)["token"][0]
    conn = store.connect(env.upgrade_advisor_db_path)
    assert summary.batch_id is not None
    assert store.check_batch(conn, summary.batch_id, token, NOW) == "ok"
    conn.close()
    assert summary.approve_url in mocks["send"].call_args.args[0]


async def test_too_new_candidate_is_held_before_review(env: Any, mocks: dict[str, MagicMock]) -> None:
    summary = await run_advisor(now=NOW)  # first seen now -> too new
    assert summary.items[0].verdict == "hold"
    assert "too new" in (summary.items[0].hold_reason or "")
    mocks["review"].assert_not_called()
    assert summary.batch_id is None


async def test_dry_run_sends_nothing_and_creates_no_batch(env: Any, mocks: dict[str, MagicMock]) -> None:
    conn = store.connect(env.upgrade_advisor_db_path)
    store.first_seen(conn, "alpha", NEW, NOW - timedelta(days=5))
    conn.close()
    summary = await run_advisor(now=NOW, send_email=False)
    assert summary.items[0].verdict == "upgrade"
    assert summary.batch_id is None
    assert summary.approve_url is None
    mocks["send"].assert_not_called()


async def test_unloadable_allowlist_aborts_with_class_name_only(env: Any, mocks: dict[str, MagicMock]) -> None:
    Path(env.upgrade_advisor_config_path).write_text("containers: [")
    before = _metric("sre_assistant_upgrade_runs_total", {"status": "error"})
    summary = await run_advisor(now=NOW)
    assert summary.aborted is not None
    assert _metric("sre_assistant_upgrade_runs_total", {"status": "error"}) == before + 1
    subject = mocks["send"].call_args.args[2]
    assert subject == f"Upgrade advisor could not run: {summary.aborted}"
    mocks["get"].assert_not_called()


async def test_yesterdays_deploys_trigger_an_email(env: Any, mocks: dict[str, MagicMock]) -> None:
    mocks["get"].return_value = _wud(available=False)
    conn = store.connect(env.upgrade_advisor_db_path)
    vid = store.save_verdict(
        conn,
        container="alpha",
        running_digest=OLD,
        candidate_digest=NEW,
        running_version=None,
        candidate_version=None,
        verdict="upgrade",
        summary="s",
        details={},
        now=NOW - timedelta(hours=20),
    )
    store.save_deploy(
        conn,
        verdict_id=vid,
        now=NOW - timedelta(hours=5),
        outcome="ok",
        running_digest=NEW,
        detail="digest matches; warning: loki skipped",
    )
    conn.close()
    summary = await run_advisor(now=NOW)
    assert summary.outcome_containers
    body = mocks["send"].call_args.args[0]
    assert "alpha: verified with warnings" in body
