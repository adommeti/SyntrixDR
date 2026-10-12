"""`POST /evidence`, `POST /evidence/{id}/file`, `GET /evidence/{id}` (API_CONTRACT.md:203-205) against
Azurite: D-225 allowlist / size cap, server-side content sniff, D-245 EICAR -> INFECTED + quarantine, SAS
download for CLEAN files, D-215 replay."""

from __future__ import annotations

import hashlib
import uuid
from typing import Any

import httpx
import pytest
from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.clock import FakeClock
from app.validation_evidence.scanning import EICAR_SIGNATURE
from tests.factories import (
    World,
    build_world,
    headers,
    http,
    login,
    seed_blocker,
    seed_issue,
    seed_milestone,
    seed_task,
)

pytestmark = [pytest.mark.api, pytest.mark.files]

PDF = b"%PDF-1.7\n1 0 obj << >> endobj\n%%EOF\n"
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32


async def _post_json(
    session: AsyncSession,
    redis_client: Redis,
    clock: FakeClock,
    actor: uuid.UUID,
    path: str,
    body: dict[str, Any],
    key: str | None = None,
) -> Any:
    sid, csrf = await login(session, redis_client, clock, actor)
    async with http(session, redis_client, clock) as c:
        r = await c.post(path, cookies={"drcc_session": sid}, headers=headers(csrf, key), json=body)
    await redis_client.delete(f"drcc:session:{sid}")
    return r


async def _upload(
    session: AsyncSession,
    redis_client: Redis,
    clock: FakeClock,
    actor: uuid.UUID,
    evidence_id: str,
    name: str,
    content: bytes,
    declared: str = "application/octet-stream",
    key: str | None = None,
) -> Any:
    sid, csrf = await login(session, redis_client, clock, actor)
    async with http(session, redis_client, clock) as c:
        r = await c.post(
            f"/api/v1/evidence/{evidence_id}/file",
            cookies={"drcc_session": sid},
            headers=headers(csrf, key),
            files={"file": (name, content, declared)},
        )
    await redis_client.delete(f"drcc:session:{sid}")
    return r


async def _get(
    session: AsyncSession, redis_client: Redis, clock: FakeClock, actor: uuid.UUID, evidence_id: str
) -> Any:
    sid, _ = await login(session, redis_client, clock, actor)
    async with http(session, redis_client, clock) as c:
        r = await c.get(f"/api/v1/evidence/{evidence_id}", cookies={"drcc_session": sid})
    await redis_client.delete(f"drcc:session:{sid}")
    return r


async def _handshake(
    session: AsyncSession,
    redis_client: Redis,
    clock: FakeClock,
    w: World,
    task_id: uuid.UUID,
    name: str,
    kind: str = "FILE",
) -> str:
    r = await _post_json(
        session, redis_client, clock, w.executor_id, "/api/v1/evidence",
        {"target_type": "TASK", "target_id": str(task_id), "evidence_type": kind, "file_name": name},
    )  # fmt: skip
    assert r.status_code == 201, r.text
    body = r.json()
    assert (body["malware_scan_status"], body["file_attached"], body["download_url"]) == (
        "PENDING",
        False,
        None,
    )
    return body["id"]


async def _audits(session: AsyncSession, target_id: uuid.UUID) -> list[str]:
    rows = await session.execute(
        text(
            "SELECT action FROM audit_events WHERE entity_id = :t AND action LIKE 'EVIDENCE_%' "
            "ORDER BY occurred_at, action"
        ),
        {"t": target_id},
    )
    return [r.action for r in rows]


