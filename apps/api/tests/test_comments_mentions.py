"""Cross-Team comments and @mentions (API_CONTRACT.md:217, RBAC_MATRIX.md:24, FROZEN §12.8, D-216 target
subset). A mention notifies the user in the same transaction, with a `NotificationCreated` outbox event."""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.comments_mentions_notifications.commands import create_comment
from app.comments_mentions_notifications.mentions import parse_mentions
from app.core.clock import FakeClock
from app.core.errors import AppError
from tests.factories import (
    World,
    build_world,
    headers,
    http,
    login,
    notifications_of,
    seed_issue,
    seed_milestone,
    seed_task,
)

pytestmark = [pytest.mark.domain]


@pytest.mark.parametrize(
    ("body", "handles"),
    [
        ('Ping @"Ada Lovelace" please', ["Ada Lovelace"]),
        ("cc @ada.lovelace and @ada@example.com.", ["ada.lovelace", "ada@example.com"]),
        ("@Bob, @bob! twice is once", ["Bob"]),
        ("mail me at ops@example.com (no mention)", []),
        ('@ alone and @"" empty are nothing', []),
        ("", []),
    ],
)
def test_parse_mentions(body: str, handles: list[str]) -> None:
    assert parse_mentions(body) == handles


async def _target_ids(session: AsyncSession, w: World, clock: FakeClock) -> dict[str, uuid.UUID]:
    task_id = await seed_task(session, w)
    blocked = await seed_task(session, w, status="BLOCKED")
    [blocker_id] = [
        r.id
        for r in await session.execute(text("SELECT id FROM blockers WHERE task_id = :t"), {"t": blocked})
    ]
    return {
        "TASK": task_id,
        "DR_APPLICATION": w.dr_application_id,
        "WORK_STREAM": w.work_stream_id,
        "BLOCKER": blocker_id,
        "ISSUE_FINDING": await seed_issue(session, w),
        "DR_EVENT": w.event_id,
        "MILESTONE": await seed_milestone(session, w),
    }


@pytest.mark.parametrize(
    "target_type",
    ["TASK", "DR_APPLICATION", "WORK_STREAM", "BLOCKER", "ISSUE_FINDING", "DR_EVENT", "MILESTONE"],
)
async def test_a_comment_on_every_allowed_target_resolves_to_the_event(
    session: AsyncSession, clock: FakeClock, target_type: str
) -> None:
    w = await build_world(session)
    targets = await _target_ids(session, w, clock)

    comment, notified = await create_comment(
        session,
        actor_id=w.outsider_id,
        target_type=target_type,
        target_id=targets[target_type],
        body="  cross-Team note  ",
        clock=clock,
    )

    assert (comment.body, notified) == ("cross-Team note", [])
    audit = (
        await session.execute(
            text(
                "SELECT entity_type, entity_id, dr_event_id FROM audit_events "
                "WHERE action = 'COMMENT_CREATED'"
            )
        )
    ).one()
    assert (audit.entity_type, audit.entity_id, audit.dr_event_id) == (
        target_type,
        targets[target_type],
        w.event_id,
    )

    with pytest.raises(AppError) as e:
        await create_comment(
            session,
            actor_id=w.stranger_id,
            target_type=target_type,
            target_id=targets[target_type],
            body="x",
            clock=clock,
        )
    assert (e.value.code, e.value.status_code) == ("COMMENT_TARGET_NOT_FOUND", 404)


@pytest.mark.parametrize("target_type", ["APPLICATION", "VALIDATION", "PLAN", "ALERT", "TASK_DEPENDENCY"])
async def test_targets_outside_the_comments_subset_are_rejected(
    session: AsyncSession, clock: FakeClock, target_type: str
) -> None:
    w = await build_world(session)
    with pytest.raises(AppError) as e:
        await create_comment(
            session, actor_id=w.admin_id, target_type=target_type, target_id=w.event_id, body="x", clock=clock
        )
    assert (e.value.code, e.value.status_code) == ("TARGET_TYPE_NOT_ALLOWED", 422)


