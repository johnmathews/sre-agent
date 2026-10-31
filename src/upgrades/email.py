"""Render the Upgrade Advisor's daily email (plain text and HTML)."""

import html
from typing import TYPE_CHECKING

from src.report.generator import (
    _BODY_STYLE,
    _BULLET_STYLE,
    _CONTAINER_STYLE,
    _H2_STYLE,
    _HEADER_STYLE,
    _SECTION_STYLE,
)
from src.upgrades.models import DeployRecord

if TYPE_CHECKING:  # job imports this module; the types are only needed for annotations
    from src.upgrades.job import ItemResult, RunSummary

WARNING_PREFIX = "warning: "
RESULTS_HEADING = "Results of approved upgrades (last 24 hours)"
# Shown under the approve link and on the page after approving, so nobody waits for an email that only comes on failure.
AFTER_APPROVAL_HEADING = "What happens after you approve"
AFTER_APPROVAL = (
    "The advisor deploys each approved upgrade then checks it a few minutes later.",
    "If a check fails then the advisor emails you immediately.",
    "The next advisor email lists the results.",
)
_LIST_STYLE = "margin: 4px 0; padding-left: 20px;"
# Plain words for each deploy outcome; the raw codes stay in the store, logs and metrics.
_OUTCOME_TEXT = {
    "ok": "upgraded and verified",
    "failed": "upgrade failed",
    "digest_mismatch": "running a different version than approved",
    "skipped": "skipped, not deployed",
    "pending_verify": "deployed, checks not run yet",
    "triggering": "deploy still in progress",
}

# A section is (heading, list of (title, detail lines)).
_Section = tuple[str, list[tuple[str, list[str]]]]


def _review_lines(item: "ItemResult") -> list[str]:
    if item.review is None:
        return []
    lines = [item.review.summary]
    lines += [f"Change: {c}" for c in item.review.relevant_changes]
    lines += [f"Risk: {r}" for r in item.review.risks]
    lines += [f"Link: {link}" for link in item.review.links]
    return lines


def _outcome_entry(summary: "RunSummary", deploy: DeployRecord) -> tuple[str, list[str]]:
    name = summary.outcome_containers.get(deploy.id, f"deploy {deploy.id}")
    warnings = [p[len(WARNING_PREFIX) :] for p in deploy.detail.split("; ") if p.startswith(WARNING_PREFIX)]
    if deploy.outcome == "ok" and warnings:
        return f"{name}: upgraded, verified with warnings", [f"Warning: {w}" for w in warnings]
    return f"{name}: {_OUTCOME_TEXT.get(deploy.outcome, deploy.outcome)}", [deploy.detail] if deploy.detail else []


def _sections(summary: "RunSummary") -> list[_Section]:
    upgrade = [i for i in summary.items if i.verdict == "upgrade" and i.error is None]
    needs = [i for i in summary.items if i.verdict == "needs_you" and i.error is None]
    held = [i for i in summary.items if i.verdict == "hold" and i.error is None]
    errors = [i for i in summary.items if i.error is not None]
    sections: list[_Section] = [
        ("Recommended (upgrade)", [(i.container, _review_lines(i)) for i in upgrade]),
        ("Needs you", [(i.container, _review_lines(i)) for i in needs]),
        ("Held", [(i.container, [i.hold_reason or ""]) for i in held]),
        ("Errors", [(i.container, [i.error or ""]) for i in errors]),
        (RESULTS_HEADING, [_outcome_entry(summary, d) for d in summary.outcomes]),
    ]
    return [(heading, entries) for heading, entries in sections if entries]


def render(summary: "RunSummary") -> tuple[str, str, str]:
    """Return ``(subject, markdown, html)``. Every interpolated value is HTML-escaped in the HTML part."""
    recommended = sum(1 for i in summary.items if i.verdict == "upgrade" and i.error is None)
    need_you = sum(1 for i in summary.items if i.verdict == "needs_you" and i.error is None)
    subject = f"Upgrades: {recommended} recommended, {need_you} need you"
    sections = _sections(summary)

    md = [f"# {subject}", ""]
    if summary.approve_url:
        md += [f"Approve: {summary.approve_url}", "", f"{AFTER_APPROVAL_HEADING}:", ""]
        md += [f"- {line}" for line in AFTER_APPROVAL]
        md.append("")
    for heading, entries in sections:
        md += [f"## {heading}", ""]
        for title, lines in entries:
            md.append(f"- {title}")
            md += [f"  - {line}" for line in lines]
        md.append("")

    e = html.escape
    parts = [f'<html><body style="{_BODY_STYLE}"><div style="{_CONTAINER_STYLE}">']
    parts.append(f'<div style="{_HEADER_STYLE}">{e(subject)}</div>')
    if summary.approve_url:
        parts.append(
            f'<div style="{_SECTION_STYLE}"><a href="{e(summary.approve_url, quote=True)}">Review and approve</a>'
            f'<p style="{_BULLET_STYLE}">{e(AFTER_APPROVAL_HEADING)}:</p><ul style="{_LIST_STYLE}">'
            + "".join(f'<li style="{_BULLET_STYLE}">{e(line)}</li>' for line in AFTER_APPROVAL)
            + "</ul></div>"
        )
    for heading, entries in sections:
        parts.append(f'<div style="{_SECTION_STYLE}"><h2 style="{_H2_STYLE}">{e(heading)}</h2>')
        for title, lines in entries:
            parts.append(f'<p style="{_BULLET_STYLE}"><strong>{e(title)}</strong></p>')
            parts += [f'<p style="{_BULLET_STYLE}">{e(line)}</p>' for line in lines]
        parts.append("</div>")
    parts.append("</div></body></html>")
    return subject, "\n".join(md), "".join(parts)


def render_abort(exc_type: str) -> tuple[str, str, str]:
    """Email for a run that could not start: the exception class only, never its text."""
    subject = f"Upgrade advisor could not run: {exc_type}"
    return (
        subject,
        f"# {subject}\n\nSee the sre-agent logs for details.\n",
        (
            f'<html><body style="{_BODY_STYLE}"><div style="{_CONTAINER_STYLE}">'
            f'<div style="{_HEADER_STYLE}">{html.escape(subject)}</div>'
            f'<p style="{_BULLET_STYLE}">See the sre-agent logs for details.</p></div></body></html>'
        ),
    )
