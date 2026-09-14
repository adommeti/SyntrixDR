from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import write_audit
from app.core.clock import Clock, SystemClock
from app.identity_auth.models import LocalCredential
from app.identity_auth.security import PasswordHasher, validate_password_policy
from app.users_teams_org.authorization import AuthorizationService, Capability
from app.users_teams_org.models import RoleAssignment, User


@dataclass
class CreateLocalUserResult:
    user_id: uuid.UUID


async def create_local_user(
    session: AsyncSession,
    *,
    actor_id: uuid.UUID,
    display_name: str,
    email: str,
    password: str,
) -> CreateLocalUserResult:
    """Explicitly create a Local fallback user (D-235: no production Local account is pre-created;
    this is the deliberate creation path). Global-Admin-only, high-risk (D-228) — the route also
    guards CSRF + `require_reauth`; `AuthorizationService` is the RBAC check, not a substitute for
    those session-layer guards.
    """
    await AuthorizationService.require(session, actor_id, Capability.CREATE_LOCAL_FALLBACK_USER)
    validate_password_policy(password)

    user = User(display_name=display_name, email=email.lower(), identity_type="LOCAL", is_active=True)
    session.add(user)
    await session.flush()

    hasher = PasswordHasher()
    credential = LocalCredential(user_id=user.id, password_hash=hasher.hash_password(password))
    session.add(credential)

    await write_audit(
        session,
        actor_user_id=actor_id,
        entity_type="USER",
        entity_id=user.id,
        action="AUTH_LOCAL_USER_CREATED",
    )
    # Does not commit: the route commits once after `complete()` so the user row, audit row, and
    # idempotency-key completion land in one transaction (D-215).
    return CreateLocalUserResult(user_id=user.id)


async def grant_role(
    session: AsyncSession,
    *,
    actor_id: uuid.UUID,
    user_id: uuid.UUID,
    role_key: str,
    scope_type: str = "GLOBAL",
    scope_id: uuid.UUID | None = None,
) -> RoleAssignment:
    """Grant `role_key` to `user_id` at the given scope. When `scope_type == "DR_EVENT"`, this is
    also a D-222 auto-enrolment trigger (source='ROLE') — the only auto-enrol source wired
    end-to-end this session; see plan Risk #2 for the other five sources' status.

    No route exposes this yet (RBAC_MATRIX/API_CONTRACT name no "assign role" endpoint) — it is a
    command-layer primitive for whichever module owns role administration to call.
    """
    assignment = RoleAssignment(
        user_id=user_id,
        role_key=role_key,
        scope_type=scope_type,
        scope_id=scope_id,
        granted_by_user_id=actor_id,
    )
    session.add(assignment)
    await session.flush()

    if scope_type == "DR_EVENT" and scope_id is not None:
        from app.dr_events.participants import enrol_participant

        await enrol_participant(session, scope_id, user_id, "ROLE", added_by_user_id=actor_id)

    await write_audit(
        session,
        actor_user_id=actor_id,
        entity_type="ROLE_ASSIGNMENT",
        entity_id=assignment.id,
        action="ROLE_GRANTED",
        metadata={"role_key": role_key, "scope_type": scope_type, "user_id": str(user_id)},
    )
    return assignment


async def revoke_role(
    session: AsyncSession,
    *,
    actor_id: uuid.UUID,
    role_assignment_id: uuid.UUID,
    clock: Clock | None = None,
) -> None:
    """Revoke an active role assignment (soft: sets `revoked_at`)."""
    clock = clock or SystemClock()
    assignment = await session.get(RoleAssignment, role_assignment_id)
    if assignment is None or assignment.revoked_at is not None:
        return
    assignment.revoked_at = clock.now()
    await write_audit(
        session,
        actor_user_id=actor_id,
        entity_type="ROLE_ASSIGNMENT",
        entity_id=assignment.id,
        action="ROLE_REVOKED",
        metadata={"role_key": assignment.role_key},
    )
