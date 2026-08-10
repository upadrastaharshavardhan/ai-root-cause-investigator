"""Application settings loaded from environment."""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # App
    app_env: Literal["development", "staging", "production"] = "production"
    log_level: str = "INFO"
    api_host: str = "0.0.0.0"
    api_port: int = 8000
    secret_key: SecretStr = Field(default=SecretStr("change-me-in-production"))
    jwt_algorithm: str = "HS256"
    jwt_expire_minutes: int = 60

    # LLM
    llm_provider: Literal["openai", "anthropic", "azure_openai"] = "openai"
    openai_api_key: SecretStr | None = None
    openai_model: str = "gpt-4o"
    anthropic_api_key: SecretStr | None = None
    azure_openai_endpoint: str | None = None
    azure_openai_api_key: SecretStr | None = None
    azure_openai_deployment: str | None = None

    # Azure DevOps
    azure_devops_org_url: str = ""
    azure_devops_pat: SecretStr | None = None
    azure_devops_project: str = ""

    # Git
    git_repo_url: str = ""
    git_token: SecretStr | None = None
    git_default_branch: str = "main"

    # Artifacts
    playwright_artifact_base_url: str = ""
    artifact_storage: Literal["local", "azure_blob", "s3"] = "local"
    azure_storage_connection_string: SecretStr | None = None
    s3_bucket: str = ""
    s3_region: str = "us-east-1"

    # Memory
    chroma_persist_dir: str = "./data/chroma"
    embedding_model: str = "sentence-transformers/all-MiniLM-L6-v2"
    memory_enabled: bool = True

    # Approval
    approval_mode: Literal["interactive", "webhook", "slack"] = "interactive"
    approval_webhook_url: str = ""
    slack_bot_token: SecretStr | None = None
    slack_channel: str = ""

    # Observability & Safety
    otel_exporter_otlp_endpoint: str = ""
    prometheus_enabled: bool = True
    allow_auto_remediation: bool = False
    max_investigation_timeout_seconds: int = 120

    @property
    def is_production(self) -> bool:
        return self.app_env == "production"


@lru_cache
def get_settings() -> Settings:
    return Settings()
