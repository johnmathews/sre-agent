"""The Upgrade Advisor's approval pages and guarded deploy.

The emailed link is ``.../upgrades/batches/{id}#token=...``: the token lives in the URL fragment, which browsers never
send, so no request URL (and therefore no nginx, Traefik, uvicorn or Cloudflare log line) ever carries it. GET serves
a tiny launcher page whose one hash-pinned inline script moves the fragment token into a form and POSTs it to
``.../{id}/view``, which checks the link (no write) and renders the review page. Its Approve form POSTs to ``.../{id}``,
which deploys only after the Access JWT verifies, the signed-in email is a listed approver, the selection is valid and
the single-use batch token is consumed; the deploys then run in the background and report by email.

Served behind the webapp's ``/api/`` prefix, so every form action is relative. Every response carries ``no-store`` and
a locked-down CSP, and all model or remote text is escaped.
"""

import base64
import hashlib
import html
import logging
import re
import unicodedata
from datetime import UTC, datetime
from urllib.parse import parse_qsl, quote

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from src.config import get_settings
from src.upgrades import deploy, store
from src.upgrades.access import AccessDenied, verify_access_jwt
from src.upgrades.config import is_advisor_configured
from src.upgrades.models import VerdictRecord
from src.upgrades.textutil import clean_text

logger = logging.getLogger(__name__)

router = APIRouter()

JWT_HEADER = "Cf-Access-Jwt-Assertion"
MAX_FORM_BYTES = 16 * 1024
MAX_FORM_FIELDS = 200
SECURITY_HEADERS = {
    "Cache-Control": "no-store",
    "X-Frame-Options": "DENY",
    "Content-Security-Policy": (
        "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; base-uri 'none'; frame-ancestors 'none'"
    ),
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
}
# The launcher page's only script: move the fragment token into the form, drop the fragment from the address bar and
# history, and submit. The CSP pins this exact text by hash, so no other script can run on the page.
LAUNCH_SCRIPT = (
    '(function(){var f=document.getElementById("approval-launch");'
    "var m=/^#token=([A-Za-z0-9_%-]{1,512})$/.exec(location.hash);"
    'history.replaceState(null,"",location.pathname+location.search);'
    'if(!m){document.getElementById("launch-status").textContent="This link has no approval token.";return;}'
    "f.elements.token.value=decodeURIComponent(m[1]);f.submit();})();"
)
LAUNCH_SCRIPT_HASH = base64.b64encode(hashlib.sha256(LAUNCH_SCRIPT.encode()).digest()).decode()
LAUNCH_HEADERS = {
    **SECURITY_HEADERS,
    "Content-Security-Policy": (
        f"default-src 'none'; script-src 'sha256-{LAUNCH_SCRIPT_HASH}'; style-src 'unsafe-inline'; "
        "form-action 'self'; base-uri 'none'; frame-ancestors 'none'"
    ),
}
_BATCH_ID_RE = re.compile(r"[A-Za-z0-9_-]{1,64}")
_VERDICT_ID_RE = re.compile(r"[0-9]{1,18}")
_LABELS = {"upgrade": "Recommended", "needs_you": "Needs you", "hold": "Held"}
_STYLE = (
    "body{font-family:system-ui,sans-serif;max-width:46rem;margin:2rem auto;padding:0 1rem;line-height:1.45}"
    "li.item{border:1px solid #ccc;border-radius:6px;padding:.75rem 1rem;margin:.75rem 0;list-style:none}"
    "ul.items{padding:0}.muted{color:#666}button{font-size:1rem;padding:.5rem 1rem}"
)

e = html.escape

# Defence in depth: no route puts the token in a URL any more (it travels in the fragment and in POST bodies), but a
# hand-edited or old-style ``?token=`` link would still be redacted from uvicorn's access log.
ACCESS_LOGGER = "uvicorn.access"
_TOKEN_QUERY_RE = re.compile(r"([?&]token=)[^&\s\"]*")


def _redact(text: str) -> str:
    return _TOKEN_QUERY_RE.sub(r"\1REDACTED", text) if "/upgrades/batches/" in text else text


