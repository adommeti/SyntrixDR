from __future__ import annotations

import uuid
from dataclasses import dataclass
from enum import Enum

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import AppError
from app.identity_auth.models import LocalCredential
from app.users_teams_org.models import Team, User
from app.users_teams_org.queries import list_active_roles


async def _global_admin_role_is_effective(session: AsyncSession, actor_id: uuid.UUID) -> bool:
    """D-235: TOTP is mandatory for a LOCAL GLOBAL_ADMIN. A LOCAL admin who hasn't enrolled TOTP
    yet does not get GLOBAL_ADMIN's implicit any-capability bypass — found in review (an admin
    role with `totp_enabled=False`, the default, otherwise granted full privileges on password
    alone). They keep whatever other roles they hold and can still log in and enrol TOTP
    normally; only the privileged bypass is withheld until enrolment completes. ENTRA identities
    are out of scope here — their MFA is enforced by Entra Conditional Access, not this app."""
    user = await session.get(User, actor_id)
    if user is None or user.identity_type != "LOCAL":
        return True
    result = await session.execute(
        select(LocalCredential.totp_enabled).where(LocalCredential.user_id == actor_id)
    )
    totp_enabled = result.scalar_one_or_none()
    if totp_enabled is None:
        # No local_credentials row at all means this LOCAL identity has no password set and
        # cannot log in locally in the first place — the D-235 login-time concern this gate
        # targets doesn't apply yet. Gating on "no row" too would also silently deactivate
        # GLOBAL_ADMIN for any fixture/role-only user that never modeled a credential, which is
        # a different, much broader behavior than "this real account skipped TOTP enrolment".
        return True
    return totp_enabled


class Capability(str, Enum):
    """One member per RBAC_MATRIX.md row (docs/spec/RBAC_MATRIX.md)."""

    READ_EVENT_PARTICIPANT_CONTENT = "READ_EVENT_PARTICIPANT_CONTENT"
    GRANT_GLOBAL_READONLY = "GRANT_GLOBAL_READONLY"
    GLOBAL_POLICY_CONFIG = "GLOBAL_POLICY_CONFIG"
    CREATE_PLANNED_DR_EVENT = "CREATE_PLANNED_DR_EVENT"
    EVENT_LIFECYCLE_COMMAND = "EVENT_LIFECYCLE_COMMAND"
    MANAGE_EVENT_PARTICIPANTS = "MANAGE_EVENT_PARTICIPANTS"
    CROSS_TEAM_ASSIGNMENT = "CROSS_TEAM_ASSIGNMENT"
    OWN_TEAM_REASSIGNMENT = "OWN_TEAM_REASSIGNMENT"
    CHANGE_TASK_METADATA = "CHANGE_TASK_METADATA"
    CHANGE_DEPENDENCIES = "CHANGE_DEPENDENCIES"
    CONFIRM_SHARED_MILESTONE = "CONFIRM_SHARED_MILESTONE"
    VALIDATE_APPLICATION_WORK = "VALIDATE_APPLICATION_WORK"
    VALIDATE_SHARED_WORK_STREAM_WORK = "VALIDATE_SHARED_WORK_STREAM_WORK"
    VALIDATE_TASK_STANDARD = "VALIDATE_TASK_STANDARD"
    BLOCKER_CREATE_RESOLVE_VERIFY = "BLOCKER_CREATE_RESOLVE_VERIFY"
    BLOCKER_START_CLAIM = "BLOCKER_START_CLAIM"
    CREATE_ISSUE_FINDING = "CREATE_ISSUE_FINDING"
    COMMENT_MENTION_CROSS_TEAM = "COMMENT_MENTION_CROSS_TEAM"
    OVERRIDE_CROSS_WORK_STREAM_GUARD = "OVERRIDE_CROSS_WORK_STREAM_GUARD"
    OVERRIDE_WITHIN_WORK_STREAM = "OVERRIDE_WITHIN_WORK_STREAM"
    CLOSURE_EXCEPTION_OVERRIDE = "CLOSURE_EXCEPTION_OVERRIDE"
    RESOLVE_NEEDS_REVIEW = "RESOLVE_NEEDS_REVIEW"
    PUBLISH_OFFICIAL_REPORT = "PUBLISH_OFFICIAL_REPORT"
    EXPORT_IMPORT_FULL_PACKAGE = "EXPORT_IMPORT_FULL_PACKAGE"
    CREATE_LOCAL_FALLBACK_USER = "CREATE_LOCAL_FALLBACK_USER"
    AI_ACT = "AI_ACT"
    #: BUILD-03: RBAC_MATRIX.md has no row for Application/Tier master data — conservative
    #: reading, Admin-only, per the gap-filling convention in ADR-035.
    MANAGE_APPLICATION_CATALOG = "MANAGE_APPLICATION_CATALOG"
    MANAGE_TIERS = "MANAGE_TIERS"


