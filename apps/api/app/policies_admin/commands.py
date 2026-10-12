from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import write_audit
from app.core.clock import Clock, SystemClock
from app.core.errors import AppError
from app.core.file_policy import FilePolicy, get_file_policy
from app.policies_admin.models import PolicyDefinition, PolicyValue
from app.policies_admin.queries import PolicyService, get_policy_definition
from app.users_teams_org.authorization import AuthorizationService, Capability, Scope
from app.validation_evidence.sniff import CANONICAL_MIME

KNOWN_EXTENSIONS = frozenset(CANONICAL_MIME)

#: schema_v2_reconciliation.sql:434 -- fixed at HARD_STOP, not configurable; the service must
#: reject writes to this key regardless of actor or scope (BUILD-03.plan.md Risk #4).
_LOCKED_KEYS = frozenset({"readiness.dependency_graph_acyclic"})


class PolicyKeyNotFoundError(AppError):
    code = "POLICY_KEY_NOT_FOUND"
    status_code = 404

    def __init__(self, key: str) -> None:
        super().__init__(f"Unknown policy key: {key}")


class PolicyKeyNotConfigurableError(AppError):
    code = "POLICY_KEY_NOT_CONFIGURABLE"
    status_code = 422

    def __init__(self, key: str) -> None:
        super().__init__(f"Policy key {key!r} is fixed and cannot be overridden.")


async def resolve_all_authorized(
    session: AsyncSession,
    *,
    actor_id: uuid.UUID,
    event_id: uuid.UUID | None = None,
    work_stream_id: uuid.UUID | None = None,
    application_id: uuid.UUID | None = None,
) -> list[tuple[PolicyDefinition, Any]]:
    """`GET /admin/policies` at global scope (no ids passed) is open to any authenticated user --
    it's just the global defaults, not sensitive per-Event data. But an event/stream/application
    scope must not be readable by an arbitrary authenticated user who happens to know or guess
    its UUID: any actor could otherwise pass another Event's id and read its effective policy
    overrides with no visibility check at all (found in review). Reuse the same
    `GLOBAL_POLICY_CONFIG` capability writes are gated on -- Admin always, or a Coordinator
    scoped to that exact scope -- since this module has no Event-participant visibility of its
    own (that's dr_events', BUILD-04+) and inventing a parallel read-only grant here would be a
    product decision, not an engineering one."""
    for scope_type, scope_id in (
        ("DR_EVENT", event_id),
        ("WORK_STREAM", work_stream_id),
        ("APPLICATION", application_id),
    ):
        if scope_id is not None:
            await AuthorizationService.require(
                session,
                actor_id,
                Capability.GLOBAL_POLICY_CONFIG,
                Scope(scope_type=scope_type, scope_id=scope_id),
            )
    return await PolicyService.resolve_all(
        session, event_id=event_id, work_stream_id=work_stream_id, application_id=application_id
    )


async def set_policy_value(
    session: AsyncSession,
    *,
    actor_id: uuid.UUID,
    key: str,
    value: Any,
    scope_type: str,
    scope_id: uuid.UUID | None = None,
    clock: Clock | None = None,
) -> PolicyValue:
    await AuthorizationService.require(
        session,
        actor_id,
        Capability.GLOBAL_POLICY_CONFIG,
        Scope(scope_type=scope_type, scope_id=scope_id),
    )

    if key in _LOCKED_KEYS:
        raise PolicyKeyNotConfigurableError(key)

    clock = clock or SystemClock()
    definition = await get_policy_definition(session, key)
    if definition is None:
        raise PolicyKeyNotFoundError(key)

    # Serialize concurrent writes to the same (definition, scope) for the rest of this
    # transaction (released automatically at commit/rollback) -- without this, two concurrent
    # first-writes (both see `previous is None`) or two concurrent supersedes under READ
    # COMMITTED could both commit, leaving two simultaneously-active rows for the same
    # key/scope, violating invariant #11/#12 (found in review). There is no partial unique
    # index to lean on instead since `policy_values` has no `superseded_at IS NULL` constraint
    # in the frozen schema (schema_v1.sql:588) and this module cannot add one (ADR-036).
    lock_key = f"{definition.id}:{scope_type}:{scope_id}"
    await session.execute(text("SELECT pg_advisory_xact_lock(hashtext(:lock_key))"), {"lock_key": lock_key})

    now = clock.now()
    scope_id_clause = PolicyValue.scope_id.is_(None) if scope_id is None else PolicyValue.scope_id == scope_id
    existing_result = await session.execute(
        select(PolicyValue).where(
            PolicyValue.policy_definition_id == definition.id,
            PolicyValue.scope_type == scope_type,
            scope_id_clause,
            PolicyValue.superseded_at.is_(None),
        )
    )
    previous = existing_result.scalar_one_or_none()
    before = {"value": previous.value} if previous is not None else None
    if previous is not None:
        previous.superseded_at = now

    new_row = PolicyValue(
        policy_definition_id=definition.id,
        scope_type=scope_type,
        scope_id=scope_id,
        value=value,
        changed_by_user_id=actor_id,
    )
    session.add(new_row)
    await session.flush()

    await write_audit(
        session,
        actor_user_id=actor_id,
        entity_type="POLICY_VALUE",
        entity_id=new_row.id,
        action="POLICY_VALUE_SET",
        before=before,
        after={"value": value},
        metadata={
            "key": key,
            "scope_type": scope_type,
            "scope_id": str(scope_id) if scope_id is not None else None,
        },
    )
    return new_row


