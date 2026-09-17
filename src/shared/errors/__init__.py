"""Application-wide error taxonomy.

Every infrastructure exception is translated into one of these at the adapter boundary,
so the application and domain layers never see a vendor exception type. Nothing is
swallowed: an adapter either handles an error or re-raises it as a ``TopoForgeError``.
"""

from __future__ import annotations

from typing import Any

__all__ = [
    "ApprovalRequiredError",
    "ArchitectureNotFoundError",
    "ConfigurationError",
    "ConflictError",
    "ConnectorDetectionError",
    "DeploymentError",
    "DeploymentValidationError",
    "DetectionError",
    "DomainError",
    "InfrastructureError",
    "InvalidImageError",
    "JobError",
    "LLMProviderError",
    "LLMResponseValidationError",
    "ModelLoadError",
    "NotFoundError",
    "OCRError",
    "ObjectStorageError",
    "RAGError",
    "RAGUnavailableError",
    "SchemaVersionError",
    "TopoForgeError",
    "TopologyError",
    "UnsupportedInputError",
    "ValidationError",
    "WorkflowError",
]


class TopoForgeError(Exception):
    """Base class for every error raised deliberately by this system."""

    #: Stable, machine-readable code surfaced to API clients.
    code: str = "topoforge_error"
    #: Suggested HTTP status when this error reaches the presentation layer.
    http_status: int = 500

    def __init__(self, message: str, **details: Any) -> None:
        super().__init__(message)
        self.message = message
        self.details: dict[str, Any] = details

    def to_dict(self) -> dict[str, Any]:
        return {"code": self.code, "message": self.message, "details": self.details}

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"{type(self).__name__}(code={self.code!r}, message={self.message!r})"


# --------------------------------------------------------------------------- #
# Configuration / programming errors
# --------------------------------------------------------------------------- #
class ConfigurationError(TopoForgeError):
    code = "configuration_error"


# --------------------------------------------------------------------------- #
# Domain errors
# --------------------------------------------------------------------------- #
class DomainError(TopoForgeError):
    code = "domain_error"
    http_status = 422


class ValidationError(DomainError):
    """A domain object failed validation. Carries the structured issues."""

    code = "validation_error"
    http_status = 422

    def __init__(self, message: str, issues: list[Any] | None = None, **details: Any) -> None:
        super().__init__(message, **details)
        self.issues = issues or []

    def to_dict(self) -> dict[str, Any]:
        payload = super().to_dict()
        payload["issues"] = [
            issue.model_dump(mode="json") if hasattr(issue, "model_dump") else issue
            for issue in self.issues
        ]
        return payload


class SchemaVersionError(DomainError):
    code = "schema_version_error"
    http_status = 400


class TopologyError(DomainError):
    code = "topology_error"


# --------------------------------------------------------------------------- #
# Persistence / lookup
# --------------------------------------------------------------------------- #
class NotFoundError(TopoForgeError):
    code = "not_found"
    http_status = 404


class ArchitectureNotFoundError(NotFoundError):
    code = "architecture_not_found"


class ConflictError(TopoForgeError):
    code = "conflict"
    http_status = 409


# --------------------------------------------------------------------------- #
# Infrastructure adapters
# --------------------------------------------------------------------------- #
class InfrastructureError(TopoForgeError):
    code = "infrastructure_error"


class ModelLoadError(InfrastructureError):
    code = "model_load_error"


class InvalidImageError(TopoForgeError):
    code = "invalid_image"
    http_status = 400


class UnsupportedInputError(TopoForgeError):
    code = "unsupported_input"
    http_status = 400


class DetectionError(InfrastructureError):
    code = "detection_error"


class OCRError(InfrastructureError):
    code = "ocr_error"


class ConnectorDetectionError(InfrastructureError):
    code = "connector_detection_error"


class ObjectStorageError(InfrastructureError):
    code = "object_storage_error"


class LLMProviderError(InfrastructureError):
    code = "llm_provider_error"
    http_status = 502


class LLMResponseValidationError(LLMProviderError):
    """The model returned something that did not validate against the expected schema."""

    code = "llm_response_validation_error"
    http_status = 502


class RAGError(InfrastructureError):
    code = "rag_error"
    http_status = 502


class RAGUnavailableError(RAGError):
    code = "rag_unavailable"
    http_status = 503


# --------------------------------------------------------------------------- #
# Orchestration / jobs / deployment
# --------------------------------------------------------------------------- #
class WorkflowError(TopoForgeError):
    code = "workflow_error"


class JobError(TopoForgeError):
    code = "job_error"


class DeploymentValidationError(TopoForgeError):
    code = "deployment_validation_error"
    http_status = 422


class DeploymentError(TopoForgeError):
    code = "deployment_error"


class ApprovalRequiredError(DeploymentError):
    """Raised whenever execution is attempted without a valid approval record."""

    code = "approval_required"
    http_status = 403
