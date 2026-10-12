from __future__ import annotations

from datetime import UTC, datetime, timedelta
from urllib.parse import urlparse

from azure.core.exceptions import ResourceExistsError, ResourceNotFoundError
from azure.storage.blob import BlobSasPermissions, ContentSettings, generate_blob_sas
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
    matching the rest of `core/`'s DI style. Objects are private; readers get short-lived SAS URLs
    (ADR-014); INFECTED objects are moved under `quarantine/` (ADR-024)."""

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
        container, blob_name = split_blob_url(uri)
        async with BlobServiceClient.from_connection_string(self._connection_string) as service:
            blob_client = service.get_container_client(container).get_blob_client(blob_name)
            try:
                downloader = await blob_client.download_blob()
                return await downloader.readall()
            except ResourceNotFoundError as exc:
                raise ObjectNotFoundError(uri) from exc

    async def content_type(self, uri: str) -> str:
        container, blob_name = split_blob_url(uri)
        async with BlobServiceClient.from_connection_string(self._connection_string) as service:
            blob_client = service.get_container_client(container).get_blob_client(blob_name)
            try:
                properties = await blob_client.get_blob_properties()
            except ResourceNotFoundError as exc:
                raise ObjectNotFoundError(uri) from exc
            return properties.content_settings.content_type or "application/octet-stream"

    async def delete(self, uri: str) -> None:
        container, blob_name = split_blob_url(uri)
        async with BlobServiceClient.from_connection_string(self._connection_string) as service:
            blob_client = service.get_container_client(container).get_blob_client(blob_name)
            try:
                await blob_client.delete_blob()
            except ResourceNotFoundError as exc:
                raise ObjectNotFoundError(uri) from exc

    async def move(self, uri: str, new_blob_name: str) -> str:
        """Copy to `new_blob_name` in the same container, then delete the original. Used for
        quarantine (`quarantine/<original name>`, ADR-024). Returns the new URI."""
        container, _ = split_blob_url(uri)
        data = await self.download(uri)
        content_type = await self.content_type(uri)
        new_uri = await self.upload(container, new_blob_name, data, content_type=content_type)
        await self.delete(uri)
        return new_uri

    def signed_download_url(self, uri: str, *, ttl_seconds: int) -> str:
        """A read-only SAS URL for `uri`, valid `ttl_seconds` from now (ADR-014's
        `BLOB_SAS_TTL_SECONDS`). Pure: the signature comes from the account key, no I/O."""
        container, blob_name = split_blob_url(uri)
        account_name, account_key = _account(self._connection_string)
        token = generate_blob_sas(
            account_name=account_name,
            container_name=container,
            blob_name=blob_name,
            account_key=account_key,
            permission=BlobSasPermissions(read=True),
            expiry=datetime.now(UTC) + timedelta(seconds=ttl_seconds),
        )
        return f"{uri}?{token}"


def _account(connection_string: str) -> tuple[str, str]:
    """`AccountName` and `AccountKey` from an Azure Storage connection string."""
    parts = dict(kv.split("=", 1) for kv in connection_string.split(";") if "=" in kv)
    return parts["AccountName"], parts["AccountKey"]


def split_blob_url(uri: str) -> tuple[str, str]:
    """A blob URL's path is `/<account>/<container>/<blob_name...>` for the Azurite emulator (the
    account is a path segment, not part of the host) or `/<container>/<blob_name...>` for real
    Azure Blob (the account is in the hostname instead)."""
    parsed = urlparse(uri)
    path_parts = parsed.path.lstrip("/").split("/")
    if parsed.hostname in {"localhost", "127.0.0.1"} and path_parts[0] == "devstoreaccount1":
        path_parts = path_parts[1:]
    container, *blob_parts = path_parts
    return container, "/".join(blob_parts)