# --------------------------------------------------------------------------------------------
# File policy (API_CONTRACT.md:266; D-225, D-245). One singleton row; the `file.max_mb` /
# `file.allowlist` policy values are kept in step (schema_v2_reconciliation.sql:352).
# --------------------------------------------------------------------------------------------


class FilePolicyValueInvalidError(AppError):
    code = "FILE_POLICY_VALUE_INVALID"
    status_code = 422

    def __init__(self, field: str, problem: str) -> None:
        super().__init__(f"{field} {problem}.", details={"field": field})


class UnknownExtensionError(AppError):
    code = "UNKNOWN_EXTENSION"
    status_code = 422

    def __init__(self, extensions: list[str]) -> None:
        super().__init__(
            "Only extensions the server can content-check may be allowed.",
            details={"unknown": extensions, "known": sorted(KNOWN_EXTENSIONS)},
        )


async def update_file_policy(
    session: AsyncSession,
    *,
    actor_id: uuid.UUID,
    max_mb: int,
    allowed_extensions: list[str],
    scanning_required: bool,
    clock: Clock | None = None,
) -> FilePolicy:
    """Global Admin only (`GLOBAL_POLICY_CONFIG` unscoped). `allowed_mime_types` are derived from the
    extensions through the sniffer's canonical map so the two lists can never disagree (BUILD-09.plan.md
    Risk B5); the policy values mirror the row for the policy UI."""
    clock = clock or SystemClock()
    await AuthorizationService.require(session, actor_id, Capability.GLOBAL_POLICY_CONFIG, Scope())
    extensions = sorted({e.strip().lower().lstrip(".") for e in allowed_extensions if e.strip()})
    unknown = [e for e in extensions if e not in KNOWN_EXTENSIONS]
    if unknown:
        raise UnknownExtensionError(unknown)
    if max_mb < 1:
        raise FilePolicyValueInvalidError("max_mb", "must be at least 1")

    policy = await get_file_policy(session, for_update=True)  # serialise concurrent admin edits
    before = {
        "max_bytes": policy.max_bytes,
        "allowed_extensions": list(policy.allowed_extensions),
        "scanning_required": policy.scanning_required,
    }
    policy.max_bytes = max_mb * 1024 * 1024
    policy.allowed_extensions = extensions
    policy.allowed_mime_types = sorted({CANONICAL_MIME[e] for e in extensions})
    policy.scanning_required = scanning_required
    policy.updated_by_user_id = actor_id
    policy.updated_at = clock.now()
    await session.flush()
    await write_audit(
        session,
        actor_user_id=actor_id,
        entity_type="FILE_POLICY",
        entity_id=policy.id,
        action="FILE_POLICY_UPDATED",
        before=before,
        after={
            "max_bytes": policy.max_bytes,
            "allowed_extensions": extensions,
            "allowed_mime_types": policy.allowed_mime_types,
            "scanning_required": scanning_required,
        },
    )
    await set_policy_value(
        session, actor_id=actor_id, key="file.max_mb", value=max_mb, scope_type="GLOBAL", clock=clock
    )
    await set_policy_value(
        session, actor_id=actor_id, key="file.allowlist", value=extensions, scope_type="GLOBAL", clock=clock
    )
    return policy
