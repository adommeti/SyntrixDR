from __future__ import annotations

import uuid
from dataclasses import dataclass
from enum import Enum

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import AppError
from app.users_teams_org.models import Team
from app.users_teams_org.queries import list_active_roles


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
    async def can(
        session: AsyncSession, actor_id: uuid.UUID, capability: Capability, scope: Scope | None = None
    ) -> bool:
        scope = scope or Scope()
        roles = await list_active_roles(session, actor_id)

        if any(r.role_key == "GLOBAL_ADMIN" for r in roles):
            return True
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
