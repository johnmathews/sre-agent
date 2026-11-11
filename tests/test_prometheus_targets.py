"""Unit tests for the scrape_targets formatting and input validation (no IO)."""

from datetime import UTC, datetime
from typing import Any

import pytest
from pydantic import ValidationError

from src.agent.tools.prometheus_targets import MAX_TARGETS_SHOWN, ScrapeTargetsInput, format_targets

NOW = datetime(2026, 11, 11, 13, 0, 0, tzinfo=UTC)


def _target(
    job: str = "node",
    instance: str = "192.0.2.10:9100",
    health: str = "up",
    hostname: str | None = "alpha",
    last_error: str = "",
    last_scrape: str = "2026-11-11T12:59:30.123456789Z",
) -> dict[str, Any]:
    labels = {"job": job, "instance": instance}
    if hostname is not None:
        labels["hostname"] = hostname
    return {
        "labels": labels,
        "scrapePool": job,
        "health": health,
        "lastScrape": last_scrape,
        "lastScrapeDuration": 0.0123,
        "lastError": last_error,
        "scrapeUrl": f"http://{instance}/metrics",
    }


def test_input_defaults() -> None:
    parsed = ScrapeTargetsInput()
    assert parsed.job is None
    assert parsed.only_down is False


@pytest.mark.parametrize("bad", ['a"b', "a b", "a{b}", "a/b", "", "x" * 101])
def test_input_rejects_bad_job(bad: str) -> None:
    with pytest.raises(ValidationError):
        ScrapeTargetsInput(job=bad)


def test_summary_counts_targets_down_and_jobs() -> None:
    text = format_targets(
        [_target(), _target(job="fastapi", health="down"), _target(job="fastapi", instance="192.0.2.11:8000")],
        job=None,
        only_down=False,
        now=NOW,
    )
    assert text.splitlines()[0].startswith("3 targets, 1 down across 2 jobs")


def test_down_targets_listed_first_with_time_age_duration_and_error() -> None:
    text = format_targets(
        [_target(), _target(job="fastapi", health="down", last_error="connection refused")],
        job=None,
        only_down=False,
        now=NOW,
    )
    lines = text.splitlines()
    assert "fastapi" in lines[1] and "down" in lines[1]
    assert "2026-11-11 12:59:30Z (29s ago)" in lines[1]
    assert "0.012s" in lines[1]
    assert "connection refused" in lines[1]
    assert "node" in lines[2]


def test_last_error_cut_to_200_characters() -> None:
    text = format_targets([_target(health="down", last_error="e" * 500)], job=None, only_down=False, now=NOW)
    assert "e" * 200 in text
    assert "e" * 201 not in text


def test_missing_hostname_and_bad_timestamp_do_not_crash() -> None:
    text = format_targets(
        [_target(hostname=None, last_scrape="not-a-time"), _target(last_scrape="0001-01-01T00:00:00Z")],
        job=None,
        only_down=False,
        now=NOW,
    )
    assert "2 targets" in text


def test_empty_targets() -> None:
    assert format_targets([], job=None, only_down=False, now=NOW).startswith("0 targets, 0 down across 0 jobs")


def test_empty_for_unknown_job_says_so() -> None:
    text = format_targets([], job="ghost", only_down=False, now=NOW)
    assert "ghost" in text


def test_cap_n_of_m() -> None:
    targets = [_target(instance=f"192.0.2.{i % 250}:{9000 + i}") for i in range(MAX_TARGETS_SHOWN + 20)]
    text = format_targets(targets, job=None, only_down=False, now=NOW)
    assert f"showing {MAX_TARGETS_SHOWN} of {MAX_TARGETS_SHOWN + 20}" in text
    assert text.count("health=") == MAX_TARGETS_SHOWN
