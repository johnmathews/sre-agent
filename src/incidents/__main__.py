"""CLI: ``python -m src.incidents --dry-run`` explains a past outage and prints the push without sending it.

Example::

    uv run python -m src.incidents --dry-run --group monitoring --name Dozzle --host infra --container dozzle \\
        --start 2026-10-27T10:06:00Z --end 2026-10-27T10:10:00Z

It builds an in-memory incident (nothing is stored), collects the evidence over ``start - 10 min`` to ``end``, asks
the model for a verdict and prints the bundle, the verdict and the composed push.
"""

import argparse
import asyncio
import logging
import sys
from datetime import datetime, timedelta

from pydantic import ValidationError

from src.config import get_settings
from src.incidents import evidence, job, message
from src.incidents import verdict as verdict_mod
from src.incidents.gatus import endpoint_key
from src.incidents.message import Push
from src.incidents.models import EvidenceBundle, Incident

# Evidence starts this long before --start, as it does before the first failing check in the job.
LEAD_TIME = timedelta(minutes=10)
# Settings the dry run cannot do without, with what each one is for.
_REQUIRED = (
    ("incident_model", "INCIDENT_MODEL", "the model that writes the verdict"),
    ("gatus_url", "GATUS_URL", "the Gatus API, read for the failing checks"),
)


def _iso(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        msg = f"{value!r} is not an ISO-8601 time, e.g. 2026-10-27T10:06:00Z"
        raise argparse.ArgumentTypeError(msg) from None
    if parsed.tzinfo is None:
        msg = f"{value!r} needs a timezone, e.g. 2026-10-27T10:06:00Z"
        raise argparse.ArgumentTypeError(msg)
    return parsed


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m src.incidents", description="Explain a past outage; send nothing.")
    parser.add_argument("--dry-run", action="store_true", required=True, help="explain and print; send nothing")
    parser.add_argument("--group", required=True, help="Gatus endpoint group")
    parser.add_argument("--name", required=True, help="Gatus endpoint name")
    parser.add_argument("--url", default="", help="the endpoint's URL")
    parser.add_argument("--host", default=None, help="Loki hostname the service runs on")
    parser.add_argument("--container", default=None, help="Docker container name")
    parser.add_argument("--start", required=True, type=_iso, help="outage start, ISO-8601 with a timezone")
    parser.add_argument("--end", required=True, type=_iso, help="outage end (the resolve), ISO-8601 with a timezone")
    return parser


def _print_bundle(bundle: EvidenceBundle) -> None:
    print("== Evidence")  # noqa: T201
    print(f"window {bundle.window_start.isoformat()} to {bundle.window_end.isoformat()}")  # noqa: T201
    first = bundle.first_failure_at.isoformat() if bundle.first_failure_at else "unknown"
    print(f"first failure {first}")  # noqa: T201
    for source in bundle.sources:
        status = f"{source.status}: {source.detail}" if source.detail else source.status
        print(f"[{source.name}] {status}")  # noqa: T201
        for line in source.lines:
            print(f"  {line}")  # noqa: T201


def _print_push(push: Push) -> None:
    print("\n== Push (not sent)")  # noqa: T201
    print(f"Title: {push.title}")  # noqa: T201
    if push.url:
        print(f"URL: {push.url} ({push.url_title})")  # noqa: T201
    print()  # noqa: T201
    print(push.message)  # noqa: T201


async def _run(incident: Incident, window: tuple[datetime, datetime]) -> None:
    settings = get_settings()
    bundle = await evidence.collect(incident, window[1], window=window)
    _print_bundle(bundle)
    print("\n== Verdict")  # noqa: T201
    try:
        verdict = await verdict_mod.judge(bundle)
    except Exception as exc:
        reason = job.no_verdict_reason(exc)
        print(f"Verdict unavailable: {reason} (the job sends the fallback push)")  # noqa: T201
        push = message.compose_fallback(
            bundle, reason, timezone=settings.user_timezone, gatus_public_url=settings.gatus_public_url
        )
    else:
        print(verdict.model_dump_json(indent=2))  # noqa: T201
        push = message.compose(
            bundle, verdict, timezone=settings.user_timezone, gatus_public_url=settings.gatus_public_url
        )
    _print_push(push)


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    if args.end <= args.start:
        parser.error("--end must be after --start")
    try:
        settings = get_settings()
    except ValidationError as exc:
        print(f"Settings are invalid or missing ({exc.error_count()} errors); check .env", file=sys.stderr)  # noqa: T201
        return 2
    missing = [f"{env} ({purpose})" for attr, env, purpose in _REQUIRED if not getattr(settings, attr)]
    if missing:
        print(f"Dry run needs these settings: {', '.join(missing)}", file=sys.stderr)  # noqa: T201
        return 2
    logging.basicConfig(level=logging.INFO)
    incident = Incident(
        id=0,
        endpoint_key=endpoint_key(args.group, args.name),
        group=args.group,
        name=args.name,
        url=args.url,
        host=args.host,
        container=args.container,
        triggered_at=args.start,
        resolved_at=args.end,
        state="resolved",
        outcome=None,
        detail=None,
    )
    asyncio.run(_run(incident, (args.start - LEAD_TIME, args.end)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
