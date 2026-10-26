"""The Upgrade Advisor's approval page (GET) and guarded deploy (POST).

Served behind the webapp's ``/api/`` prefix, so the form action is relative. GET never changes state (mail scanners
open links); only the POST deploys, and only after the Cloudflare Access JWT verifies and the single-use batch token
is consumed. Every response carries ``no-store`` and a locked-down CSP, and all model or remote text is escaped.
"""

import asyncio
import html
import logging
import re
from datetime import UTC, datetime
from urllib.parse import parse_qsl, quote

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from src.config import get_settings
from src.upgrades import deploy, store
from src.upgrades.access import AccessDenied, verify_access_jwt
from src.upgrades.config import is_advisor_configured
from src.upgrades.models import DeployRecord, VerdictRecord
from src.upgrades.textutil import clean_text

logger = logging.getLogger(__name__)

router = APIRouter()

JWT_HEADER = "Cf-Access-Jwt-Assertion"
MAX_FORM_BYTES = 16 * 1024
MAX_FORM_FIELDS = 200
SECURITY_HEADERS = {
    "Cache-Control": "no-store",
    "X-Frame-Options": "DENY",
    "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'",
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
}
_BATCH_ID_RE = re.compile(r"[A-Za-z0-9_-]{1,64}")
_VERDICT_ID_RE = re.compile(r"[0-9]{1,18}")
_LABELS = {"upgrade": "Recommended", "needs_you": "Needs you", "hold": "Held"}
_OUTCOME_LABELS = {
    "pending_verify": "Deploy triggered",
    "skipped": "Skipped",
    "failed": "Failed",
    "ok": "Verified",
    "digest_mismatch": "Digest mismatch",
}
_STYLE = (
    "body{font-family:system-ui,sans-serif;max-width:46rem;margin:2rem auto;padding:0 1rem;line-height:1.45}"
    "li.item{border:1px solid #ccc;border-radius:6px;padding:.75rem 1rem;margin:.75rem 0;list-style:none}"
    "ul.items{padding:0}.muted{color:#666}button{font-size:1rem;padding:.5rem 1rem}"
)

e = html.escape


