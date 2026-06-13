"""Email notifications for nightspot.

Fire-and-forget alerts to the installation owner whenever someone uses the
service (a deposit, a question, a signup). Uses stdlib smtplib so there's no
new dependency.

Configured entirely by environment variables; if SMTP isn't configured it's a
silent (logged) no-op, so dev runs and the test suite never touch the network:

    SMTP_HOST   smtp.gmail.com           (required to enable)
    SMTP_PORT   587 (STARTTLS) or 465 (SSL)
    SMTP_USER   the login / sending address
    SMTP_PASS   the password or app-password
    NOTIFY_FROM defaults to SMTP_USER
    NOTIFY_TO   where alerts go (required to enable)

Sends happen on a daemon thread and swallow all errors, so a flaky mail server
can never delay or break a visitor's request.
"""
import logging
import os
import smtplib
import ssl
import threading
from email.message import EmailMessage

log = logging.getLogger("nightspot.notify")

SMTP_HOST = os.environ.get("SMTP_HOST", "")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))
SMTP_USER = os.environ.get("SMTP_USER", "")
SMTP_PASS = os.environ.get("SMTP_PASS", "")
NOTIFY_FROM = os.environ.get("NOTIFY_FROM", "") or SMTP_USER
NOTIFY_TO = os.environ.get("NOTIFY_TO", "")


def configured() -> bool:
    return bool(SMTP_HOST and NOTIFY_TO and SMTP_USER)


def _deliver(subject: str, body: str) -> None:
    try:
        msg = EmailMessage()
        msg["Subject"] = subject
        msg["From"] = NOTIFY_FROM or SMTP_USER
        msg["To"] = NOTIFY_TO
        msg.set_content(body)
        if SMTP_PORT == 465:
            ctx = ssl.create_default_context()
            with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, context=ctx,
                                  timeout=15) as srv:
                srv.login(SMTP_USER, SMTP_PASS)
                srv.send_message(msg)
        else:
            with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=15) as srv:
                srv.starttls(context=ssl.create_default_context())
                srv.login(SMTP_USER, SMTP_PASS)
                srv.send_message(msg)
        log.info("notify sent: %s", subject)
    except Exception as e:  # never let mail trouble surface into the flow
        log.warning("notify failed (%s): %s", subject, e)


def send(subject: str, body: str) -> None:
    """Queue an email. Returns immediately; no-op (logged) if unconfigured."""
    if not configured():
        log.info("notify skipped (SMTP not configured): %s", subject)
        return
    threading.Thread(target=_deliver, args=(subject, body),
                     daemon=True).start()
