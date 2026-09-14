"""One positive + one negative test per RBAC_MATRIX.md row, driven by the `GRANTS` data table
itself so every `Capability` member is covered without hand-writing 26 near-identical tests."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.identity_auth.models import LocalCredential
from app.identity_auth.security import PasswordHasher
from app.users_teams_org.authorization import (
    GRANTS,
    AuthorizationRequiredError,
    AuthorizationService,
    Capability,
    Scope,
)
from app.users_teams_org.models import RoleAssignment, Team

pytestmark = [pytest.mark.api, pytest.mark.auth]


async def _create_user(session: AsyncSession) -> uuid.UUID:
    user_id = uuid.uuid4()
    await session.execute(
        text(
            "INSERT INTO users (id, identity_type, display_name, email) "
            "VALUES (:id, 'LOCAL', 'Test User', :email)"
        ),
        {"id": user_id, "email": f"{user_id}@example.test"},
    )
    await session.flush()
    return user_id


async def _grant_role(
    session: AsyncSession,
    user_id: uuid.UUID,
    role_key: str,
    *,
    scope_type: str = "GLOBAL",
    scope_id: uuid.UUID | None = None,
) -> RoleAssignment:
    ra = RoleAssignment(user_id=user_id, role_key=role_key, scope_type=scope_type, scope_id=scope_id)
    session.add(ra)
    await session.flush()
    return ra


_SCOPE_TYPE_FOR_ROLE = {
    "DR_COORDINATOR": "DR_EVENT",
    "WORK_STREAM_LEAD": "WORK_STREAM",
    "APP_OWNER": "APPLICATION",
    "MANAGER": "APPLICATION",
    "EXECUTOR": "APPLICATION",
}


async def _grant_positive(
    session: AsyncSession, user_id: uuid.UUID, role_key: str, marker: bool | str
) -> Scope:
    """Grant `role_key` to `user_id` so `require()` should pass, and return the matching `Scope`."""
    if marker is True:
        await _grant_role(session, user_id, role_key)
        return Scope()
    if marker == "SCOPE":
        scope_id = uuid.uuid4()
        scope_type = _SCOPE_TYPE_FOR_ROLE.get(role_key, "APPLICATION")
        await _grant_role(session, user_id, role_key, scope_type=scope_type, scope_id=scope_id)
        return Scope(scope_type=scope_type, scope_id=scope_id)
    if marker == "OWN_TEAM":
        team_id = uuid.uuid4()
        session.add(Team(id=team_id, name=f"team-{team_id}", manager_user_id=user_id))
        await session.flush()
        await _grant_role(session, user_id, role_key)
        return Scope(owning_team_id=team_id)
    raise AssertionError(f"unhandled grant marker: {marker!r}")


@pytest.mark.asyncio
@pytest.mark.parametrize("capability", list(Capability))
async def test_authz_matrix_row(capability: Capability, session: AsyncSession) -> None:
    grant_row = GRANTS[capability]
    assert grant_row, f"{capability} has no grants in RBAC_MATRIX — every row grants at least Admin"
    role_key, marker = next(iter(grant_row.items()))

    positive_user = await _create_user(session)
    scope = await _grant_positive(session, positive_user, role_key, marker)

    assert await AuthorizationService.can(session, positive_user, capability, scope) is True
    await AuthorizationService.require(session, positive_user, capability, scope)

    # Negative: a user holding no role at all must always be denied, for every capability.
    negative_user = await _create_user(session)
    assert await AuthorizationService.can(session, negative_user, capability, scope) is False
    with pytest.raises(AuthorizationRequiredError):
        await AuthorizationService.require(session, negative_user, capability, scope)


@pytest.mark.asyncio
async def test_global_admin_bypasses_every_capability(session: AsyncSession) -> None:
    """GLOBAL_ADMIN is `✓` on every RBAC_MATRIX row — one generic bypass test, not 26 repeats."""
    admin = await _create_user(session)
    await _grant_role(session, admin, "GLOBAL_ADMIN")

    for capability in Capability:
        assert await AuthorizationService.can(session, admin, capability, Scope()) is True


@pytest.mark.asyncio
async def test_global_readonly_grants_only_the_read_capability(session: AsyncSession) -> None:
    reader = await _create_user(session)
    await _grant_role(session, reader, "GLOBAL_READONLY")

    assert (
        await AuthorizationService.can(session, reader, Capability.READ_EVENT_PARTICIPANT_CONTENT, Scope())
        is True
    )
    assert (
        await AuthorizationService.can(session, reader, Capability.EVENT_LIFECYCLE_COMMAND, Scope()) is False
    )


@pytest.mark.asyncio
async def test_scope_mismatch_denies_scope_marked_capability(session: AsyncSession) -> None:
    """A SCOPE-marked role holder is denied when checked against a DIFFERENT scope_id."""
    user_id = await _create_user(session)
    granted_scope_id = uuid.uuid4()
    await _grant_role(
        session, user_id, "WORK_STREAM_LEAD", scope_type="WORK_STREAM", scope_id=granted_scope_id
    )

    other_scope = Scope(scope_type="WORK_STREAM", scope_id=uuid.uuid4())
    assert (
        await AuthorizationService.can(session, user_id, Capability.CROSS_TEAM_ASSIGNMENT, other_scope)
        is False
    )


@pytest.mark.asyncio
async def test_own_team_denies_manager_of_a_different_team(session: AsyncSession) -> None:
    """OWN_TEAM-marked capability denies a Manager whose managed Team differs from the target."""
    manager_id = await _create_user(session)
    own_team_id = uuid.uuid4()
    other_team_id = uuid.uuid4()
    session.add(Team(id=own_team_id, name=f"team-{own_team_id}", manager_user_id=manager_id))
    session.add(Team(id=other_team_id, name=f"team-{other_team_id}", manager_user_id=None))
    await session.flush()
    await _grant_role(session, manager_id, "MANAGER")

    assert (
        await AuthorizationService.can(
            session, manager_id, Capability.OWN_TEAM_REASSIGNMENT, Scope(owning_team_id=other_team_id)
        )
        is False
    )


async def _create_local_admin(session: AsyncSession, *, totp_enabled: bool) -> uuid.UUID:
    user_id = await _create_user(session)
    hasher = PasswordHasher()
    session.add(
        LocalCredential(
            user_id=user_id,
            password_hash=hasher.hash_password("AdminPassword123!"),
            totp_enabled=totp_enabled,
            totp_secret_encrypted="fake-secret" if totp_enabled else None,
        )
    )
    await _grant_role(session, user_id, "GLOBAL_ADMIN")
    await session.flush()
    return user_id


async def _create_entra_admin(session: AsyncSession) -> uuid.UUID:
    user_id = uuid.uuid4()
    await session.execute(
        text(
            "INSERT INTO users (id, identity_type, display_name, email, entra_object_id) "
            "VALUES (:id, 'ENTRA', 'Test Admin', :email, :oid)"
        ),
        {"id": user_id, "email": f"{user_id}@example.test", "oid": str(uuid.uuid4())},
    )
    await _grant_role(session, user_id, "GLOBAL_ADMIN")
    await session.flush()
    return user_id


@pytest.mark.asyncio
async def test_local_global_admin_without_totp_loses_the_bypass(session: AsyncSession) -> None:
    """D-235: TOTP is mandatory for a LOCAL GLOBAL_ADMIN — without it enrolled, the role's
    implicit any-capability bypass must not apply (found in review: a LOCAL admin with the
    default `totp_enabled=False` otherwise got full privileges on password alone)."""
    admin_id = await _create_local_admin(session, totp_enabled=False)

    assert await AuthorizationService.is_global_admin(session, admin_id) is False
    assert (
        await AuthorizationService.can(session, admin_id, Capability.CREATE_LOCAL_FALLBACK_USER, Scope())
        is False
    )


@pytest.mark.asyncio
async def test_local_global_admin_with_totp_keeps_the_bypass(session: AsyncSession) -> None:
    admin_id = await _create_local_admin(session, totp_enabled=True)

    assert await AuthorizationService.is_global_admin(session, admin_id) is True
    assert (
        await AuthorizationService.can(session, admin_id, Capability.CREATE_LOCAL_FALLBACK_USER, Scope())
        is True
    )


@pytest.mark.asyncio
async def test_entra_global_admin_keeps_the_bypass_without_totp(session: AsyncSession) -> None:
    """The TOTP-effectiveness gate is LOCAL-only — Entra MFA is Entra's own responsibility."""
    admin_id = await _create_entra_admin(session)

    assert await AuthorizationService.is_global_admin(session, admin_id) is True