#: Capabilities `GLOBAL_READONLY` may exercise (read-only; RBAC_MATRIX.md's only read-labeled row).
READ_ONLY_CAPABILITIES = frozenset({Capability.READ_EVENT_PARTICIPANT_CONTENT})

#: Grant markers:
#:   True    — granted at any scope the role holds (or unconditionally for GLOBAL-only capabilities).
#:   "SCOPE" — granted only when the actor holds the role at the EXACT scope_type/scope_id passed to
#:             require() (RBAC_MATRIX qualifiers like "stream", "app", "in scope", "eligible").
#:   "OWN_TEAM" — MANAGER only: granted when `scope.owning_team_id` is a Team the actor manages
#:             (`Team.manager_user_id`), since there is no TEAM `role_scope_type` to hold a role at.
#: Absent/False — RBAC_MATRIX marks it "no"/"—", or the cell's qualifier ("volunteer only", "never
#:             self-complete", "if authorized", "optional business note") needs domain context this
#:             service doesn't have (the object doesn't exist as a modeled entity yet) — the *owning*
#:             transition service enforces that nuance when it lands (see plan Risk #1). Denying by
#:             default here is the conservative reading, not a gap.
GRANTS: dict[Capability, dict[str, bool | str]] = {
    Capability.READ_EVENT_PARTICIPANT_CONTENT: {
        "GLOBAL_ADMIN": True,
        "DR_COORDINATOR": True,
        "WORK_STREAM_LEAD": True,
        "APP_OWNER": True,
        "BUSINESS_OWNER": True,
        "MANAGER": True,
        "EXECUTOR": True,
    },
    Capability.GRANT_GLOBAL_READONLY: {"GLOBAL_ADMIN": True, "DR_COORDINATOR": True},
    Capability.GLOBAL_POLICY_CONFIG: {"GLOBAL_ADMIN": True, "DR_COORDINATOR": "SCOPE"},
    Capability.CREATE_PLANNED_DR_EVENT: {
        "GLOBAL_ADMIN": True,
        "DR_COORDINATOR": True,
        "APP_OWNER": "SCOPE",
    },
    Capability.EVENT_LIFECYCLE_COMMAND: {"GLOBAL_ADMIN": True, "DR_COORDINATOR": True},
    Capability.MANAGE_EVENT_PARTICIPANTS: {"GLOBAL_ADMIN": True, "DR_COORDINATOR": True},
    Capability.CROSS_TEAM_ASSIGNMENT: {
        "GLOBAL_ADMIN": True,
        "DR_COORDINATOR": True,
        "WORK_STREAM_LEAD": "SCOPE",
    },
    Capability.OWN_TEAM_REASSIGNMENT: {
        "GLOBAL_ADMIN": True,
        "DR_COORDINATOR": True,
        "WORK_STREAM_LEAD": "SCOPE",
        "APP_OWNER": "SCOPE",
        "MANAGER": "OWN_TEAM",
    },
    Capability.CHANGE_TASK_METADATA: {
        "GLOBAL_ADMIN": True,
        "DR_COORDINATOR": True,
        "WORK_STREAM_LEAD": "SCOPE",
        "APP_OWNER": "SCOPE",
        "MANAGER": "OWN_TEAM",
    },
    Capability.CHANGE_DEPENDENCIES: {
        "GLOBAL_ADMIN": True,
        "DR_COORDINATOR": True,
        "WORK_STREAM_LEAD": "SCOPE",
        "APP_OWNER": "SCOPE",
        "MANAGER": "OWN_TEAM",
    },
    Capability.CONFIRM_SHARED_MILESTONE: {
        "GLOBAL_ADMIN": True,
        "DR_COORDINATOR": True,
        "WORK_STREAM_LEAD": True,
    },
    Capability.VALIDATE_APPLICATION_WORK: {
        "GLOBAL_ADMIN": True,
        "DR_COORDINATOR": True,
        "APP_OWNER": "SCOPE",
    },
    Capability.VALIDATE_SHARED_WORK_STREAM_WORK: {
        "GLOBAL_ADMIN": True,
        "DR_COORDINATOR": True,
        "WORK_STREAM_LEAD": True,
    },
    Capability.VALIDATE_TASK_STANDARD: {
        "GLOBAL_ADMIN": True,
        "DR_COORDINATOR": True,
        "WORK_STREAM_LEAD": "SCOPE",
        "APP_OWNER": "SCOPE",
    },
    Capability.BLOCKER_CREATE_RESOLVE_VERIFY: {
        "GLOBAL_ADMIN": True,
        "DR_COORDINATOR": True,
        "WORK_STREAM_LEAD": True,
        "APP_OWNER": True,
        "MANAGER": "SCOPE",
        "EXECUTOR": "SCOPE",
    },
    Capability.BLOCKER_START_CLAIM: {
        "GLOBAL_ADMIN": True,
        "DR_COORDINATOR": True,
        "WORK_STREAM_LEAD": "SCOPE",
        "APP_OWNER": "SCOPE",
        "MANAGER": "SCOPE",
        "EXECUTOR": "SCOPE",
    },
    Capability.CREATE_ISSUE_FINDING: {
        "GLOBAL_ADMIN": True,
        "DR_COORDINATOR": True,
        "WORK_STREAM_LEAD": True,
        "APP_OWNER": True,
        "MANAGER": True,
        "EXECUTOR": True,
    },
    Capability.COMMENT_MENTION_CROSS_TEAM: {
        "GLOBAL_ADMIN": True,
        "DR_COORDINATOR": True,
        "WORK_STREAM_LEAD": True,
        "APP_OWNER": True,
        "BUSINESS_OWNER": True,
        "MANAGER": True,
        "EXECUTOR": True,
    },
    Capability.OVERRIDE_CROSS_WORK_STREAM_GUARD: {"GLOBAL_ADMIN": True, "DR_COORDINATOR": True},
    Capability.OVERRIDE_WITHIN_WORK_STREAM: {
        "GLOBAL_ADMIN": True,
        "DR_COORDINATOR": True,
        "WORK_STREAM_LEAD": "SCOPE",
    },
    Capability.CLOSURE_EXCEPTION_OVERRIDE: {"GLOBAL_ADMIN": True, "DR_COORDINATOR": True},
    Capability.RESOLVE_NEEDS_REVIEW: {
        "GLOBAL_ADMIN": True,
        "DR_COORDINATOR": True,
        "WORK_STREAM_LEAD": "SCOPE",
        "APP_OWNER": "SCOPE",
        "MANAGER": "SCOPE",
        "EXECUTOR": "SCOPE",
    },
    Capability.PUBLISH_OFFICIAL_REPORT: {"GLOBAL_ADMIN": True, "DR_COORDINATOR": True},
    Capability.EXPORT_IMPORT_FULL_PACKAGE: {"GLOBAL_ADMIN": True, "DR_COORDINATOR": "SCOPE"},
    Capability.CREATE_LOCAL_FALLBACK_USER: {"GLOBAL_ADMIN": True},
    Capability.MANAGE_APPLICATION_CATALOG: {"GLOBAL_ADMIN": True},
    Capability.MANAGE_TIERS: {"GLOBAL_ADMIN": True},
    Capability.AI_ACT: {
        "GLOBAL_ADMIN": True,
        "DR_COORDINATOR": True,
        "WORK_STREAM_LEAD": True,
        "APP_OWNER": True,
        "BUSINESS_OWNER": True,
        "MANAGER": True,
        "EXECUTOR": True,
    },
}