async def test_a_mention_notifies_the_user_in_the_same_transaction(
    session: AsyncSession, clock: FakeClock
) -> None:
    w = await build_world(session)
    task_id = await seed_task(session, w)
    name = (
        await session.execute(text("SELECT display_name FROM users WHERE id = :u"), {"u": w.teammate_id})
    ).scalar_one()

    comment, notified = await create_comment(
        session, actor_id=w.outsider_id, target_type="TASK", target_id=task_id,
        body=f'@"{name}" can you check the VLAN? @nobody-here', clock=clock,
    )  # fmt: skip

    assert notified == [w.teammate_id]
    [note] = await notifications_of(session, w.teammate_id)
    assert (note.notification_type, note.severity, note.target_type, note.target_id, note.dr_event_id) == (
        "MENTIONED", "LOW", "TASK", task_id, w.event_id,
    )  # fmt: skip
    mentions = (
        (
            await session.execute(
                text("SELECT mentioned_user_id FROM mentions WHERE comment_id = :c"), {"c": comment.id}
            )
        )
        .scalars()
        .all()
    )
    assert mentions == [w.teammate_id]
    outbox = (
        await session.execute(
            text(
                "SELECT payload FROM outbox_events "
                "WHERE event_type = 'NotificationCreated' AND aggregate_id = :t"
            ),
            {"t": task_id},
        )
    ).scalar_one()
    assert outbox["user_id"] == str(w.teammate_id) and outbox["notification_type"] == "MENTIONED"
    meta = (
        await session.execute(text("SELECT metadata FROM audit_events WHERE action = 'COMMENT_CREATED'"))
    ).scalar_one()
    assert meta["unresolved_mentions"] == ["nobody-here"]


async def test_mentions_resolve_by_email_and_explicit_ids_but_never_yourself(
    session: AsyncSession, clock: FakeClock
) -> None:
    w = await build_world(session)
    task_id = await seed_task(session, w)
    email = (
        await session.execute(text("SELECT email FROM users WHERE id = :u"), {"u": w.ws_lead_id})
    ).scalar_one()

    _, notified = await create_comment(
        session, actor_id=w.executor_id, target_type="TASK", target_id=task_id,
        body=f"@{email} FYI", mentioned_user_ids=[w.coordinator_id, w.executor_id], clock=clock,
    )  # fmt: skip

    assert set(notified) == {w.ws_lead_id, w.coordinator_id}
    assert await notifications_of(session, w.executor_id) == []


async def test_a_mentioned_user_who_cannot_see_the_event_is_recorded_not_notified(
    session: AsyncSession, clock: FakeClock
) -> None:
    w = await build_world(session)
    task_id = await seed_task(session, w)

    _, notified = await create_comment(
        session, actor_id=w.executor_id, target_type="TASK", target_id=task_id,
        body="heads up", mentioned_user_ids=[w.stranger_id], clock=clock,
    )  # fmt: skip

    assert notified == []
    assert await notifications_of(session, w.stranger_id) == []
    meta = (
        await session.execute(text("SELECT metadata FROM audit_events WHERE action = 'COMMENT_CREATED'"))
    ).scalar_one()
    assert meta["not_visible_user_ids"] == [str(w.stranger_id)]


async def test_a_reply_must_answer_a_comment_on_the_same_target(
    session: AsyncSession, clock: FakeClock
) -> None:
    w = await build_world(session)
    a, b = await seed_task(session, w), await seed_task(session, w)
    parent, _ = await create_comment(
        session, actor_id=w.executor_id, target_type="TASK", target_id=a, body="root", clock=clock
    )

    reply, _ = await create_comment(
        session,
        actor_id=w.teammate_id,
        target_type="TASK",
        target_id=a,
        body="reply",
        parent_comment_id=parent.id,
        clock=clock,
    )
    assert reply.parent_comment_id == parent.id

    with pytest.raises(AppError) as e:
        await create_comment(
            session,
            actor_id=w.teammate_id,
            target_type="TASK",
            target_id=b,
            body="x",
            parent_comment_id=parent.id,
            clock=clock,
        )
    assert (e.value.code, e.value.status_code) == ("PARENT_COMMENT_MISMATCH", 422)


async def test_comment_authority_needs_a_role(session: AsyncSession, clock: FakeClock) -> None:
    w = await build_world(session)
    task_id = await seed_task(session, w)
    for actor in (w.business_owner_id, w.manager_id, w.outsider_id, w.ws_lead_id):  # every role ✓
        await create_comment(
            session, actor_id=actor, target_type="TASK", target_id=task_id, body="ok", clock=clock
        )
    with pytest.raises(AppError) as e:
        await create_comment(
            session,
            actor_id=w.roleless_member_id,
            target_type="TASK",
            target_id=task_id,
            body="x",
            clock=clock,
        )
    assert (e.value.code, e.value.status_code) == ("AUTHORIZATION_REQUIRED", 403)


async def test_blank_body_is_rejected(session: AsyncSession, clock: FakeClock) -> None:
    w = await build_world(session)
    with pytest.raises(AppError) as e:
        await create_comment(
            session,
            actor_id=w.admin_id,
            target_type="DR_EVENT",
            target_id=w.event_id,
            body="   ",
            clock=clock,
        )
    assert e.value.code == "COMMENT_BODY_REQUIRED"