class RedactApprovalToken(logging.Filter):
    """Replace the ``token`` query value of approval-link paths with ``REDACTED`` (the line itself is kept)."""

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = _redact(record.msg)
        if isinstance(record.args, tuple):
            record.args = tuple(_redact(a) if isinstance(a, str) else a for a in record.args)
        return True


def install_access_log_redaction() -> None:
    """Attach the redaction filter to uvicorn's access logger once. ``logging.config.dictConfig`` (which uvicorn runs
    at startup) adds filters but never removes existing ones, so installing at import is enough."""
    access_logger = logging.getLogger(ACCESS_LOGGER)
    if not any(isinstance(f, RedactApprovalToken) for f in access_logger.filters):
        access_logger.addFilter(RedactApprovalToken())


install_access_log_redaction()


class _BadForm(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


def _page(status: int, title: str, body: str, headers: dict[str, str] = SECURITY_HEADERS) -> HTMLResponse:
    document = (
        f'<!doctype html><html lang="en"><head><meta charset="utf-8">'
        f'<meta name="viewport" content="width=device-width, initial-scale=1">'
        f"<title>{e(title)}</title><style>{_STYLE}</style></head><body><h1>{e(title)}</h1>{body}</body></html>"
    )
    return HTMLResponse(document, status_code=status, headers=headers)


def _not_found() -> HTMLResponse:
    return _page(404, "Not found", "<p>Not found.</p>")


def _denied() -> HTMLResponse:
    return _page(403, "Access denied", "<p>This page needs a valid Cloudflare Access sign-in.</p>")


def _not_an_approver(email: str) -> HTMLResponse:
    return _page(403, "Access denied", f"<p>{e(email)} is not allowed to approve upgrades.</p>")


def _approval_unconfigured() -> HTMLResponse:
    return _page(
        403,
        "Approval not configured",
        "<p>Approval is not configured on this server; nothing can be approved here. See the sre-agent logs.</p>",
    )


def normalise_email(value: str) -> str | None:
    """Comparable form of an email (same on both sides): trimmed, NFKC-normalised, case-folded. Anything non-ASCII,
    before or after normalising, is ``None`` and never matches: NFKC alone would fold e.g. a full-width address
    into an ASCII one, letting a distinct identity match a listed approver."""
    stripped = value.strip()
    if not stripped.isascii():
        return None
    folded = unicodedata.normalize("NFKC", stripped).casefold()
    return folded if folded.isascii() else None


def _approver_entries() -> tuple[frozenset[str], int]:
    """``(normalised approvers, number of non-empty entries dropped as non-ASCII)``."""
    entries = [part for part in get_settings().upgrade_advisor_approver_emails.split(",") if part.strip()]
    kept = [normalise_email(part) for part in entries]
    return frozenset(k for k in kept if k is not None), sum(k is None for k in kept)


def _approvers() -> frozenset[str]:
    return _approver_entries()[0]


def missing_approval_settings() -> list[str]:
    """Names (never values) of the settings approval needs that are empty."""
    s = get_settings()
    missing = [] if _approvers() else ["UPGRADE_ADVISOR_APPROVER_EMAILS"]
    if not s.cf_access_team_domain:
        missing.append("CF_ACCESS_TEAM_DOMAIN")
    if not s.cf_access_aud:
        missing.append("CF_ACCESS_AUD")
    return missing


def warn_if_approval_unconfigured() -> None:
    """At startup: when the advisor runs but approval cannot work, say which settings are missing (names only), and
    how many approver entries were dropped as non-ASCII (a count, never the entries)."""
    if not is_advisor_configured():
        return
    _, dropped = _approver_entries()
    if dropped:
        logger.warning(
            "UPGRADE_ADVISOR_APPROVER_EMAILS: %d %s ignored (not ASCII; such an address never matches)",
            dropped,
            "entry" if dropped == 1 else "entries",
        )
    missing = missing_approval_settings()
    if missing:
        logger.warning(
            "Upgrade Advisor approval is not configured (missing: %s); every approval request is refused",
            ", ".join(missing),
        )


async def _authorise(request: Request) -> str | HTMLResponse:
    """The approver's email, or the page to answer with, in this order: 404 when the advisor is off; the generic
    denied page without a valid Access sign-in (also when CF_ACCESS_* are missing: verification then denies, and
    the startup warning names them); 403 "approval not configured" for a signed-in user when no approver is
    listed; 403 for a signed-in user who is not an approver. Unauthenticated callers never learn the config state."""
    if not is_advisor_configured():
        return _not_found()
    try:
        email = await verify_access_jwt(request.headers.get(JWT_HEADER))
    except AccessDenied:
        return _denied()
    approvers = _approvers()
    if not approvers:
        return _approval_unconfigured()
    normalised = normalise_email(email)
    if normalised is None or normalised not in approvers:
        logger.warning("Upgrade approval refused: %s is not an approver", clean_text(email))
        return _not_an_approver(email)
    return email


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


def _item(v: VerdictRecord, superseded: bool) -> str:  # noqa: FBT001
    parts = ['<li class="item">']
    title = f"<strong>{e(v.container)}</strong> {_versions(v)} &middot; {e(_LABELS.get(v.verdict, v.verdict))}"
    if superseded:  # a newer review exists; the POST would skip it anyway
        title += " &middot; <em>superseded by a newer email</em>"
    if v.verdict in deploy.DEPLOYABLE:
        checked = " checked" if v.verdict == "upgrade" and not superseded else ""
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


def _review_body(batch_id: str, token: str, verdicts: list[VerdictRecord], superseded: set[int], email: str) -> str:
    items = '<ul class="items">' + "".join(_item(v, v.id in superseded) for v in verdicts) + "</ul>"
    signed_in = f'<p class="muted">Signed in as {e(email)}.</p>'
    if not any(v.verdict in deploy.DEPLOYABLE for v in verdicts):
        return signed_in + items + "<p>Nothing in this batch can be deployed.</p>"
    return (
        signed_in
        + "<p>Ticked updates are deployed one at a time when you press the button. This link works once.</p>"
        + f'<form method="post" action="../{e(quote(batch_id, safe=""), quote=True)}">'
        + f'<input type="hidden" name="token" value="{e(token, quote=True)}">'
        + items
        + '<button type="submit">Deploy ticked updates</button></form>'
    )


def _outcome_body(email: str, ticked: list[VerdictRecord]) -> str:
    if not ticked:
        return f"<p>Approved by {e(email)}. No updates were selected; nothing was deployed.</p>"
    rows = "".join(f'<li class="item"><strong>{e(v.container)}</strong> {_versions(v)}</li>' for v in ticked)
    return f"<p>Approved by {e(email)}. Deploying {len(ticked)} update(s); results arrive by email.</p>" + (
        f'<ul class="items">{rows}</ul>' if rows else ""
    )


async def _read_form(request: Request) -> tuple[str, list[str]]:
    """Read ``token`` and the raw repeated ``verdict_id`` values from a bounded urlencoded body (``_BadForm``)."""
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
    return (tokens[0] if tokens else ""), [v for k, v in pairs if k == "verdict_id"]


def _parse_ids(raw_ids: list[str]) -> list[int]:
    if not all(_VERDICT_ID_RE.fullmatch(r) for r in raw_ids):
        raise _BadForm(400, "verdict ids must be integers")
    ids = [int(r) for r in raw_ids]
    if len(set(ids)) != len(ids):
        raise _BadForm(400, "duplicate verdict id")
    return ids


def _launch_body(batch_id: str) -> str:
    action = e(quote(batch_id, safe=""), quote=True) + "/view"
    return (
        '<p id="launch-status">Opening the approval page&hellip;</p>'
        f'<form id="approval-launch" method="post" action="{action}">'
        '<input type="hidden" name="token" value=""></form>'
        "<noscript><p>This page needs JavaScript: the approval token is in the link's #fragment, which only a script"
        " on this page can read and send.</p></noscript>"
        f"<script>{LAUNCH_SCRIPT}</script>"
    )


@router.get("/upgrades/batches/{batch_id}")
async def launch_batch(batch_id: str, request: Request) -> HTMLResponse:
    """Serve the launcher that POSTs the fragment token to ``{batch_id}/view``. Reads no token and touches no state."""
    email = await _authorise(request)
    if isinstance(email, HTMLResponse):
        return email
    if not _BATCH_ID_RE.fullmatch(batch_id):
        return _state_page("invalid")
    return _page(200, "Upgrade approval", _launch_body(batch_id), headers=LAUNCH_HEADERS)


@router.post("/upgrades/batches/{batch_id}/view")
async def view_batch(batch_id: str, request: Request) -> HTMLResponse:
    """Check the posted token (no write) and render the batch for review."""
    email = await _authorise(request)
    if isinstance(email, HTMLResponse):
        return email
    try:
        token, _ = await _read_form(request)
    except _BadForm as exc:
        logger.warning("Upgrade batch %s: view rejected: %s", clean_text(batch_id), exc.message)
        return _page(exc.status, "Bad request", f"<p>{e(exc.message)}.</p>")
    try:
        conn = store.connect(get_settings().upgrade_advisor_db_path)
        try:
            valid_id = bool(_BATCH_ID_RE.fullmatch(batch_id))
            state = store.check_batch(conn, batch_id, token, datetime.now(UTC)) if valid_id else "invalid"
            if state != "ok":
                return _state_page(state)
            verdicts = store.get_batch_verdicts(conn, batch_id)
            superseded: set[int] = set()
            for v in verdicts:
                latest = store.latest_verdict_for_container(conn, v.container)
                if latest is None or latest.id != v.id:
                    superseded.add(v.id)
        finally:
            conn.close()
    except Exception:
        logger.exception("Upgrade approval page for batch %s failed", clean_text(batch_id))
        return _error_page()
    return _page(200, "Upgrade approval", _review_body(batch_id, token, verdicts, superseded, email))


@router.post("/upgrades/batches/{batch_id}")
async def approve_batch(batch_id: str, request: Request) -> HTMLResponse:
    """Approve the ticked verdicts and start their deploys in the background.

    Order: JWT, form, link check (no write), ids, selection, single-use consume, decisions, background deploy.
    Nothing is consumed until the request is known to be valid.
    """
    email = await _authorise(request)
    if isinstance(email, HTMLResponse):
        return email
    safe_batch = clean_text(batch_id)
    safe_email = clean_text(email)

    def bad(status: int, message: str) -> HTMLResponse:
        logger.warning("Upgrade batch %s: POST from %s rejected: %s", safe_batch, safe_email, message)
        return _page(status, "Bad request", f"<p>{e(message)}; nothing was deployed.</p>")

    try:
        token, raw_ids = await _read_form(request)
    except _BadForm as exc:
        return bad(exc.status, exc.message)
    ids: list[int] = []
    consumed = False
    try:
        conn = store.connect(get_settings().upgrade_advisor_db_path)
        try:
            valid_id = bool(_BATCH_ID_RE.fullmatch(batch_id))
            state = store.check_batch(conn, batch_id, token, datetime.now(UTC)) if valid_id else "invalid"
            if state != "ok":
                logger.warning("Upgrade batch %s: POST from %s refused (%s)", safe_batch, safe_email, state)
                return _state_page(state)
            try:
                ids = _parse_ids(raw_ids)
            except _BadForm as exc:
                return bad(exc.status, exc.message)
            try:
                deploy.validate_selection(conn, batch_id, ids)
            except deploy.InvalidSelection as exc:
                return bad(400, f"the selection does not match this batch ({exc})")
            now = datetime.now(UTC)
            state = store.consume_batch(conn, batch_id, token, now)
            if state != "ok":  # a concurrent POST consumed it between the check and here
                logger.warning("Upgrade batch %s: POST from %s lost to another (%s)", safe_batch, safe_email, state)
                return _state_page(state)
            consumed = True
            deploy.record_decisions(conn, batch_id, ids, now)
            ticked = [v for v in store.get_batch_verdicts(conn, batch_id) if v.id in set(ids)]
        finally:
            conn.close()
        logger.info("Upgrade batch %s approved by %s: verdicts %s ticked", safe_batch, safe_email, ids)
        if ids:
            deploy.start_deploy(batch_id, ids)
    except Exception as exc:
        logger.exception("Upgrade batch %s: approval by %s failed", safe_batch, safe_email)
        if consumed:  # the link is spent: leave a failed row for every ticked verdict rather than nothing
            deploy.record_missing(ids, f"approval crashed ({type(exc).__name__})")
        return _error_page()
    return _page(200, "Upgrade approved", _outcome_body(email, ticked))
