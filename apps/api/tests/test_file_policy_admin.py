"""`GET/PUT /admin/file-policy` (API_CONTRACT.md:266; D-225): Admin edits the singleton row, the policy values
mirror it, and uploads read the row live."""

from __future__ import annotations

from typing import Any

import pytest
from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.clock import FakeClock
from app.core.errors import AppError
from app.policies_admin.commands import update_file_policy
from app.policies_admin.queries import PolicyService
from app.validation_evidence.queries import AcceptableEvidenceCounter
from tests.factories import build_world, headers, http, login, seed_evidence, seed_task

pytestmark = [pytest.mark.files, pytest.mark.auth]


async def test_update_changes_row_mirrors_policy_values_and_audits(
    session: AsyncSession, clock: FakeClock
) -> None:
    w = await build_world(session)

    policy = await update_file_policy(
        session,
        actor_id=w.admin_id,
        max_mb=25,
        allowed_extensions=["PDF", ".png", "txt", "txt "],
        scanning_required=False,
        clock=clock,
    )

    assert policy.max_bytes == 25 * 1024 * 1024
    assert policy.allowed_extensions == ["pdf", "png", "txt"]
    assert policy.allowed_mime_types == ["application/pdf", "image/png", "text/plain"]
    assert policy.scanning_required is False and policy.updated_by_user_id == w.admin_id
    assert await PolicyService.resolve(session, "file.max_mb") == 25
    assert await PolicyService.resolve(session, "file.allowlist") == ["pdf", "png", "txt"]
    actions = (
        (await session.execute(text("SELECT action FROM audit_events WHERE entity_type = 'FILE_POLICY'")))
        .scalars()
        .all()
    )
    assert list(actions) == ["FILE_POLICY_UPDATED"]


@pytest.mark.parametrize("actor", ["coordinator_id", "system_owner_id", "manager_id", "executor_id"])
async def test_only_a_global_admin_may_change_it(session: AsyncSession, clock: FakeClock, actor: str) -> None:
    w = await build_world(session)
    with pytest.raises(AppError) as e:
        await update_file_policy(
            session,
            actor_id=getattr(w, actor),
            max_mb=1,
            allowed_extensions=["pdf"],
            scanning_required=True,
            clock=clock,
        )
    assert e.value.status_code == 403


async def test_unknown_extensions_and_zero_size_are_rejected(session: AsyncSession, clock: FakeClock) -> None:
    w = await build_world(session)
    with pytest.raises(AppError) as e:
        await update_file_policy(
            session,
            actor_id=w.admin_id,
            max_mb=10,
            allowed_extensions=["pdf", "exe"],
            scanning_required=True,
            clock=clock,
        )
    assert (e.value.code, e.value.details["unknown"]) == ("UNKNOWN_EXTENSION", ["exe"])
    with pytest.raises(AppError) as e:
        await update_file_policy(
            session,
            actor_id=w.admin_id,
            max_mb=0,
            allowed_extensions=["pdf"],
            scanning_required=True,
            clock=clock,
        )
    assert e.value.status_code == 422


async def test_scanning_not_required_lets_unscanned_files_count(
    session: AsyncSession, clock: FakeClock
) -> None:
    w = await build_world(session)
    task_id = await seed_task(session, w, status="IN_PROGRESS")
    await seed_evidence(session, w, target_id=task_id, evidence_type="FILE", scan_status="PENDING")
    assert await AcceptableEvidenceCounter().count_acceptable_for_task(session, task_id) == 0

    await update_file_policy(
        session,
        actor_id=w.admin_id,
        max_mb=100,
        allowed_extensions=["pdf"],
        scanning_required=False,
        clock=clock,
    )

    assert await AcceptableEvidenceCounter().count_acceptable_for_task(session, task_id) == 1


@pytest.mark.api
async def test_over_http_the_new_allowlist_and_cap_apply_to_uploads(
    session: AsyncSession, redis_client: Redis, clock: FakeClock
) -> None:
    w = await build_world(session)
    task_id = await seed_task(session, w, status="IN_PROGRESS")
    sid, csrf = await login(session, redis_client, clock, w.admin_id)
    async with http(session, redis_client, clock) as c:
        before = await c.get("/api/v1/admin/file-policy", cookies={"drcc_session": sid})
        assert before.status_code == 200 and "txt" in before.json()["allowed_extensions"]
        put = await c.put(
            "/api/v1/admin/file-policy",
            cookies={"drcc_session": sid},
            headers=headers(csrf),
            json={"max_mb": 1, "allowed_extensions": ["pdf"], "scanning_required": True},
        )
        assert put.status_code == 200, put.text
        assert put.json()["allowed_extensions"] == ["pdf"] and put.json()["max_bytes"] == 1024 * 1024

        txt = await c.post(
            "/api/v1/evidence",
            cookies={"drcc_session": sid},
            headers=headers(csrf),
            json={
                "target_type": "TASK",
                "target_id": str(task_id),
                "evidence_type": "FILE",
                "file_name": "n.txt",
            },
        )
        assert (txt.status_code, txt.json()["error"]["code"]) == (422, "UNSUPPORTED_FILE_TYPE")
        pdf = await c.post(
            "/api/v1/evidence",
            cookies={"drcc_session": sid},
            headers=headers(csrf),
            json={
                "target_type": "TASK",
                "target_id": str(task_id),
                "evidence_type": "FILE",
                "file_name": "r.pdf",
            },
        )
        big: dict[str, Any] = {"file": ("r.pdf", b"%PDF-1.7\n" + b"x" * (1024 * 1024), "application/pdf")}
        too_big = await c.post(
            f"/api/v1/evidence/{pdf.json()['id']}/file",
            cookies={"drcc_session": sid},
            headers=headers(csrf),
            files=big,
        )
        assert (too_big.status_code, too_big.json()["error"]["code"]) == (422, "FILE_TOO_LARGE")
        denied = await c.get(
            "/api/v1/admin/file-policy",
            cookies={"drcc_session": (await login(session, redis_client, clock, w.executor_id))[0]},
        )
        assert denied.status_code == 403
    await redis_client.delete(f"drcc:session:{sid}")
