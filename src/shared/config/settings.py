"""Typed application configuration.

One settings tree, loaded from the environment with the ``TOPOFORGE_`` prefix and ``__``
as the nesting delimiter (e.g. ``TOPOFORGE_VISION__DEVICE=cuda``). Secrets are read from
the environment only and are never logged: see :func:`Settings.redacted_dump`.

``pydantic-settings`` is optional at import time so that the domain unit suite runs on a
bare interpreter; when it is absent a minimal environment-backed fallback is used.
"""

from __future__ import annotations

import json
import os
from enum import Enum
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

try:  # pragma: no cover - exercised implicitly by whichever path is installed
    from pydantic_settings import BaseSettings, SettingsConfigDict

    _HAS_PYDANTIC_SETTINGS = True
except ImportError:  # pragma: no cover
    _HAS_PYDANTIC_SETTINGS = False

    class SettingsConfigDict(dict):  # type: ignore[no-redef]
        pass

    class BaseSettings(BaseModel):  # type: ignore[no-redef]
        """Fallback that reads ``TOPOFORGE_``-prefixed variables from ``os.environ``."""

        model_config = {"extra": "ignore"}

        def __init__(self, **values: Any) -> None:
            super().__init__(**{**_collect_env_overrides(type(self)), **values})


_ENV_PREFIX = "TOPOFORGE_"
_NESTED_DELIMITER = "__"

SECRET_FIELD_MARKERS = ("key", "secret", "password", "token", "credential")
REDACTED = "***redacted***"


def _collect_env_overrides(model: type[BaseModel]) -> dict[str, Any]:
    """Minimal ``TOPOFORGE_A__B=value`` reader used when pydantic-settings is absent."""
    overrides: dict[str, Any] = {}
    for raw_key, raw_value in os.environ.items():
        if not raw_key.startswith(_ENV_PREFIX):
            continue
        path = raw_key[len(_ENV_PREFIX) :].lower().split(_NESTED_DELIMITER)
        cursor = overrides
        for part in path[:-1]:
            cursor = cursor.setdefault(part, {})
        cursor[path[-1]] = _coerce(raw_value)
    return {k: v for k, v in overrides.items() if k in model.model_fields}


def _coerce(value: str) -> Any:
    lowered = value.strip().lower()
    if lowered in {"true", "false"}:
        return lowered == "true"
    if value.strip().startswith(("[", "{")):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return value
    return value


class Environment(str, Enum):
    DEVELOPMENT = "development"
    TEST = "test"
    STAGING = "staging"
    PRODUCTION = "production"


class APISettings(BaseModel):
    host: str = "0.0.0.0"  # noqa: S104 - container binding, restricted by the ingress
    port: int = 8000
    root_path: str = ""
    cors_origins: list[str] = Field(default_factory=lambda: ["http://localhost:3000"])
    max_request_bytes: int = 32 * 1024 * 1024
    sse_keepalive_seconds: float = 15.0


class DatabaseSettings(BaseModel):
    url: str = "postgresql+asyncpg://topoforge:topoforge@localhost:5432/topoforge"
    echo: bool = False
    pool_size: int = 10
    max_overflow: int = 20
    statement_timeout_ms: int = 30_000


class StorageSettings(BaseModel):
    backend: Literal["local", "s3"] = "local"
    local_root: Path = Path("./var/storage")
    s3_bucket: str | None = None
    s3_prefix: str = "topoforge/"
    s3_region: str | None = None
    presign_expiry_seconds: int = 900


class VisionSettings(BaseModel):
    detector_backend: Literal["yolo", "stub", "null"] = "stub"
    yolo_weights_path: Path | None = None
    device: Literal["cpu", "cuda"] = "cpu"
    detection_confidence_threshold: float = 0.25
    detection_iou_threshold: float = 0.45
    max_detections: int = 300

    ocr_backend: Literal["paddle", "tesseract", "stub", "null"] = "stub"
    ocr_language: str = "en"
    ocr_confidence_threshold: float = 0.35

    connector_backend: Literal["hough", "stub", "null"] = "hough"

    # Upload guards. Enforced before any decoder touches the bytes.
    max_upload_bytes: int = 25 * 1024 * 1024
    max_image_pixels: int = 40_000_000
    allowed_mime_types: list[str] = Field(
        default_factory=lambda: ["image/png", "image/jpeg", "image/webp", "image/bmp"]
    )

    # Preprocessing
    preprocess_target_long_edge: int = 1600
    preprocess_enhance_contrast: bool = True
    preprocess_denoise: bool = False

    @field_validator("detection_confidence_threshold", "ocr_confidence_threshold")
    @classmethod
    def _unit_interval(cls, value: float) -> float:
        if not 0.0 <= value <= 1.0:
            raise ValueError("confidence thresholds must lie in [0, 1]")
        return value


