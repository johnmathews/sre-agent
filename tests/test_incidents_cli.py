"""Unit tests for the dry-run CLI ``python -m src.incidents --dry-run`` (evidence and model stubbed)."""

import json
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from src.incidents.__main__ import main
from src.incidents.verdict import VerdictError
from tests.incidents_helpers import make_bundle, make_verdict

ARGS = [
    "--dry-run",
    "--group",
    "apps",
    "--name",
    "Wiki",
    "--host",
    "apps",
    "--container",
    "wiki",
    "--start",
    "2026-10-27T10:06:00Z",
    "--end",
    "2026-10-27T10:10:00Z",
]


@pytest.fixture(autouse=True)
def _env(mock_settings: Any) -> None:
    mock_settings.user_timezone = "Europe/Amsterdam"


@pytest.fixture
def collect() -> Iterator[AsyncMock]:
    with patch("src.incidents.evidence.collect", AsyncMock(return_value=make_bundle())) as fake:
        yield fake


def test_dry_run_prints_bundle_verdict_and_push(collect: AsyncMock, capsys: pytest.CaptureFixture[str]) -> None:
    with (
        patch("src.incidents.verdict.judge", AsyncMock(return_value=make_verdict())),
        patch("src.incidents.pushover.send") as send,
    ):
        code = main(ARGS)
    assert code == 0
    assert send.call_count == 0
    out = capsys.readouterr().out
    assert "== Evidence" in out and "== Verdict" in out and "== Push (not sent)" in out
    assert "[docker_events] ok" in out and "  10:06:12Z kill signal=15" in out
    assert "[deploys] unavailable: Loki timed out after 10 s" in out
    verdict_json = out.split("== Verdict\n", 1)[1].split("\n\n== Push", 1)[0]
    assert json.loads(verdict_json)["resolution"] == "manual"
    assert "Title: Why Wiki went down" in out
    assert "URL: https://status.example.com/endpoints/apps_wiki (Gatus history)" in out
    assert "Down 11:06–11:09 CET (3 min)" in out

    incident, now = collect.call_args.args
    start, end = datetime(2026, 10, 27, 10, 6, tzinfo=UTC), datetime(2026, 10, 27, 10, 10, tzinfo=UTC)
    assert collect.call_args.kwargs["window"] == (start - timedelta(minutes=10), end)
    assert now == end
    assert (incident.id, incident.state, incident.endpoint_key) == (0, "resolved", "apps_wiki")
    assert (incident.triggered_at, incident.resolved_at) == (start, end)
    assert (incident.host, incident.container, incident.url) == ("apps", "wiki", "")


def test_dry_run_verdict_failure_prints_fallback(collect: AsyncMock, capsys: pytest.CaptureFixture[str]) -> None:
    with patch("src.incidents.verdict.judge", AsyncMock(side_effect=VerdictError("no JSON object in the answer"))):
        code = main(ARGS)
    assert code == 0
    out = capsys.readouterr().out
    assert "Verdict unavailable: no JSON object in the answer (the job sends the fallback push)" in out
    assert "Summary unavailable: no JSON object in the answer" in out


def test_dry_run_without_gatus_url_exits_with_message(mock_settings: Any, capsys: pytest.CaptureFixture[str]) -> None:
    mock_settings.gatus_url = ""
    assert main(ARGS) == 2
    assert "GATUS_URL" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("start", "end", "message"),
    [
        ("yesterday", "2026-10-27T10:10:00Z", "not an ISO-8601 time"),
        ("2026-10-27T10:06:00", "2026-10-27T10:10:00Z", "needs a timezone"),
        ("2026-10-27T10:10:00Z", "2026-10-27T10:06:00Z", "--end must be after --start"),
    ],
)
def test_dry_run_rejects_bad_times(start: str, end: str, message: str, capsys: pytest.CaptureFixture[str]) -> None:
    args = [*ARGS[:-4], "--start", start, "--end", end]
    with pytest.raises(SystemExit) as exc:
        main(args)
    assert exc.value.code == 2
    assert message in capsys.readouterr().err


def test_dry_run_flag_is_required(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        main(ARGS[1:])
    assert exc.value.code == 2
