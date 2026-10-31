"""Unit tests for composing the explanation push (pure functions, no settings)."""

from datetime import UTC, datetime, timedelta

import pytest

from src.incidents.message import (
    MAX_MESSAGE_CHARS,
    MAX_TITLE_CHARS,
    Push,
    compose,
    compose_fallback,
    down_line,
)
from src.incidents.models import Source
from tests.incidents_helpers import make_bundle, make_incident, make_verdict

AMS = "Europe/Amsterdam"
PUBLIC = "https://status.example.com"


def _lines(push: Push) -> list[str]:
    return push.message.split("\n")


def test_compose_normal_push() -> None:
    push = compose(make_bundle(), make_verdict(), timezone=AMS, gatus_public_url=PUBLIC)
    assert push.title == "Why Wiki went down"
    lines = _lines(push)
    assert lines[0] == "Down 11:06–11:09 CET (3 min)"
    assert lines[1].startswith("Cause: The wiki container was stopped on purpose")
    assert lines[2] == "How it came back: Someone started the container again at 11:09:30."
    assert lines[3] == "Confidence: high"
    assert lines[4:7] == [
        "• 11:06:12 kill signal=15 then stop",
        "• 10:05:58Z SSH login by admin".replace("10:05:58Z", "11:05:58"),
        "• 11:09:30 start",
    ]
    assert lines[7] == "Missing: deploys (Loki timed out after 10 s)"
    assert push.url == "https://status.example.com/endpoints/apps_wiki"
    assert push.url_title == "Gatus history"


def test_model_times_in_utc_are_converted_to_local() -> None:
    verdict = make_verdict(how="Started again at 10:09:30Z by restart policy.")
    push = compose(make_bundle(), verdict, timezone=AMS, gatus_public_url=PUBLIC)
    assert "How it came back: Started again at 11:09:30 by restart policy." in push.message


def test_down_line_in_summer_time() -> None:
    bundle = make_bundle(
        first_failure_at=datetime(2026, 7, 1, 8, 0, tzinfo=UTC),
        incident=make_incident(resolved_at=datetime(2026, 7, 1, 8, 12, 31, tzinfo=UTC)),
    )
    assert down_line(bundle, AMS) == "Down 10:00–10:12 CEST (13 min)"


def test_down_line_across_the_cest_to_cet_change() -> None:
    # 2026-10-25 01:00 UTC: clocks go back from 03:00 CEST to 02:00 CET.
    bundle = make_bundle(
        first_failure_at=datetime(2026, 10, 25, 0, 58, tzinfo=UTC),
        incident=make_incident(resolved_at=datetime(2026, 10, 25, 1, 3, tzinfo=UTC)),
    )
    assert down_line(bundle, AMS) == "Down 02:58 CEST–02:03 CET (5 min)"


def test_down_line_under_one_minute() -> None:
    resolved = datetime(2026, 10, 27, 10, 9, 44, tzinfo=UTC)
    bundle = make_bundle(first_failure_at=resolved - timedelta(seconds=25))
    assert down_line(bundle, AMS) == "Down 11:09–11:09 CET (under 1 min)"


def test_down_line_rounds_minutes() -> None:
    resolved = datetime(2026, 10, 27, 10, 9, 44, tzinfo=UTC)
    bundle = make_bundle(first_failure_at=resolved - timedelta(seconds=150))
    assert down_line(bundle, "UTC").endswith("(3 min)")
    bundle = make_bundle(first_failure_at=resolved - timedelta(seconds=89))
    assert down_line(bundle, "UTC").endswith("(1 min)")


def test_down_line_without_first_failure() -> None:
    assert down_line(make_bundle(first_failure_at=None), AMS) == "Back up at 11:09 CET"


def test_no_missing_line_when_all_sources_ok() -> None:
    sources = [s for s in make_bundle().sources if s.status != "unavailable"]
    push = compose(make_bundle(sources=sources), make_verdict(), timezone=AMS, gatus_public_url=PUBLIC)
    assert "Missing:" not in push.message


def test_missing_line_lists_unavailable_sources_only() -> None:
    sources = [
        Source(name="gatus", status="unavailable", detail="HTTP 502"),
        Source(name="logins", status="skipped", detail="no host"),
        Source(name="restarts", status="unavailable", detail="x" * 300),
    ]
    push = compose(make_bundle(sources=sources), make_verdict(), timezone=AMS, gatus_public_url=PUBLIC)
    [missing] = [line for line in _lines(push) if line.startswith("Missing:")]
    assert missing.startswith("Missing: gatus (HTTP 502), restarts (")
    assert "logins" not in missing
    assert len(missing) < 120


def test_huge_bundle_and_verdict_fit_and_keep_first_lines() -> None:
    sources = [Source(name=f"src{i}", status="unavailable", detail="d" * 400) for i in range(40)]
    verdict = make_verdict(cause="c" * 200, how="h" * 200, evidence=["e" * 120] * 3)
    incident = make_incident(name="N" * 500)
    push = compose(make_bundle(sources=sources, incident=incident), verdict, timezone=AMS, gatus_public_url=PUBLIC)
    assert len(push.message) <= MAX_MESSAGE_CHARS
    assert len(push.title) <= MAX_TITLE_CHARS
    lines = _lines(push)
    assert lines[0].startswith("Down ")
    assert lines[1] == "Cause: " + "c" * 200
    assert lines[2] == "How it came back: " + "h" * 200
    assert lines[3] == "Confidence: high"
    assert lines[-1].startswith("Missing: src0 (")