class LLMSettings(BaseModel):
    provider: Literal["openai", "anthropic", "stub"] = "stub"
    model: str = "claude-sonnet-5"
    temperature: float = 0.0
    max_tokens: int = 8192
    timeout_seconds: float = 120.0
    max_retries: int = 2

    @property
    def api_key(self) -> str | None:
        if self.provider == "openai":
            return os.environ.get("OPENAI_API_KEY")
        if self.provider == "anthropic":
            return os.environ.get("ANTHROPIC_API_KEY")
        return None


class RAGSettings(BaseModel):
    """Configuration for the *existing* external RAG (net2tf_v3).

    ``path`` points at a checkout of that project. Nothing in it is modified; the loader
    imports it under a guarded ``sys.path`` entry and overrides its module-level path
    constants in memory.
    """

    backend: Literal["legacy_net2tf", "stub", "null"] = "stub"
    path: Path = Path("./external/rag")
    kb_dir: Path | None = None
    index_dir: Path = Path("./var/rag/index")
    generated_dir: Path = Path("./var/rag/generated")
    enable_ansible: bool = False
    timeout_seconds: float = 900.0
    top_k: int = 6

    def resolved_kb_dir(self) -> Path:
        return self.kb_dir if self.kb_dir is not None else self.path / "kb"


class OrchestrationSettings(BaseModel):
    checkpointer: Literal["memory", "sqlite", "postgres"] = "sqlite"
    sqlite_path: Path = Path("./var/checkpoints.sqlite")
    postgres_url: str | None = None
    recursion_limit: int = 60
    node_timeout_seconds: float = 900.0


class JobSettings(BaseModel):
    backend: Literal["inprocess", "sqs"] = "inprocess"
    sqs_queue_url: str | None = None
    visibility_timeout_seconds: int = 900
    max_attempts: int = 3
    poll_interval_seconds: float = 1.0


class DeploymentSettings(BaseModel):
    backend: Literal["terraform", "stub", "null"] = "stub"
    terraform_binary: str = "terraform"
    workspace_root: Path = Path("./var/deployments")
    #: Master kill switch. Even with an approval record, apply is refused when false.
    allow_apply: bool = False
    aws_region: str = "eu-west-1"
    aws_role_arn: str | None = None
    command_timeout_seconds: float = 3600.0


class TelemetrySettings(BaseModel):
    enabled: bool = False
    service_name: str = "topoforge-api"
    otlp_endpoint: str | None = None
    sample_ratio: float = 1.0


class Settings(BaseSettings):
    """Root configuration object. Obtain it through :func:`get_settings`."""

    if _HAS_PYDANTIC_SETTINGS:
        model_config = SettingsConfigDict(
            env_prefix=_ENV_PREFIX,
            env_nested_delimiter=_NESTED_DELIMITER,
            env_file=(".env",),
            env_file_encoding="utf-8",
            extra="ignore",
            case_sensitive=False,
        )

    env: Environment = Environment.DEVELOPMENT
    log_level: str = "INFO"
    log_format: Literal["json", "console"] = "json"

    api: APISettings = Field(default_factory=APISettings)
    database: DatabaseSettings = Field(default_factory=DatabaseSettings)
    storage: StorageSettings = Field(default_factory=StorageSettings)
    vision: VisionSettings = Field(default_factory=VisionSettings)
    llm: LLMSettings = Field(default_factory=LLMSettings)
    rag: RAGSettings = Field(default_factory=RAGSettings)
    orchestration: OrchestrationSettings = Field(default_factory=OrchestrationSettings)
    jobs: JobSettings = Field(default_factory=JobSettings)
    deployment: DeploymentSettings = Field(default_factory=DeploymentSettings)
    telemetry: TelemetrySettings = Field(default_factory=TelemetrySettings)

    @property
    def is_production(self) -> bool:
        return self.env is Environment.PRODUCTION

    def redacted_dump(self) -> dict[str, Any]:
        """Serialisable settings with anything secret-looking masked. Safe to log."""
        return _redact(self.model_dump(mode="json"))


def _is_secret(key: str, value: Any) -> bool:
    """A secret is a non-empty *string* under a secret-looking name.

    The value-type check matters: ``max_tokens`` contains "token" but holds an int and
    carries no secret.
    """
    if not isinstance(value, str) or not value:
        return False
    return any(marker in key.lower() for marker in SECRET_FIELD_MARKERS)


def _redact(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: REDACTED if _is_secret(key, item) else _redact(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_redact(item) for item in value]
    return value


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Process-wide settings singleton."""
    return Settings()


def reset_settings_cache() -> None:
    """Drop the cached settings. Tests use this after mutating the environment."""
    get_settings.cache_clear()
