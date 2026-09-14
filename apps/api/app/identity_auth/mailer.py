from __future__ import annotations

import asyncio
import smtplib
from email.mime.text import MIMEText

import structlog

from app.core.config import get_settings

logger = structlog.get_logger(__name__)


def _send_sync(to: str, subject: str, body: str) -> None:
    settings = get_settings()
    message = MIMEText(body)
    message["Subject"] = subject
    message["From"] = settings.smtp_from_address
    message["To"] = to

    server = smtplib.SMTP(settings.smtp_host, settings.smtp_port)
    try:
        server.sendmail(settings.smtp_from_address, [to], message.as_string())
    finally:
        server.quit()


async def send_password_reset_email(to: str, token: str) -> None:
    """Send a password reset email via SMTP.

    Args:
        to: Recipient email address
        token: The password reset token (already hashed in DB, this is the plaintext for the link)
    """
    settings = get_settings()

    subject = "Reset Your SyntrixDR Password"
    reset_url = f"{settings.frontend_public_url}/auth/reset-password?token={token}"
    body = f"""
Hello,

You requested a password reset. Click the link below to set a new password:

{reset_url}

This link will expire in {settings.password_reset_token_ttl_minutes} minutes.

If you did not request this, ignore this email.

Best regards,
SyntrixDR Team
"""

    # smtplib is blocking; run off the event loop rather than stalling the request path
    # (python-api.md: async everywhere on the request path). No Celery task queue for this
    # yet — request_password_reset always returns 202 regardless (no enumeration), so a send
    # failure must not raise; it's logged instead so it's visible to ops.
    try:
        await asyncio.to_thread(_send_sync, to, subject, body)
    except Exception:
        logger.warning("password_reset_email_send_failed", to_domain=to.rsplit("@", 1)[-1])
