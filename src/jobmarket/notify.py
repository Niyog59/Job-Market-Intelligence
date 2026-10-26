"""Deliver queued notifications by SMTP.

Alerts and recovery notices are always written to the notifications table first (the
outbox). Delivery is a separate step and happens only when SMTP settings are present;
without them, notifications simply stay pending and can be read with
`jobmarket notifications`.

Settings (environment or .env):
  JOBMARKET_SMTP_HOST, JOBMARKET_SMTP_PORT (default 587), JOBMARKET_SMTP_USER,
  JOBMARKET_SMTP_PASSWORD, JOBMARKET_ALERT_FROM, JOBMARKET_ALERT_TO,
  JOBMARKET_SMTP_STARTTLS (default "true")
"""

import os
import smtplib
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from email.message import EmailMessage

import psycopg


@dataclass(frozen=True)
class SmtpSettings:
    host: str
    port: int
    user: str
    password: str = field(repr=False)
    sender: str
    recipient: str
    starttls: bool


def smtp_settings(environ: Mapping[str, str] | None = None) -> SmtpSettings | None:
    """Return SMTP settings, or None if delivery is not configured."""
    env = os.environ if environ is None else environ
    host = env.get("JOBMARKET_SMTP_HOST", "").strip()
    recipient = env.get("JOBMARKET_ALERT_TO", "").strip()
    sender = env.get("JOBMARKET_ALERT_FROM", "").strip()
    if not (host and recipient and sender):
        return None
    return SmtpSettings(
        host=host,
        port=int(env.get("JOBMARKET_SMTP_PORT", "587")),
        user=env.get("JOBMARKET_SMTP_USER", "").strip(),
        password=env.get("JOBMARKET_SMTP_PASSWORD", ""),
        sender=sender,
        recipient=recipient,
        starttls=env.get("JOBMARKET_SMTP_STARTTLS", "true").strip().lower() != "false",
    )


@dataclass(frozen=True)
class SendResult:
    configured: bool
    sent: int
    failed: int
    pending_left: int


def send_pending(
    conn: psycopg.Connection,
    settings: SmtpSettings | None,
    now: datetime,
    smtp_factory: Callable[..., smtplib.SMTP] = smtplib.SMTP,
) -> SendResult:
    """Try to deliver every pending notification once."""
    pending = conn.execute(
        "SELECT notification_id, subject, body FROM notifications WHERE status = 'pending' "
        "ORDER BY notification_id"
    ).fetchall()
    if settings is None:
        return SendResult(False, 0, 0, len(pending))

    sent = failed = 0
    for notification_id, subject, body in pending:
        message = EmailMessage()
        message["Subject"] = subject
        message["From"] = settings.sender
        message["To"] = settings.recipient
        message.set_content(body)
        try:
            with smtp_factory(settings.host, settings.port, timeout=30) as smtp:
                if settings.starttls:
                    smtp.starttls()
                if settings.user:
                    smtp.login(settings.user, settings.password)
                smtp.send_message(message)
        except (OSError, smtplib.SMTPException) as exc:
            failed += 1
            conn.execute(
                "UPDATE notifications SET attempts = attempts + 1, last_error = %s "
                "WHERE notification_id = %s",
                (f"{type(exc).__name__}: {exc}"[:500], notification_id),
            )
            continue
        sent += 1
        conn.execute(
            "UPDATE notifications SET status = 'sent', sent_at = %s, attempts = attempts + 1, "
            "last_error = NULL WHERE notification_id = %s",
            (now, notification_id),
        )
    return SendResult(True, sent, failed, len(pending) - sent)
