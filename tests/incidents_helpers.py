"""Builders for incident explainer tests: an incident, an evidence bundle and a verdict with fake values."""

from datetime import UTC, datetime
from typing import Any

from src.incidents.models import EvidenceBundle, Incident, Source
from src.incidents.verdict import Verdict

TRIGGERED = datetime(2026, 10, 27, 10, 7, 44, tzinfo=UTC)
FIRST_FAILURE = datetime(2026, 10, 27, 10, 6, 44, tzinfo=UTC)
RESOLVED = datetime(2026, 10, 27, 10, 9, 44, tzinfo=UTC)


def make_incident(**kw: Any) -> Incident:
    data: dict[str, Any] = {
        "id": 7,
        "endpoint_key": "apps_wiki",
        "group": "apps",
        "name": "Wiki",
        "url": "http://wiki.example.test",
        "host": "apps",
        "container": "wiki",
        "triggered_at": TRIGGERED,
        "resolved_at": RESOLVED,
        "state": "explaining",
        "outcome": None,
        "detail": None,
    }
    data.update(kw)
    return Incident(**data)


def make_sources() -> list[Source]:
    return [
        Source(
            name="gatus",
            status="ok",
            lines=["first failed check 10:06:44Z", "error: connection refused", "recovered 10:09:44Z"],
        ),
        Source(
            name="docker_events",
            status="ok",
            lines=[
                "10:06:12Z kill signal=15",
                "10:06:13Z die exitCode=0",
                "10:06:13Z stop",
                "10:09:30Z start",
            ],
        ),
        Source(name="container_logs", status="ok", lines=["10:06:12Z received SIGTERM, shutting down"]),
        Source(name="restarts", status="ok", lines=["container created 10:09:30Z"]),
        Source(name="proxmox_tasks", status="ok", lines=[]),
        Source(name="logins", status="ok", lines=["10:05:58Z sshd: accepted publickey for admin"]),
        Source(name="deploys", status="unavailable", detail="Loki timed out after 10 s"),
    ]


def make_bundle(**kw: Any) -> EvidenceBundle:
    data: dict[str, Any] = {
        "incident": make_incident(),
        "window_start": datetime(2026, 10, 27, 9, 56, 44, tzinfo=UTC),
        "window_end": RESOLVED,
        "first_failure_at": FIRST_FAILURE,
        "sources": make_sources(),
    }
    data.update(kw)
    return EvidenceBundle(**data)


def make_verdict(**kw: Any) -> Verdict:
    data: dict[str, Any] = {
        "cause": "The wiki container was stopped on purpose (SIGTERM, exit code 0) after an SSH login.",
        "how": "Someone started the container again at 10:09:30Z.",
        "resolution": "manual",
        "confidence": "high",
        "evidence": ["10:06:12Z kill signal=15 then stop", "10:05:58Z SSH login by admin", "10:09:30Z start"],
    }
    data.update(kw)
    return Verdict(**data)
