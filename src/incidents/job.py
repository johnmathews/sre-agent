"""The background job that explains one resolved incident.

``POST /incidents/gatus`` schedules ``explain(incident_id)`` once per newly resolved incident. The full job (settle,
collect evidence, ask the model for a verdict, send the Pushover push, record the outcome) replaces this body.
"""

import logging

logger = logging.getLogger(__name__)


async def explain(incident_id: int) -> None:
    """Explain incident ``incident_id``. Not implemented yet: logs a warning and returns."""
    logger.warning("incident explanation not implemented yet (incident %d)", incident_id)


__all__ = ["explain"]