@pytest.mark.api
async def test_comments_over_http_list_in_order_with_mentions(
    session: AsyncSession, redis_client: Redis, clock: FakeClock
) -> None:
    w = await build_world(session)
    task_id = await seed_task(session, w)
    sid, csrf = await login(session, redis_client, clock, w.outsider_id)
    async with http(session, redis_client, clock) as c:
        first = await c.post(
            f"/api/v1/TASK/{task_id}/comments", cookies={"drcc_session": sid}, headers=headers(csrf),
            json={"body": "first", "mentioned_user_ids": [str(w.teammate_id)]},
        )  # fmt: skip
        assert first.status_code == 201, first.text
        clock.advance(seconds=1)
        second = await c.post(
            f"/api/v1/TASK/{task_id}/comments", cookies={"drcc_session": sid}, headers=headers(csrf),
            json={"body": "second", "parent_comment_id": first.json()["id"]},
        )  # fmt: skip
        assert second.status_code == 201, second.text
        listed = await c.get(f"/api/v1/TASK/{task_id}/comments", cookies={"drcc_session": sid})
        bad = await c.get(f"/api/v1/PLAN/{task_id}/comments", cookies={"drcc_session": sid})
    await redis_client.delete(f"drcc:session:{sid}")

    bodies: list[Any] = listed.json()["comments"]
    assert [b["body"] for b in bodies] == ["first", "second"]
    assert bodies[0]["mentioned_user_ids"] == [str(w.teammate_id)]
    assert bodies[1]["parent_comment_id"] == first.json()["id"]
    assert bad.status_code == 422  # not in the comments subset (schema-level Literal)
    assert len(await notifications_of(session, w.teammate_id)) == 1


async def test_comment_size_and_mention_counts_are_capped(session: AsyncSession, clock: FakeClock) -> None:
    from app.comments_mentions_notifications.mentions import MAX_COMMENT_CHARS, MAX_MENTIONS

    w = await build_world(session)
    task_id = await seed_task(session, w)
    assert len(parse_mentions(" ".join(f"@user{i}" for i in range(MAX_MENTIONS + 20)))) == MAX_MENTIONS + 1

    with pytest.raises(AppError) as e:  # 51 distinct handles: rejected, never silently truncated
        await create_comment(
            session, actor_id=w.admin_id, target_type="TASK", target_id=task_id,
            body=" ".join(f"@user{i}" for i in range(MAX_MENTIONS + 1)), clock=clock,
        )  # fmt: skip
    assert e.value.code == "COMMENT_TOO_LARGE"
    with pytest.raises(AppError) as e:  # parsed + explicit together over the cap
        await create_comment(
            session, actor_id=w.admin_id, target_type="TASK", target_id=task_id,
            body=" ".join(f"@user{i}" for i in range(MAX_MENTIONS - 1)),
            mentioned_user_ids=[uuid.uuid4(), uuid.uuid4()], clock=clock,
        )  # fmt: skip
    assert e.value.code == "COMMENT_TOO_LARGE"

    with pytest.raises(AppError) as e:
        await create_comment(
            session,
            actor_id=w.admin_id,
            target_type="TASK",
            target_id=task_id,
            body="x" * (MAX_COMMENT_CHARS + 1),
            clock=clock,
        )
    assert (e.value.code, e.value.status_code) == ("COMMENT_TOO_LARGE", 422)
    with pytest.raises(AppError) as e:
        await create_comment(
            session, actor_id=w.admin_id, target_type="TASK", target_id=task_id, body="x",
            mentioned_user_ids=[uuid.uuid4() for _ in range(MAX_MENTIONS + 1)], clock=clock,
        )  # fmt: skip
    assert e.value.code == "COMMENT_TOO_LARGE"


@pytest.mark.api
async def test_an_oversized_comment_is_rejected_at_the_schema(
    session: AsyncSession, redis_client: Redis, clock: FakeClock
) -> None:
    from app.comments_mentions_notifications.mentions import MAX_COMMENT_CHARS

    w = await build_world(session)
    task_id = await seed_task(session, w)
    sid, csrf = await login(session, redis_client, clock, w.admin_id)
    async with http(session, redis_client, clock) as c:
        r = await c.post(
            f"/api/v1/TASK/{task_id}/comments", cookies={"drcc_session": sid}, headers=headers(csrf),
            json={"body": "x" * (MAX_COMMENT_CHARS + 1)},
        )  # fmt: skip
    await redis_client.delete(f"drcc:session:{sid}")
    assert r.status_code == 422