@pytest.mark.parametrize(
    ("evidence_type", "payload"),
    [
        ("TEXT", {"text_content": "  checked the failover logs  "}),
        ("LOG", {"text_content": "2026-10-11 INFO failover ok"}),
        ("EXTERNAL_LINK", {"external_url": "https://runbooks.example.test/step-4"}),
    ],
)
async def test_text_and_link_evidence_are_created_and_count(
    session: AsyncSession, redis_client: Redis, clock: FakeClock, evidence_type: str, payload: dict[str, str]
) -> None:
    w = await build_world(session)
    task_id = await seed_task(session, w, status="IN_PROGRESS")

    r = await _post_json(
        session, redis_client, clock, w.executor_id, "/api/v1/evidence",
        {"target_type": "TASK", "target_id": str(task_id), "evidence_type": evidence_type, **payload},
    )  # fmt: skip

    assert r.status_code == 201, r.text
    body = r.json()
    assert body["evidence_type"] == evidence_type and body["malware_scan_status"] is None
    if "text_content" in payload:
        assert body["text_content"] == payload["text_content"].strip()
    assert await _audits(session, task_id) == ["EVIDENCE_CREATED"]
    assert (
        await session.execute(
            text("SELECT count(*) FROM outbox_events WHERE aggregate_id = :t AND event_type = 'TaskChanged'"),
            {"t": task_id},
        )
    ).scalar_one() == 1


async def test_file_upload_clean_is_sniffed_hashed_stored_and_downloadable(
    session: AsyncSession, redis_client: Redis, clock: FakeClock
) -> None:
    w = await build_world(session)
    task_id = await seed_task(session, w, status="IN_PROGRESS")
    evidence_id = await _handshake(session, redis_client, clock, w, task_id, "report.pdf")

    r = await _upload(
        session, redis_client, clock, w.executor_id, evidence_id, "report.pdf", PDF, declared="image/png"
    )

    assert r.status_code == 200, r.text
    body = r.json()
    assert (body["malware_scan_status"], body["mime_type"], body["size_bytes"], body["file_attached"]) == (
        "CLEAN", "application/pdf", len(PDF), True,
    )  # fmt: skip
    assert body["checksum"] == hashlib.sha256(PDF).hexdigest()
    assert body["download_url"] and "sig=" in body["download_url"]
    async with httpx.AsyncClient() as client:
        assert (await client.get(body["download_url"])).content == PDF
    assert await _audits(session, task_id) == [
        "EVIDENCE_CREATED",
        "EVIDENCE_FILE_ATTACHED",
        "EVIDENCE_SCANNED",
    ]
    got = await _get(session, redis_client, clock, w.teammate_id, evidence_id)
    assert got.status_code == 200 and got.json()["download_url"]


async def test_eicar_upload_is_infected_quarantined_and_never_downloadable(
    session: AsyncSession, redis_client: Redis, clock: FakeClock
) -> None:
    w = await build_world(session)
    task_id = await seed_task(session, w, status="IN_PROGRESS")
    evidence_id = await _handshake(session, redis_client, clock, w, task_id, "notes.txt")

    r = await _upload(session, redis_client, clock, w.executor_id, evidence_id, "notes.txt", EICAR_SIGNATURE)

    assert r.status_code == 200, r.text
    body = r.json()
    assert (body["malware_scan_status"], body["download_url"], body["file_attached"]) == (
        "INFECTED",
        None,
        True,
    )
    blob_uri = (
        await session.execute(text("SELECT blob_uri FROM evidence_items WHERE id = :e"), {"e": evidence_id})
    ).scalar_one()
    assert "/quarantine/" in blob_uri
    assert await _audits(session, task_id) == [
        "EVIDENCE_CREATED", "EVIDENCE_FILE_ATTACHED", "EVIDENCE_SCANNED", "EVIDENCE_QUARANTINED",
    ]  # fmt: skip
    got = await _get(session, redis_client, clock, w.coordinator_id, evidence_id)
    assert got.json()["download_url"] is None