@dataclass(frozen=True)
class Scope:
    """The object an authorization check is against. `scope_type`/`scope_id` mirror
    `role_assignments.scope_type`/`scope_id` (GLOBAL/DR_EVENT/WORK_STREAM/APPLICATION).
    `owning_team_id` is set only for capabilities checked against `Team.manager_user_id`
    (the "OWN_TEAM" grant marker) since there is no TEAM `role_scope_type`."""

    scope_type: str | None = None
    scope_id: uuid.UUID | None = None
    owning_team_id: uuid.UUID | None = None


class AuthorizationRequiredError(AppError):
    code = "AUTHORIZATION_REQUIRED"
    status_code = 403

    def __init__(self) -> None:
        super().__init__("You do not have permission to perform this action.")


class AuthorizationService:
    """`require(actor, capability, scope)` implementing RBAC_MATRIX.md as data (see `GRANTS`)."""

    @staticmethod
    async def is_global_admin(session: AsyncSession, actor_id: uuid.UUID) -> bool:
        """True iff `actor_id` holds an effective GLOBAL_ADMIN role — the single source of truth
        for the "implicit everywhere" Global Admin rule (D-222's participant visibility,
        `can()`'s bypass, etc.), so the TOTP-effectiveness gate (D-235) can't be bypassed by
        calling `has_active_role` directly instead of going through this service."""
        roles = await list_active_roles(session, actor_id)
        return any(r.role_key == "GLOBAL_ADMIN" for r in roles) and await _global_admin_role_is_effective(
            session, actor_id
        )

    @staticmethod
    async def is_global_readonly(session: AsyncSession, actor_id: uuid.UUID) -> bool:
        """True iff `actor_id` holds the GLOBAL_READONLY role — RBAC_MATRIX.md's only
        read-labeled row (D-222: Auditor/Executive visibility via a separately granted global
        read-only role, not per-Event enrolment). Callers needing Event visibility (not the
        broader per-capability grants in `can()`) should use this, not `can()` directly — found
        in review that `dr_events.participants.user_can_see_event` didn't check this at all."""
        roles = await list_active_roles(session, actor_id)
        return any(r.role_key == "GLOBAL_READONLY" for r in roles)

    @staticmethod
    async def can(
        session: AsyncSession, actor_id: uuid.UUID, capability: Capability, scope: Scope | None = None
    ) -> bool:
        scope = scope or Scope()
        roles = await list_active_roles(session, actor_id)

        if any(r.role_key == "GLOBAL_ADMIN" for r in roles):
            if await _global_admin_role_is_effective(session, actor_id):
                return True
            # Not effective (D-235): treat as if the GLOBAL_ADMIN role isn't held at all — must
            # be filtered out of `roles` here too, not just skipped in this early-return, or the
            # per-capability grant_row loop below (which also lists "GLOBAL_ADMIN": True on every
            # row, mirroring RBAC_MATRIX's ✓ column) would grant it anyway. Found in review's own
            # follow-up: the first fix only blocked this early return, not the loop.
            roles = [r for r in roles if r.role_key != "GLOBAL_ADMIN"]
        if capability in READ_ONLY_CAPABILITIES and any(r.role_key == "GLOBAL_READONLY" for r in roles):
            return True

        grant_row = GRANTS.get(capability, {})
        for role in roles:
            marker = grant_row.get(role.role_key)
            if marker is True:
                return True
            if marker == "SCOPE":
                if (
                    scope.scope_type is not None
                    and role.scope_type == scope.scope_type
                    and role.scope_id == scope.scope_id
                ):
                    return True
            elif marker == "OWN_TEAM" and role.role_key == "MANAGER" and scope.owning_team_id is not None:
                team_result = await session.get(Team, scope.owning_team_id)
                if team_result is not None and team_result.manager_user_id == actor_id:
                    return True
        return False

    @staticmethod
    async def require(
        session: AsyncSession, actor_id: uuid.UUID, capability: Capability, scope: Scope | None = None
    ) -> None:
        if not await AuthorizationService.can(session, actor_id, capability, scope):
            raise AuthorizationRequiredError()
