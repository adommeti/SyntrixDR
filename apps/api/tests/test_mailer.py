from __future__ import annotations

import pytest

from app.core.config import Settings
from app.identity_auth import mailer

pytestmark = [pytest.mark.api, pytest.mark.auth]


@pytest.mark.asyncio
async def test_password_reset_email_uses_configured_frontend_url(monkeypatch: pytest.MonkeyPatch) -> None:
    """The reset link must come from settings.frontend_public_url, not a hardcoded
    http://localhost:3000 — a production deployment with a real domain (and HTTPS) would
    otherwise mail users a dead/insecure link (found in review)."""
    configured_settings = Settings(frontend_public_url="https://dr.example.com")
    monkeypatch.setattr(mailer, "get_settings", lambda: configured_settings)

    sent_bodies: list[str] = []

    def _fake_send_sync(to: str, subject: str, body: str) -> None:
        sent_bodies.append(body)

    monkeypatch.setattr(mailer, "_send_sync", _fake_send_sync)

    await mailer.send_password_reset_email("user@example.test", "sometoken")

    assert len(sent_bodies) == 1
    assert "https://dr.example.com/auth/reset-password?token=sometoken" in sent_bodies[0]
    assert "localhost:3000" not in sent_bodies[0]
