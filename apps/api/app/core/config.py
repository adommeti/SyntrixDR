from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

# config.py -> core -> app -> api -> apps -> repo root, so the root .env is
# found regardless of cwd (every Makefile target runs `uv run` from apps/api).
# Real process env vars still take precedence over .env (pydantic-settings default).
_REPO_ROOT = Path(__file__).resolve().parents[4]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=_REPO_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    drcc_env: str = "local"
    drcc_tenant_id: str = "00000000-0000-0000-0000-000000000001"

    database_url: str = "postgresql+psycopg://drcc:drcc@localhost:5432/drcc"
    database_url_ro: str = "postgresql+psycopg://drcc_readonly:drcc_ro@localhost:5432/drcc"

    redis_url: str = "redis://localhost:6379/0"
    celery_broker_url: str = "redis://localhost:6379/0"
    celery_result_backend: str = "redis://localhost:6379/0"

    azure_storage_connection_string: str = (
        "DefaultEndpointsProtocol=http;AccountName=devstoreaccount1;"
        "AccountKey=Eby8vdM02xNOcqFlqUwJPLlmEtlCDXJ1OUzFT50uSRZ6IFsuFq2UVErCz4I6tq/K1SZFPTOtr/KBHBeksoGMGw==;"
        "BlobEndpoint=http://localhost:10000/devstoreaccount1;"
    )
    blob_container_evidence: str = "evidence"
    blob_container_documents: str = "documents"
    blob_container_packages: str = "packages"

    smtp_host: str = "localhost"
    smtp_port: int = 1025
    smtp_from_address: str = "drcc-noreply@localhost"

    session_secret: str = ""
    csrf_secret: str = ""

    ai_enabled: bool = False
    ai_provider: str = "synthetic"

    log_level: str = "INFO"
    log_format: str = "json"

    otel_exporter_otlp_endpoint: str = ""
    otel_service_name: str = "drcc-api"


@lru_cache
def get_settings() -> Settings:
    return Settings()
