"""Email delivery for weekly reliability reports.

Uses stdlib smtplib with STARTTLS for Gmail SMTP.  Sends multipart/alternative
emails with both plain-text (markdown) and HTML bodies so email clients can
pick their preferred rendering.  All functions are designed to never raise —
they return success/failure booleans and log errors.
"""

import logging
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from src.config import get_settings

logger = logging.getLogger(__name__)


def is_email_configured() -> bool:
    """Check whether all required SMTP settings are present."""
    settings = get_settings()
    return bool(
        settings.smtp_host and settings.smtp_username and settings.smtp_password and settings.report_recipient_email
    )


def send_report_email(
    markdown_report: str,
    html_report: str | None = None,
    subject: str | None = None,
) -> bool:
    """Send a report via SMTP with STARTTLS.

    When *html_report* is provided the email is sent as multipart/alternative
    with both a plain-text fallback and the rich HTML body.  If only
    *markdown_report* is given, a plain-text email is sent (backward compat).

    Args:
        markdown_report: The report body as markdown/plain text.
        html_report: Optional HTML rendering of the report.
        subject: Optional email subject override.

    Returns:
        True if the email was sent successfully, False otherwise.
    """
    settings = get_settings()

    if not is_email_configured():
        logger.warning("Email not configured — skipping send")
        return False

    if subject is None:
        subject = "SRE Assistant — Weekly Reliability Report"

    if html_report:
        msg = MIMEMultipart("alternative")
        msg.attach(MIMEText(markdown_report, "plain", "utf-8"))
        msg.attach(MIMEText(html_report, "html", "utf-8"))
    else:
        msg = MIMEMultipart()
        msg.attach(MIMEText(markdown_report, "plain", "utf-8"))

    msg["Subject"] = subject
    msg["From"] = settings.smtp_username
    msg["To"] = settings.report_recipient_email

    try:
        with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=30) as server:
            _ = server.starttls()
            server.login(settings.smtp_username, settings.smtp_password)
            server.send_message(msg)
        logger.info("Report email sent to %s", settings.report_recipient_email)
        return True
    except Exception:
        logger.exception("Failed to send report email")
        return False
