"""Pydantic models for the Upgrade Advisor store."""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel

Verdict = Literal["upgrade", "hold", "needs_you"]
DeployOutcome = Literal["triggering", "pending_verify", "ok", "failed", "digest_mismatch", "skipped"]
# How a deploy's WUD trigger ended: still running, returned OK, raised (incl. timeout), or cut off by shutdown/kill.
# None for rows that never reached the trigger (skipped, failed guards).
TriggerState = Literal["triggering", "triggered", "trigger_error", "interrupted"]
# The end states a ``triggering`` row may settle into.
SETTLED_TRIGGER_STATES = ("triggered", "trigger_error", "interrupted")


class VerdictRecord(BaseModel):
    """One advisor verdict for a (container, candidate digest) pair."""

    id: int
    container: str
    running_digest: str
    candidate_digest: str
    running_version: str | None
    candidate_version: str | None
    verdict: Verdict
    summary: str
    details: dict[str, object]
    created_at: datetime
    batch_id: str | None


class DeployRecord(BaseModel):
    """One deploy attempt for a verdict, and how its verification went."""

    id: int
    verdict_id: int
    started_at: datetime
    outcome: DeployOutcome
    running_digest: str | None
    detail: str
    trigger_state: TriggerState | None = None
    # When the WUD trigger ended (settle_trigger); None while triggering and for rows that never reached the trigger.
    settled_at: datetime | None = None


class Batch(BaseModel):
    """A set of verdicts approvable through one single-use link."""

    id: str
    verdict_ids: list[int]
    created_at: datetime
    expires_at: datetime
    used_at: datetime | None
