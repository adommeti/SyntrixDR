from __future__ import annotations

import uuid
from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.applications_catalog.models import Application, ApplicationOwner, Tier
from app.core.audit import write_audit
from app.core.clock import Clock, SystemClock
from app.core.errors import AppError, ConcurrencyConflictError
from app.users_teams_org.authorization import AuthorizationService, Capability
from app.users_teams_org.queries import is_user_active


class _Unset:
    """Sentinel distinguishing 'field omitted from the PATCH payload' from an explicit
    `null`, so `description`/`external_system`/`external_id` can be cleared (found in
    review: treating explicit null the same as omitted made nullable fields un-clearable)."""

    def __repr__(self) -> str:
        return "UNSET"


UNSET = _Unset()


class ApplicationNameConflictError(AppError):
    code = "APPLICATION_NAME_CONFLICT"
    status_code = 409

    def __init__(self) -> None:
        super().__init__("An Application with this name already exists.")


class ApplicationNotFoundError(AppError):
    code = "APPLICATION_NOT_FOUND"
    status_code = 404

    def __init__(self) -> None:
        super().__init__("Application not found.")


class TierNotFoundError(AppError):
    code = "TIER_NOT_FOUND"
    status_code = 404

    def __init__(self) -> None:
        super().__init__("Tier not found.")


class ApplicationOwnerLimitError(AppError):
    code = "APPLICATION_OWNER_LIMIT_EXCEEDED"
    status_code = 422

    def __init__(self) -> None:
        super().__init__("At most 3 owner slots are allowed per owner type (D-254).")


class ApplicationOwnerPrimaryRequiredError(AppError):
    code = "APPLICATION_OWNER_PRIMARY_REQUIRED"
    status_code = 422

    def __init__(self) -> None:
        super().__init__("Slot 1 (Primary) is required whenever any other slot for that owner type is set.")


class ApplicationOwnerUserNotFoundError(AppError):
    code = "APPLICATION_OWNER_USER_NOT_FOUND"
    status_code = 404

    def __init__(self) -> None:
        super().__init__("One or more owner user_id values do not reference an active user.")


class UnknownTierCodeError(AppError):
    code = "UNKNOWN_TIER_CODE"
    status_code = 404

    def __init__(self, code: str) -> None:
        super().__init__(f"Unknown tier code: {code}. V1 tiers are fixed; none can be added.")


@dataclass(frozen=True)
class OwnerSlot:
    owner_type: str
    owner_order: int
    user_id: uuid.UUID


@dataclass(frozen=True)
class TierUpdate:
    code: str
    expected_version: int
    default_sla_minutes: int
    default_health_weight: Decimal
    description: str | None = None


async def _find_by_name(
    session: AsyncSession, name: str, *, exclude_id: uuid.UUID | None = None
) -> Application | None:
    stmt = select(Application).where(
        Application.deleted_at.is_(None), func.lower(Application.name) == name.lower()
    )
    if exclude_id is not None:
        stmt = stmt.where(Application.id != exclude_id)
    result = await session.execute(stmt)
    return result.scalar_one_or_none()


async def create_application(
    session: AsyncSession,
    *,
    actor_id: uuid.UUID,
    name: str,
    tier_id: uuid.UUID,
    description: str | None = None,
    external_system: str | None = None,
    external_id: str | None = None,
) -> Application:
    await AuthorizationService.require(session, actor_id, Capability.MANAGE_APPLICATION_CATALOG)

    tier = await session.get(Tier, tier_id)
    if tier is None:
        raise TierNotFoundError()

    if await _find_by_name(session, name) is not None:
        raise ApplicationNameConflictError()

    application = Application(
        name=name,
        description=description,
        tier_id=tier_id,
        external_system=external_system,
        external_id=external_id,
    )
    session.add(application)
    await session.flush()

    await write_audit(
        session,
        actor_user_id=actor_id,
        entity_type="APPLICATION",
        entity_id=application.id,
        action="APPLICATION_CREATED",
        after={"name": name, "tier_id": str(tier_id)},
    )
    return application


async def update_application(
    session: AsyncSession,
    *,
    actor_id: uuid.UUID,
    application_id: uuid.UUID,
    expected_version: int,
    name: str | _Unset | None = UNSET,
    description: str | None | _Unset = UNSET,
    tier_id: uuid.UUID | _Unset | None = UNSET,
    external_system: str | None | _Unset = UNSET,
    external_id: str | None | _Unset = UNSET,
    clock: Clock | None = None,
) -> Application:
    """D-214: a stale `expected_version` write is rejected with 409 CONCURRENCY_CONFLICT
    (invariant #22) — Application master data has no Manager-precedence carve-out."""
    clock = clock or SystemClock()
    await AuthorizationService.require(session, actor_id, Capability.MANAGE_APPLICATION_CATALOG)

    # `with_for_update=True` closes a race two sequential reads alone can't: without the row
    # lock, two concurrent PATCH requests can both read version=N, both pass this check, and
    # both commit as version=N+1, silently discarding one of them (found in review). The lock
    # forces the second transaction to block until the first commits, so it re-reads the
    # already-incremented version and correctly hits the mismatch below.
    application = await session.get(Application, application_id, with_for_update=True)
    if application is None:
        raise ApplicationNotFoundError()
    if application.version != expected_version:
        raise ConcurrencyConflictError()

    before = {
        "name": application.name,
        "description": application.description,
        "tier_id": str(application.tier_id),
        "external_system": application.external_system,
        "external_id": application.external_id,
        "version": application.version,
    }

    if isinstance(name, str) and name != application.name:
        if await _find_by_name(session, name, exclude_id=application_id) is not None:
            raise ApplicationNameConflictError()
        application.name = name
    if not isinstance(description, _Unset):
        application.description = description
    if isinstance(tier_id, uuid.UUID):
        if await session.get(Tier, tier_id) is None:
            raise TierNotFoundError()
        application.tier_id = tier_id
    if not isinstance(external_system, _Unset):
        application.external_system = external_system
    if not isinstance(external_id, _Unset):
        application.external_id = external_id

    application.version += 1
    application.updated_at = clock.now()
    await session.flush()

    await write_audit(
        session,
        actor_user_id=actor_id,
        entity_type="APPLICATION",
        entity_id=application.id,
        action="APPLICATION_UPDATED",
        before=before,
        after={
            "name": application.name,
            "description": application.description,
            "tier_id": str(application.tier_id),
            "external_system": application.external_system,
            "external_id": application.external_id,
            "version": application.version,
        },
    )
    return application


