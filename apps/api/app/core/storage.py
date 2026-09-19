from __future__ import annotations

from urllib.parse import urlparse

from azure.core.exceptions import ResourceExistsError, ResourceNotFoundError
from azure.storage.blob import ContentSettings
from azure.storage.blob.aio import BlobServiceClient

from app.core.errors import AppError


class ObjectNotFoundError(AppError):
    code = "OBJECT_NOT_FOUND"
    status_code = 404

    def __init__(self, uri: str) -> None:
        super().__init__(f"Object not found: {uri}", details={"uri": uri})


class ObjectStore:
    """One `ObjectStore` adapter on `azure-storage-blob` (D-217) -- Azure Blob in Azure, Azurite
    locally, same account/connection-string shape either way. Takes the connection string via the
    constructor (not `get_settings()` internally) so it's testable without patching global config,
    matching the rest of `core/`'s DI style."""

    def __init__(self, connection_string: str) -> None:
        self._connection_string = connection_string

    async def upload(self, container: str, blob_name: str, data: bytes, content_type: str) -> str:
        async with BlobServiceClient.from_connection_string(self._connection_string) as service:
            container_client = service.get_container_client(container)
            try:
                await container_client.create_container()
            except ResourceExistsError:
                pass

            blob_client = container_client.get_blob_client(blob_name)
            await blob_client.upload_blob(
                data, overwrite=True, content_settings=ContentSettings(content_type=content_type)
            )
            return blob_client.url

    async def download(self, uri: str) -> bytes:
        container, blob_name = _split_blob_url(uri)
        async with BlobServiceClient.from_connection_string(self._connection_string) as service:
            blob_client = service.get_container_client(container).get_blob_client(blob_name)
            try:
                downloader = await blob_client.download_blob()
                return await downloader.readall()
            except ResourceNotFoundError as exc:
                raise ObjectNotFoundError(uri) from exc


def _split_blob_url(uri: str) -> tuple[str, str]:
    """A blob URL's path is `/<account>/<container>/<blob_name...>` for the Azurite emulator (the
    account is a path segment, not part of the host) or `/<container>/<blob_name...>` for real
    Azure Blob (the account is in the hostname instead)."""
    parsed = urlparse(uri)
    path_parts = parsed.path.lstrip("/").split("/")
    if parsed.hostname in {"localhost", "127.0.0.1"} and path_parts[0] == "devstoreaccount1":
        path_parts = path_parts[1:]
    container, *blob_parts = path_parts
    return container, "/".join(blob_parts)