def test_evidence_is_dropped_before_anything_else() -> None:
    sources = [Source(name=f"src{i}", status="unavailable", detail="d" * 60) for i in range(5)]
    verdict = make_verdict(cause="c" * 200, how="h" * 200, evidence=["e" * 120] * 3)
    push = compose(make_bundle(sources=sources), verdict, timezone=AMS, gatus_public_url=PUBLIC)
    assert len(push.message) <= MAX_MESSAGE_CHARS
    bullets = [line for line in _lines(push) if line.startswith("•")]
    assert bullets[:2] == ["• " + "e" * 120] * 2
    assert bullets[-1].endswith("…") and len(bullets[-1]) < 122  # the last one is cut to fit
    assert _lines(push)[-1].startswith("Missing: ")


def test_fallback_body_order() -> None:
    sources = [
        Source(
            name="gatus",
            status="ok",
            lines=[
                "first failed check 10:06:44Z",
                "failed checks: 3 of 5 in the window",
                "error: connection refused",
                "failed condition: [STATUS] == 200",
            ],
        ),
        Source(name="deploys", status="ok", lines=["10:05:00Z wud updated wiki"]),
        Source(name="proxmox_tasks", status="ok", lines=[]),
        Source(name="restarts", status="ok", lines=["container created 10:09:30Z"]),
        Source(name="docker_events", status="ok", lines=["10:06:13Z die exitCode=137"]),
        Source(name="logins", status="unavailable", detail="Loki down"),
    ]
    push = compose_fallback(make_bundle(sources=sources), "TimeoutError", timezone=AMS, gatus_public_url=PUBLIC)
    assert push.title == "Why Wiki went down"
    assert _lines(push) == [
        "Down 11:06–11:09 CET (3 min)",
        "Summary unavailable: TimeoutError",
        "• 11:06:13 die exitCode=137",
        "• container created 11:09:30",
        "• 11:05:00 wud updated wiki",
        "• error: connection refused",
        "Missing: logins (Loki down)",
    ]


def test_fallback_caps_facts_at_four() -> None:
    sources = [Source(name="docker_events", status="ok", lines=[f"10:0{i}:00Z event {i}" for i in range(9)])]
    push = compose_fallback(make_bundle(sources=sources), "no JSON", timezone="UTC", gatus_public_url=PUBLIC)
    bullets = [line for line in _lines(push) if line.startswith("•")]
    assert bullets == ["• 10:00:00 event 0", "• 10:01:00 event 1", "• 10:02:00 event 2", "• 10:03:00 event 3"]


def test_fallback_gatus_uses_failed_condition_without_error_line() -> None:
    lines = ["first failed check 10:06:44Z", "failed checks: 3 of 5 in the window", "failed condition: [STATUS] == 200"]
    sources = [Source(name="gatus", status="ok", lines=lines)]
    push = compose_fallback(make_bundle(sources=sources), "x", timezone=AMS, gatus_public_url=PUBLIC)
    bullets = [line for line in _lines(push) if line.startswith("•")]
    assert bullets == ["• failed condition: [STATUS] == 200"]


def test_fallback_gatus_without_error_or_condition_adds_nothing() -> None:
    lines = ["first failed check 10:06:44Z", "failed checks: 3 of 5 in the window", "recovered 10:09:44Z"]
    sources = [Source(name="gatus", status="ok", lines=lines)]
    push = compose_fallback(make_bundle(sources=sources), "x", timezone=AMS, gatus_public_url=PUBLIC)
    assert not [line for line in _lines(push) if line.startswith("•")]


def test_fallback_long_reason_and_huge_lines_fit() -> None:
    sources = [Source(name="docker_events", status="ok", lines=["z" * 2000] * 4)]
    push = compose_fallback(make_bundle(sources=sources), "r" * 500, timezone=AMS, gatus_public_url=PUBLIC)
    assert len(push.message) <= MAX_MESSAGE_CHARS
    assert _lines(push)[1].startswith("Summary unavailable: rrr")
    assert len(_lines(push)[1]) <= 150


@pytest.mark.parametrize("public", ["", "https://status.example.com/"])
def test_url(public: str) -> None:
    push = compose(make_bundle(), make_verdict(), timezone=AMS, gatus_public_url=public)
    if public:
        assert push.url == "https://status.example.com/endpoints/apps_wiki"
    else:
        assert push.url is None and push.url_title is None


def test_url_quotes_the_endpoint_key() -> None:
    bundle = make_bundle(incident=make_incident(endpoint_key="dns_adguard-dns-(lan-192-0-2-11)"))
    push = compose_fallback(bundle, "x", timezone=AMS, gatus_public_url=PUBLIC)
    assert push.url == "https://status.example.com/endpoints/dns_adguard-dns-%28lan-192-0-2-11%29"


def test_times_outside_the_window_are_left_alone() -> None:
    sources = [Source(name="docker_events", status="ok", lines=["03:00:00Z start", "25:99:00Z junk"])]
    push = compose_fallback(make_bundle(sources=sources), "x", timezone=AMS, gatus_public_url=PUBLIC)
    assert "• 03:00:00Z start" in push.message
    assert "• 25:99:00Z junk" in push.message