@pytest.mark.parametrize(
    ("name", "content", "status", "code"),
    [
        ("tool.exe", b"MZ\x90\x00", 422, "UNSUPPORTED_FILE_TYPE"),  # not on the allowlist
        ("photo.pdf", PNG, 422, "FILE_CONTENT_MISMATCH"),  # PNG bytes named .pdf
        ("empty.txt", b"", 422, "FILE_CONTENT_MISMATCH"),
        ("big.txt", b"x" * (104_857_600 + 1), 422, "FILE_TOO_LARGE"),
    ],
    ids=["exe", "png-as-pdf", "empty", "oversize"],
)
async def test_rejected_uploads(
    session: AsyncSession,
    redis_client: Redis,
    clock: FakeClock,
    name: str,
    content: bytes,
    status: int,
    code: str,
) -> None:
    w = await build_world(session)
    task_id = await seed_task(session, w, status="IN_PROGRESS")
    handshake_name = "ok.txt" if name == "tool.exe" else name
    evidence_id = await _handshake(session, redis_client, clock, w, task_id, handshake_name)
    if name == "tool.exe":  # the handshake already rejects a bad extension; force one past it
        await session.execute(
            text("UPDATE evidence_items SET file_name = 'tool.exe' WHERE id = :e"), {"e": evidence_id}
        )

    r = await _upload(session, redis_client, clock, w.executor_id, evidence_id, name, content)

    assert (r.status_code, r.json()["error"]["code"]) == (status, code), r.text
    row = (
        await session.execute(
            text("SELECT checksum, malware_scan_status FROM evidence_items WHERE id = :e"), {"e": evidence_id}
        )
    ).one()
    assert (row.checksum, row.malware_scan_status) == (None, "PENDING")


async def test_handshake_rejects_a_disallowed_extension_up_front(
    session: AsyncSession, redis_client: Redis, clock: FakeClock
) -> None:
    w = await build_world(session)
    task_id = await seed_task(session, w, status="IN_PROGRESS")
    r = await _post_json(
        session, redis_client, clock, w.executor_id, "/api/v1/evidence",
        {"target_type": "TASK", "target_id": str(task_id), "evidence_type": "FILE", "file_name": "bad.exe"},
    )  # fmt: skip
    assert (r.status_code, r.json()["error"]["code"]) == (422, "UNSUPPORTED_FILE_TYPE")


async def test_second_upload_and_upload_onto_text_are_409(
    session: AsyncSession, redis_client: Redis, clock: FakeClock
) -> None:
    w = await build_world(session)
    task_id = await seed_task(session, w, status="IN_PROGRESS")
    evidence_id = await _handshake(session, redis_client, clock, w, task_id, "a.pdf")
    assert (
        await _upload(session, redis_client, clock, w.executor_id, evidence_id, "a.pdf", PDF)
    ).status_code == 200

    again = await _upload(session, redis_client, clock, w.executor_id, evidence_id, "a.pdf", PDF)
    assert (again.status_code, again.json()["error"]["code"]) == (409, "EVIDENCE_FILE_ALREADY_ATTACHED")

    text_item = await _post_json(
        session, redis_client, clock, w.executor_id, "/api/v1/evidence",
        {"target_type": "TASK", "target_id": str(task_id), "evidence_type": "TEXT", "text_content": "t"},
    )  # fmt: skip
    onto_text = await _upload(
        session, redis_client, clock, w.executor_id, text_item.json()["id"], "a.pdf", PDF
    )
    assert (onto_text.status_code, onto_text.json()["error"]["code"]) == (409, "EVIDENCE_NOT_A_FILE")


async def test_upload_replays_the_original_and_needs_a_key(
    session: AsyncSession, redis_client: Redis, clock: FakeClock
) -> None:
    w = await build_world(session)
    task_id = await seed_task(session, w, status="IN_PROGRESS")
    evidence_id = await _handshake(session, redis_client, clock, w, task_id, "a.pdf")
    key = str(uuid.uuid4())

    first = await _upload(session, redis_client, clock, w.executor_id, evidence_id, "a.pdf", PDF, key=key)
    replay = await _upload(session, redis_client, clock, w.executor_id, evidence_id, "a.pdf", PDF, key=key)

    assert first.status_code == 200 and (replay.status_code, replay.json()) == (200, first.json())
    assert await _audits(session, task_id) == [
        "EVIDENCE_CREATED",
        "EVIDENCE_FILE_ATTACHED",
        "EVIDENCE_SCANNED",
    ]

    sid, csrf = await login(session, redis_client, clock, w.executor_id)
    async with http(session, redis_client, clock) as c:
        no_key = await c.post(
            f"/api/v1/evidence/{evidence_id}/file", cookies={"drcc_session": sid},
            headers={"X-CSRF-Token": csrf}, files={"file": ("a.pdf", PDF, "application/pdf")},
        )  # fmt: skip
    await redis_client.delete(f"drcc:session:{sid}")
    assert (no_key.status_code, no_key.json()["error"]["code"]) == (400, "IDEMPOTENCY_KEY_REQUIRED")