class _BadForm(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


def _page(status: int, title: str, body: str) -> HTMLResponse:
    document = (
        f'<!doctype html><html lang="en"><head><meta charset="utf-8">'
        f'<meta name="viewport" content="width=device-width, initial-scale=1">'
        f"<title>{e(title)}</title><style>{_STYLE}</style></head><body><h1>{e(title)}</h1>{body}</body></html>"
    )
    return HTMLResponse(document, status_code=status, headers=SECURITY_HEADERS)


def _not_found() -> HTMLResponse:
    return _page(404, "Not found", "<p>Not found.</p>")


def _denied() -> HTMLResponse:
    return _page(403, "Access denied", "<p>This page needs a valid Cloudflare Access sign-in.</p>")


def _state_page(state: str) -> HTMLResponse:
    if state == "invalid":
        return _page(403, "Link not valid", "<p>This approval link is not valid.</p>")
    return _page(
        410,
        "Link no longer valid",
        "<p>This approval link has already been used or has expired. The next daily review sends a fresh one.</p>",
    )


def _error_page() -> HTMLResponse:
    return _page(500, "Something went wrong", "<p>The request failed; see the sre-agent logs.</p>")


def _strings(value: object) -> list[str]:
    return [v for v in value if isinstance(v, str)] if isinstance(value, list) else []


def _link(link: str) -> str:
    if link.startswith("https://") and not any(ch.isspace() for ch in link):
        return f'<a href="{e(link, quote=True)}" rel="noreferrer noopener">{e(link)}</a>'
    return e(link)


def _versions(v: VerdictRecord) -> str:
    return f"{e(v.running_version or 'unknown')} &rarr; {e(v.candidate_version or 'unknown')}"


def _item(v: VerdictRecord) -> str:
    parts = ['<li class="item">']
    title = f"<strong>{e(v.container)}</strong> {_versions(v)} &middot; {e(_LABELS.get(v.verdict, v.verdict))}"
    if v.verdict in deploy.DEPLOYABLE:
        checked = " checked" if v.verdict == "upgrade" else ""
        control = f'<input type="checkbox" name="verdict_id" value="{v.id}"{checked}>'
        parts.append(f"<label>{control} {title}</label>")
    else:
        parts.append(f"<div>{title}</div>")
    parts.append(f"<p>{e(v.summary)}</p>")
    for heading, key in (("Relevant changes", "relevant_changes"), ("Risks", "risks")):
        entries = _strings(v.details.get(key))
        if entries:
            parts.append(f"<p>{heading}:</p><ul>" + "".join(f"<li>{e(x)}</li>" for x in entries) + "</ul>")
    links = _strings(v.details.get("links"))
    if links:
        parts.append("<p>Links:</p><ul>" + "".join(f"<li>{_link(x)}</li>" for x in links) + "</ul>")
    parts.append("</li>")
    return "".join(parts)


def _review_body(batch_id: str, token: str, verdicts: list[VerdictRecord], email: str) -> str:
    items = '<ul class="items">' + "".join(_item(v) for v in verdicts) + "</ul>"
    signed_in = f'<p class="muted">Signed in as {e(email)}.</p>'
    if not any(v.verdict in deploy.DEPLOYABLE for v in verdicts):
        return signed_in + items + "<p>Nothing in this batch can be deployed.</p>"
    return (
        signed_in
        + "<p>Ticked updates are deployed one at a time when you press the button. This link works once.</p>"
        + f'<form method="post" action="{e(quote(batch_id, safe=""), quote=True)}">'
        + f'<input type="hidden" name="token" value="{e(token, quote=True)}">'
        + items
        + '<button type="submit">Deploy ticked updates</button></form>'
    )


def _outcome_body(email: str, verdicts: list[VerdictRecord], records: list[DeployRecord]) -> str:
    by_verdict = {r.verdict_id: r for r in records}
    rows = []
    for v in verdicts:
        record = by_verdict.get(v.id)
        head = f"<strong>{e(v.container)}</strong> {_versions(v)}"
        if record is None:
            rows.append(f'<li class="item">{head}: not approved</li>')
        else:
            label = _OUTCOME_LABELS.get(record.outcome, record.outcome)
            rows.append(f'<li class="item">{head}: {e(label)} &middot; {e(record.detail)}</li>')
    return f"<p>Approved by {e(email)}.</p>" + '<ul class="items">' + "".join(rows) + "</ul>"


async def _read_form(request: Request) -> tuple[str, list[int]]:
    """Parse ``token`` and repeated ``verdict_id`` from a bounded urlencoded body; raises ``_BadForm``."""
    content_type = request.headers.get("content-type", "").split(";")[0].strip().lower()
    if content_type != "application/x-www-form-urlencoded":
        raise _BadForm(415, "expected a form submission")
    body = bytearray()
    async for chunk in request.stream():
        body += chunk
        if len(body) > MAX_FORM_BYTES:
            raise _BadForm(413, "form too large")
    try:
        pairs = parse_qsl(body.decode("ascii"), keep_blank_values=True, max_num_fields=MAX_FORM_FIELDS, errors="strict")
    except ValueError:  # includes UnicodeDecodeError
        raise _BadForm(400, "unreadable form") from None
    tokens = [v for k, v in pairs if k == "token"]
    if len(tokens) > 1:
        raise _BadForm(400, "more than one token")
    raw_ids = [v for k, v in pairs if k == "verdict_id"]
    if not all(_VERDICT_ID_RE.fullmatch(r) for r in raw_ids):
        raise _BadForm(400, "verdict ids must be integers")
    ids = [int(r) for r in raw_ids]
    if len(set(ids)) != len(ids):
        raise _BadForm(400, "duplicate verdict id")
    return (tokens[0] if tokens else ""), ids


@router.get("/upgrades/batches/{batch_id}")
async def show_batch(batch_id: str, request: Request) -> HTMLResponse:
    """Render the batch for review. Never changes state."""
    if not is_advisor_configured():
        return _not_found()
    try:
        email = await verify_access_jwt(request.headers.get(JWT_HEADER))
    except AccessDenied:
        return _denied()
    token = request.query_params.get("token", "")
    try:
        conn = store.connect(get_settings().upgrade_advisor_db_path)
        try:
            valid_id = bool(_BATCH_ID_RE.fullmatch(batch_id))
            state = store.check_batch(conn, batch_id, token, datetime.now(UTC)) if valid_id else "invalid"
            if state != "ok":
                return _state_page(state)
            verdicts = store.get_batch_verdicts(conn, batch_id)
        finally:
            conn.close()
    except Exception:
        logger.exception("Upgrade approval page for batch %s failed", clean_text(batch_id))
        return _error_page()
    return _page(200, "Upgrade approval", _review_body(batch_id, token, verdicts, email))


@router.post("/upgrades/batches/{batch_id}")
async def approve_batch(batch_id: str, request: Request) -> HTMLResponse:
    """Consume the batch and deploy the ticked verdicts. Order: JWT, form, single-use token, selection, deploy."""
    if not is_advisor_configured():
        return _not_found()
    try:
        email = await verify_access_jwt(request.headers.get(JWT_HEADER))
    except AccessDenied:
        return _denied()
    safe_batch = clean_text(batch_id)
    safe_email = clean_text(email)
    try:
        token, ids = await _read_form(request)
    except _BadForm as exc:
        logger.warning("Upgrade batch %s: rejected form from %s: %s", safe_batch, safe_email, exc.message)
        return _page(exc.status, "Bad request", f"<p>{e(exc.message)}.</p>")
    try:
        conn = store.connect(get_settings().upgrade_advisor_db_path)
        try:
            valid_id = bool(_BATCH_ID_RE.fullmatch(batch_id))
            state = store.consume_batch(conn, batch_id, token, datetime.now(UTC)) if valid_id else "invalid"
            if state != "ok":
                logger.warning("Upgrade batch %s: POST from %s refused (%s)", safe_batch, safe_email, state)
                return _state_page(state)
            try:
                deploy.validate_selection(conn, batch_id, ids)
            except deploy.InvalidSelection as exc:
                logger.warning("Upgrade batch %s: selection from %s rejected: %s", safe_batch, safe_email, exc)
                return _page(
                    400, "Bad request", "<p>The selection does not match this batch; nothing was deployed.</p>"
                )
            verdicts = store.get_batch_verdicts(conn, batch_id)
        finally:
            conn.close()
        logger.info("Upgrade batch %s approved by %s: verdicts %s ticked", safe_batch, safe_email, ids)
        # Shielded: if the client disconnects, the deploys still run to completion and are recorded.
        records = await asyncio.shield(deploy.start_deploy(batch_id, ids))
    except Exception:
        logger.exception("Upgrade batch %s: approval by %s failed", safe_batch, safe_email)
        return _error_page()
    return _page(200, "Upgrade outcome", _outcome_body(email, verdicts, records))