async def set_application_owners(
    session: AsyncSession,
    *,
    actor_id: uuid.UUID,
    application_id: uuid.UUID,
    owners: list[OwnerSlot],
    clock: Clock | None = None,
) -> list[ApplicationOwner]:
    """Full-replace semantics: the slot set in `owners` becomes the sole active set for the
    Application. `application_owner_slot_unique` is NOT partial (spans soft-deleted rows too —
    schema_v1.sql:183), so a slot that already has a row (active or soft-deleted) must be
    UPDATEd in place, never re-INSERTed, or the unique constraint on
    (application_id, owner_type, owner_order) rejects the write."""
    clock = clock or SystemClock()
    await AuthorizationService.require(session, actor_id, Capability.MANAGE_APPLICATION_CATALOG)

    application = await session.get(Application, application_id)
    if application is None:
        raise ApplicationNotFoundError()

    by_type: dict[str, set[int]] = {}
    for slot in owners:
        by_type.setdefault(slot.owner_type, set()).add(slot.owner_order)
    for orders in by_type.values():
        if len(orders) > 3:
            raise ApplicationOwnerLimitError()
        if 1 not in orders:
            raise ApplicationOwnerPrimaryRequiredError()

    for slot in owners:
        if not await is_user_active(session, slot.user_id):
            raise ApplicationOwnerUserNotFoundError()

    now = clock.now()
    desired = {(slot.owner_type, slot.owner_order): slot.user_id for slot in owners}

    existing_result = await session.execute(
        select(ApplicationOwner).where(ApplicationOwner.application_id == application_id)
    )
    existing_by_key = {(row.owner_type, row.owner_order): row for row in existing_result.scalars().all()}

    # Snapshot the active slot->user mapping before mutation: rows are UPDATEd in place (see
    # the docstring above), so this is the only place the prior assignment is ever visible --
    # without it, replacing one user with another in the same slot produced an identical
    # before/after audit payload and the removed assignment was unreconstructable (found in
    # review).
    before_owners = {
        f"{k[0]}:{k[1]}": str(row.user_id) for k, row in existing_by_key.items() if row.deleted_at is None
    }

    for key, row in existing_by_key.items():
        if key not in desired and row.deleted_at is None:
            row.deleted_at = now
            row.updated_at = now

    result_rows: list[ApplicationOwner] = []
    for (owner_type, owner_order), user_id in desired.items():
        row = existing_by_key.get((owner_type, owner_order))
        if row is not None:
            row.user_id = user_id
            row.deleted_at = None
            row.updated_at = now
            result_rows.append(row)
        else:
            new_row = ApplicationOwner(
                application_id=application_id,
                owner_type=owner_type,
                owner_order=owner_order,
                user_id=user_id,
            )
            session.add(new_row)
            result_rows.append(new_row)

    await session.flush()

    after_owners = {f"{slot.owner_type}:{slot.owner_order}": str(slot.user_id) for slot in owners}
    await write_audit(
        session,
        actor_user_id=actor_id,
        entity_type="APPLICATION",
        entity_id=application_id,
        action="APPLICATION_OWNERS_SET",
        before={"owners": before_owners},
        after={"owners": after_owners},
    )
    return result_rows


async def update_tiers(
    session: AsyncSession,
    *,
    actor_id: uuid.UUID,
    updates: list[TierUpdate],
    clock: Clock | None = None,
) -> list[Tier]:
    """`code`/`rank` are immutable — V1 has exactly 5 tiers, no create/delete
    (BUILD-03.plan.md Risk #3). An unrecognised `code` is rejected, not upserted."""
    clock = clock or SystemClock()
    await AuthorizationService.require(session, actor_id, Capability.MANAGE_TIERS)

    now = clock.now()
    result_rows: list[Tier] = []
    for update in updates:
        tier_result = await session.execute(select(Tier).where(Tier.code == update.code).with_for_update())
        tier = tier_result.scalar_one_or_none()
        if tier is None:
            raise UnknownTierCodeError(update.code)
        if tier.version != update.expected_version:
            raise ConcurrencyConflictError()

        before = {
            "default_sla_minutes": tier.default_sla_minutes,
            "default_health_weight": str(tier.default_health_weight),
        }
        tier.default_sla_minutes = update.default_sla_minutes
        tier.default_health_weight = update.default_health_weight
        if update.description is not None:
            tier.description = update.description
        tier.version += 1
        tier.updated_at = now
        result_rows.append(tier)

        await write_audit(
            session,
            actor_user_id=actor_id,
            entity_type="TIER",
            entity_id=tier.id,
            action="TIER_UPDATED",
            before=before,
            after={
                "default_sla_minutes": tier.default_sla_minutes,
                "default_health_weight": str(tier.default_health_weight),
            },
        )

    await session.flush()
    return result_rows
