from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.policies_admin.models import PolicyDefinition, PolicyValue

#: Most-specific-wins order for `PolicyService.resolve` (BUILD-03.plan.md "Domain first").
_SCOPE_PRECEDENCE = ("APPLICATION", "WORK_STREAM", "DR_EVENT", "GLOBAL")


async def list_policy_definitions(session: AsyncSession) -> list[PolicyDefinition]:
    result = await session.execute(select(PolicyDefinition).order_by(PolicyDefinition.key))
    return list(result.scalars().all())


async def get_policy_definition(session: AsyncSession, key: str) -> PolicyDefinition | None:
    result = await session.execute(select(PolicyDefinition).where(PolicyDefinition.key == key))
    return result.scalar_one_or_none()


async def _active_values(session: AsyncSession, policy_definition_id: uuid.UUID) -> list[PolicyValue]:
    result = await session.execute(
        select(PolicyValue).where(
            PolicyValue.policy_definition_id == policy_definition_id,
            PolicyValue.superseded_at.is_(None),
        )
    )
    return list(result.scalars().all())


def _pick_effective(
    definition: PolicyDefinition,
    values: list[PolicyValue],
    *,
    event_id: uuid.UUID | None,
    work_stream_id: uuid.UUID | None,
    application_id: uuid.UUID | None,
) -> Any:
    by_scope = {(v.scope_type, v.scope_id): v.value for v in values}
    scope_ids: dict[str, uuid.UUID | None] = {
        "APPLICATION": application_id,
        "WORK_STREAM": work_stream_id,
        "DR_EVENT": event_id,
        "GLOBAL": None,
    }
    for scope_type in _SCOPE_PRECEDENCE:
        scope_id = scope_ids[scope_type]
        if scope_type != "GLOBAL" and scope_id is None:
            continue
        if (scope_type, scope_id) in by_scope:
            return by_scope[(scope_type, scope_id)]
    return definition.default_value


class PolicyService:
    """`resolve(key, event_id?, work_stream_id?, application_id?)`: most-specific non-superseded
    `policy_values` row wins (APPLICATION -> WORK_STREAM -> DR_EVENT -> GLOBAL), else falls back
    to `policy_definitions.default_value` (BUILD-03.plan.md "Domain first")."""

    @staticmethod
    async def resolve(
        session: AsyncSession,
        key: str,
        *,
        event_id: uuid.UUID | None = None,
        work_stream_id: uuid.UUID | None = None,
        application_id: uuid.UUID | None = None,
    ) -> Any:
        definition = await get_policy_definition(session, key)
        if definition is None:
            return None
        values = await _active_values(session, definition.id)
        return _pick_effective(
            definition,
            values,
            event_id=event_id,
            work_stream_id=work_stream_id,
            application_id=application_id,
        )

    @staticmethod
    async def resolve_all(
        session: AsyncSession,
        *,
        event_id: uuid.UUID | None = None,
        work_stream_id: uuid.UUID | None = None,
        application_id: uuid.UUID | None = None,
    ) -> list[tuple[PolicyDefinition, Any]]:
        definitions = await list_policy_definitions(session)
        resolved: list[tuple[PolicyDefinition, Any]] = []
        for definition in definitions:
            values = await _active_values(session, definition.id)
            resolved.append(
                (
                    definition,
                    _pick_effective(
                        definition,
                        values,
                        event_id=event_id,
                        work_stream_id=work_stream_id,
                        application_id=application_id,
                    ),
                )
            )
        return resolved
