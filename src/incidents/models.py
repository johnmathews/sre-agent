"""Shared models for the incident explainer: the Gatus webhook body, the stored incident and the evidence bundle.

These types are the contract between the receiver (``src/api/incidents.py``), the store, the evidence collector and
the explanation job. All datetimes are timezone-aware UTC.
"""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

# Bound every webhook field so an oversized body is rejected (422) before it reaches the store.
MAX_FIELD_LENGTH = 512

GatusEventType = Literal["TRIGGERED", "RESOLVED"]
IncidentState = Literal["open", "resolved", "explaining", "explained", "failed"]
OutcomeState = Literal["explained", "failed"]
SourceStatus = Literal["ok", "unavailable", "skipped"]


class GatusEvent(BaseModel):
    """The JSON body Gatus's custom alert provider posts on every alert transition."""

    model_config = ConfigDict(extra="ignore")

    event: GatusEventType
    group: str = Field(max_length=MAX_FIELD_LENGTH)
    name: str = Field(max_length=MAX_FIELD_LENGTH)
    url: str = Field(default="", max_length=MAX_FIELD_LENGTH)
    description: str = Field(default="", max_length=MAX_FIELD_LENGTH)


class Target(BaseModel):
    """Where the monitored service runs, parsed from the alert description (``host=<h>;container=<c>``)."""

    model_config = ConfigDict(frozen=True)

    host: str | None = None
    container: str | None = None


class Incident(BaseModel):
    """One outage of one Gatus endpoint, from TRIGGERED (when seen) to its explanation."""

    model_config = ConfigDict(frozen=True)

    id: int
    endpoint_key: str
    group: str
    name: str
    url: str
    host: str | None
    container: str | None
    # None when the TRIGGERED event was never received; the start is then read from Gatus.
    triggered_at: datetime | None
    resolved_at: datetime | None
    state: IncidentState
    outcome: str | None
    detail: str | None


class Source(BaseModel):
    """One evidence source and what it returned. ``detail`` gives the reason when the status is not ``ok``, and can
    carry notes on an ``ok`` source (for example "no lifecycle events for this container in the window")."""

    name: str
    status: SourceStatus
    detail: str = ""
    lines: list[str] = Field(default_factory=list)


class EvidenceBundle(BaseModel):
    """Everything collected about one incident, over the window ``window_start`` to ``window_end``."""

    incident: Incident
    window_start: datetime
    window_end: datetime
    first_failure_at: datetime | None
    sources: list[Source]


__all__ = [
    "MAX_FIELD_LENGTH",
    "EvidenceBundle",
    "GatusEvent",
    "GatusEventType",
    "Incident",
    "IncidentState",
    "OutcomeState",
    "Source",
    "SourceStatus",
    "Target",
]
