from __future__ import annotations

import uuid

import pyotp
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import AuditEvent
from app.identity_auth.commands import TotpAlreadyEnabledError, TotpInvalidError, enrol_totp, verify_totp
from app.identity_auth.models import LocalCredential
from app.identity_auth.security import generate_totp_secret, verify_totp_code

pytestmark = [pytest.mark.api, pytest.mark.auth, pytest.mark.integration]


@pytest.mark.asyncio
async def test_enrol_totp_returns_secret_and_provisioning_uri(
    session: AsyncSession, local_user_with_password: tuple[uuid.UUID, str]
) -> None:
    """Enrolling TOTP should return a secret and provisioning URI."""
    user_id, _ = local_user_with_password
    result = await enrol_totp(session, user_id=user_id)

    assert result.secret is not None
    assert isinstance(result.secret, str)
    assert len(result.secret) > 0
    assert result.provisioning_uri is not None
    assert isinstance(result.provisioning_uri, str)
    assert "otpauth://totp/" in result.provisioning_uri


@pytest.mark.asyncio
async def test_enrol_totp_keeps_totp_disabled(
    session: AsyncSession, local_user_with_password: tuple[uuid.UUID, str]
) -> None:
    """After enrolment, totp_enabled should remain False until verified."""
    user_id, _ = local_user_with_password
    await enrol_totp(session, user_id=user_id)

    # Fetch the credential and verify totp_enabled is still False
    result = await session.execute(select(LocalCredential).where(LocalCredential.user_id == user_id))
    cred = result.scalar_one_or_none()
    assert cred is not None
    assert cred.totp_enabled is False
    assert cred.totp_secret_encrypted is not None


@pytest.mark.asyncio
async def test_enrol_totp_writes_audit_event(
    session: AsyncSession, local_user_with_password: tuple[uuid.UUID, str]
) -> None:
    """Enrolling TOTP should write an AUTH_TOTP_ENROLLED audit event."""
    user_id, _ = local_user_with_password
    await enrol_totp(session, user_id=user_id)

    audit = (
        await session.execute(
            select(AuditEvent).where(
                (AuditEvent.actor_user_id == user_id) & (AuditEvent.action == "AUTH_TOTP_ENROLLED")
            )
        )
    ).scalar_one_or_none()

    assert audit is not None
    assert audit.entity_type == "USER"
    assert audit.entity_id == user_id


@pytest.mark.asyncio
async def test_verify_totp_with_correct_code(
    session: AsyncSession, local_user_with_password: tuple[uuid.UUID, str]
) -> None:
    """Verifying TOTP with the correct code should enable TOTP and set totp_enabled=True."""
    user_id, _ = local_user_with_password
    enrol_result = await enrol_totp(session, user_id=user_id)

    # Generate a valid TOTP code from the secret using pyotp
    totp = pyotp.TOTP(enrol_result.secret)
    valid_code = totp.now()

    await verify_totp(session, user_id=user_id, code=valid_code)

    # Fetch the credential and verify totp_enabled is now True
    result = await session.execute(select(LocalCredential).where(LocalCredential.user_id == user_id))
    cred = result.scalar_one_or_none()
    assert cred is not None
    assert cred.totp_enabled is True


@pytest.mark.asyncio
async def test_verify_totp_with_correct_code_writes_audit(
    session: AsyncSession, local_user_with_password: tuple[uuid.UUID, str]
) -> None:
    """Verifying TOTP with the correct code should write an AUTH_TOTP_VERIFIED audit event."""
    user_id, _ = local_user_with_password
    enrol_result = await enrol_totp(session, user_id=user_id)

    totp = pyotp.TOTP(enrol_result.secret)
    valid_code = totp.now()

    await verify_totp(session, user_id=user_id, code=valid_code)

    audit = (
        await session.execute(
            select(AuditEvent).where(
                (AuditEvent.actor_user_id == user_id) & (AuditEvent.action == "AUTH_TOTP_VERIFIED")
            )
        )
    ).scalar_one_or_none()

    assert audit is not None
    assert audit.entity_type == "USER"
    assert audit.entity_id == user_id


@pytest.mark.asyncio
async def test_verify_totp_with_incorrect_code_raises_error(
    session: AsyncSession, local_user_with_password: tuple[uuid.UUID, str]
) -> None:
    """Verifying TOTP with an incorrect code should raise TotpInvalidError."""
    user_id, _ = local_user_with_password
    await enrol_totp(session, user_id=user_id)

    invalid_code = "000000"

    with pytest.raises(TotpInvalidError):
        await verify_totp(session, user_id=user_id, code=invalid_code)


