from __future__ import annotations

import uuid

import pytest

from app.core.config import get_settings
from app.core.storage import ObjectNotFoundError, ObjectStore

pytestmark = pytest.mark.integration


@pytest.fixture
def object_store() -> ObjectStore:
    return ObjectStore(get_settings().azure_storage_connection_string)


@pytest.mark.asyncio
async def test_upload_then_download_round_trips_the_same_bytes(object_store: ObjectStore) -> None:
    container = f"test-{uuid.uuid4().hex}"
    blob_name = "workbook.xlsx"
    payload = b"fictitious xlsx bytes for the round-trip test"

    uri = await object_store.upload(container, blob_name, payload, content_type="application/octet-stream")
    downloaded = await object_store.download(uri)

    assert downloaded == payload


@pytest.mark.asyncio
async def test_upload_is_idempotent_on_overwrite(object_store: ObjectStore) -> None:
    container = f"test-{uuid.uuid4().hex}"
    blob_name = "workbook.xlsx"

    await object_store.upload(container, blob_name, b"first", content_type="text/plain")
    uri = await object_store.upload(container, blob_name, b"second", content_type="text/plain")

    assert await object_store.download(uri) == b"second"


@pytest.mark.asyncio
async def test_download_of_missing_blob_raises_object_not_found(object_store: ObjectStore) -> None:
    container = f"test-{uuid.uuid4().hex}"
    # Force the container to exist (upload then rely on the same container name) so the failure
    # being tested is genuinely "blob missing", not "container missing".
    await object_store.upload(container, "present.txt", b"x", content_type="text/plain")
    missing_uri = await object_store.upload(container, "present.txt", b"x", content_type="text/plain")
    missing_uri = missing_uri.rsplit("/", 1)[0] + "/does-not-exist.txt"

    with pytest.raises(ObjectNotFoundError):
        await object_store.download(missing_uri)
