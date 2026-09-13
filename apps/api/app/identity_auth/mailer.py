from __future__ import annotations

import smtplib
from email.mime.text import MIMEText

from app.core.config import get_settings


async def send_password_reset_email(to: str, token: str) -> None:
    """Send a password reset email via SMTP.

    Args:
        to: Recipient email address
        token: The password reset token (already hashed in DB, this is the plaintext for the link)
    """
    settings = get_settings()

    subject = "Reset Your SyntrixDR Password"
    reset_url = f"http://localhost:3000/auth/reset-password?token={token}"
    body = f"""
Hello,

You requested a password reset. Click the link below to set a new password:

{reset_url}

This link will expire in {settings.password_reset_token_ttl_minutes} minutes.

If you did not request this, ignore this email.

Best regards,
SyntrixDR Team
"""

    message = MIMEText(body)
    message["Subject"] = subject
    message["From"] = settings.smtp_from_address
    message["To"] = to

    # Use synchronous SMTP (blocking); for high throughput, move to Celery task queue
    try:
        server = smtplib.SMTP(settings.smtp_host, settings.smtp_port)
        server.sendmail(settings.smtp_from_address, [to], message.as_string())
        server.quit()
    except Exception:
        # In local dev (Mailpit), mail send failures are rare but silent in logs
        pass