@pytest.mark.asyncio
async def test_verify_totp_with_incorrect_code_keeps_totp_disabled(
    session: AsyncSession, local_user_with_password: tuple[uuid.UUID, str]
) -> None:
    """After a failed TOTP verification, totp_enabled should remain False."""
    user_id, _ = local_user_with_password
    await enrol_totp(session, user_id=user_id)

    invalid_code = "000000"

    with pytest.raises(TotpInvalidError):
        await verify_totp(session, user_id=user_id, code=invalid_code)

    result = await session.execute(select(LocalCredential).where(LocalCredential.user_id == user_id))
    cred = result.scalar_one_or_none()
    assert cred is not None
    assert cred.totp_enabled is False


@pytest.mark.asyncio
async def test_verify_totp_with_incorrect_code_writes_audit(
    session: AsyncSession, local_user_with_password: tuple[uuid.UUID, str]
) -> None:
    """Failed TOTP verification should write an AUTH_TOTP_FAILED audit event."""
    user_id, _ = local_user_with_password
    await enrol_totp(session, user_id=user_id)

    invalid_code = "000000"

    with pytest.raises(TotpInvalidError):
        await verify_totp(session, user_id=user_id, code=invalid_code)

    audit = (
        await session.execute(
            select(AuditEvent).where(
                (AuditEvent.actor_user_id == user_id) & (AuditEvent.action == "AUTH_TOTP_FAILED")
            )
        )
    ).scalar_one_or_none()

    assert audit is not None
    assert audit.entity_type == "USER"
    assert audit.entity_id == user_id


@pytest.mark.asyncio
async def test_verify_totp_without_enrollment(
    session: AsyncSession, local_user_with_password: tuple[uuid.UUID, str]
) -> None:
    """Verifying TOTP without prior enrollment should raise TotpInvalidError."""
    user_id, _ = local_user_with_password
    valid_code = "123456"

    with pytest.raises(TotpInvalidError):
        await verify_totp(session, user_id=user_id, code=valid_code)


@pytest.mark.asyncio
async def test_verify_totp_already_enabled_raises_error(
    session: AsyncSession, local_user_with_password: tuple[uuid.UUID, str]
) -> None:
    """Verifying TOTP when already enabled should raise TotpInvalidError."""
    user_id, _ = local_user_with_password
    enrol_result = await enrol_totp(session, user_id=user_id)

    # Enable TOTP
    totp = pyotp.TOTP(enrol_result.secret)
    valid_code = totp.now()
    await verify_totp(session, user_id=user_id, code=valid_code)

    # Try to verify again
    with pytest.raises(TotpInvalidError):
        await verify_totp(session, user_id=user_id, code=valid_code)


@pytest.mark.asyncio
async def test_enrol_totp_rejects_re_enrolment_while_already_enabled(
    session: AsyncSession, local_user_with_password: tuple[uuid.UUID, str]
) -> None:
    """A hijacked session must not be able to silently replace/disable an active TOTP factor by
    calling enrol again — found in review: re-enrolling unconditionally overwrote the secret and
    set totp_enabled=False with no verification of the caller's possession of the current one."""
    user_id, _ = local_user_with_password
    enrol_result = await enrol_totp(session, user_id=user_id)
    totp = pyotp.TOTP(enrol_result.secret)
    await verify_totp(session, user_id=user_id, code=totp.now())

    original = (
        await session.execute(select(LocalCredential).where(LocalCredential.user_id == user_id))
    ).scalar_one()
    assert original.totp_enabled is True

    with pytest.raises(TotpAlreadyEnabledError):
        await enrol_totp(session, user_id=user_id)

    unchanged = (
        await session.execute(select(LocalCredential).where(LocalCredential.user_id == user_id))
    ).scalar_one()
    assert unchanged.totp_enabled is True
    assert unchanged.totp_secret_encrypted == original.totp_secret_encrypted


@pytest.mark.asyncio
async def test_security_module_verify_totp_code_with_valid_code() -> None:
    """Security module verify_totp_code should return True for a valid code."""
    secret = generate_totp_secret()
    totp = pyotp.TOTP(secret)
    valid_code = totp.now()

    assert verify_totp_code(secret, valid_code) is True


@pytest.mark.asyncio
async def test_security_module_verify_totp_code_with_invalid_code() -> None:
    """Security module verify_totp_code should return False for an invalid code."""
    secret = generate_totp_secret()

    assert verify_totp_code(secret, "000000") is False


@pytest.mark.asyncio
async def test_security_module_verify_totp_code_with_malformed_code() -> None:
    """Security module verify_totp_code should return False for a malformed code."""
    secret = generate_totp_secret()

    assert verify_totp_code(secret, "notacode") is False
    assert verify_totp_code(secret, "") is False