async def test_invisible_target_and_evidence_are_404(
    session: AsyncSession, redis_client: Redis, clock: FakeClock
) -> None:
    w = await build_world(session)
    task_id = await seed_task(session, w, status="IN_PROGRESS")
    evidence_id = await _handshake(session, redis_client, clock, w, task_id, "a.pdf")

    r = await _post_json(
        session, redis_client, clock, w.stranger_id, "/api/v1/evidence",
        {"target_type": "TASK", "target_id": str(task_id), "evidence_type": "TEXT", "text_content": "x"},
    )  # fmt: skip
    assert (r.status_code, r.json()["error"]["code"]) == (404, "EVIDENCE_TARGET_NOT_FOUND")
    assert (await _get(session, redis_client, clock, w.stranger_id, evidence_id)).status_code == 404
    assert (
        await _upload(session, redis_client, clock, w.stranger_id, evidence_id, "a.pdf", PDF)
    ).status_code == 404
    assert (await _get(session, redis_client, clock, w.admin_id, str(uuid.uuid4()))).status_code == 404


async def test_every_allowed_target_type_resolves_to_its_event(
    session: AsyncSession, redis_client: Redis, clock: FakeClock
) -> None:
    w = await build_world(session)
    task_id = await seed_task(session, w, status="IN_PROGRESS")
    blocked = await seed_task(session, w, status="BLOCKED")
    await session.execute(text("DELETE FROM blockers WHERE task_id = :t"), {"t": blocked})
    blocker_id = await seed_blocker(session, w, blocked, owner_id=w.executor_id)
    validation_id = uuid.uuid4()
    await session.execute(
        text(
            "INSERT INTO validations (id, target_type, target_id, status, created_by_user_id, "
            "created_at, updated_at) VALUES (:id, 'TASK', :t, 'PENDING', :u, now(), now())"
        ),
        {"id": validation_id, "t": task_id, "u": w.executor_id},
    )
    targets = {
        "TASK": (task_id, w.executor_id),
        "DR_APPLICATION": (w.dr_application_id, w.system_owner_id),
        "MILESTONE": (await seed_milestone(session, w), w.ws_lead_id),
        "BLOCKER": (blocker_id, w.executor_id),
        "VALIDATION": (validation_id, w.executor_id),
        "ISSUE_FINDING": (await seed_issue(session, w, created_by=w.executor_id), w.executor_id),
    }

    def body(target_type: str, target_id: uuid.UUID) -> dict[str, str]:
        return {
            "target_type": target_type,
            "target_id": str(target_id),
            "evidence_type": "TEXT",
            "text_content": "x",
        }

    for target_type, (target_id, actor) in targets.items():
        r = await _post_json(
            session, redis_client, clock, actor, "/api/v1/evidence", body(target_type, target_id)
        )
        assert r.status_code == 201, (target_type, r.text)
        denied = await _post_json(
            session,
            redis_client,
            clock,
            w.business_owner_id,
            "/api/v1/evidence",
            body(target_type, target_id),
        )
        assert denied.status_code == 403, (target_type, denied.text)
    bad = await _post_json(
        session, redis_client, clock, w.admin_id, "/api/v1/evidence", body("WORK_STREAM", w.work_stream_id)
    )
    assert bad.status_code == 422  # schema Literal: not in the evidence subset
