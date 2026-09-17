"""Persistence ports.

Repositories speak in domain objects. No SQLAlchemy type, session or query language
crosses this boundary.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from src.domain.architecture.models import ArchitectureRevision, NetworkArchitecture
from src.domain.cloud.models import CloudArchitecture
from src.domain.deployment.models import ApprovalRecord, DeploymentState

from src.application.ports.jobs import Job

__all__ = [
    "ArchitectureRepository",
    "AssetRepository",
    "AuditEvent",
    "AuditRepository",
    "CloudArchitectureRepository",
    "DeploymentRepository",
    "JobRepository",
    "Project",
    "ProjectAsset",
    "ProjectRepository",
]


@dataclass(frozen=True, slots=True)
class Project:
    id: str
    name: str
    description: str | None = None
    owner_id: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ProjectAsset:
    """An uploaded image. The bytes live in object storage; this is the record."""

    id: str
    project_id: str
    filename: str
    storage_key: str
    content_type: str
    size_bytes: int
    width: int | None = None
    height: int | None = None
    checksum: str | None = None
    #: Processing status shown on the thumbnail.
    status: str = "not_processed"
    architecture_id: str | None = None
    created_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class AuditEvent:
    id: str
    action: str
    actor: str | None
    subject_type: str
    subject_id: str
    occurred_at: datetime
    details: dict[str, Any] = field(default_factory=dict)


class ProjectRepository(ABC):
    @abstractmethod
    async def create(self, project: Project) -> Project: ...

    @abstractmethod
    async def get(self, project_id: str) -> Project | None: ...

    @abstractmethod
    async def list(self, *, owner_id: str | None = None, limit: int = 50) -> list[Project]: ...

    @abstractmethod
    async def delete(self, project_id: str) -> None: ...


class AssetRepository(ABC):
    @abstractmethod
    async def create(self, asset: ProjectAsset) -> ProjectAsset: ...

    @abstractmethod
    async def get(self, asset_id: str) -> ProjectAsset | None: ...

    @abstractmethod
    async def list_for_project(self, project_id: str) -> list[ProjectAsset]: ...

    @abstractmethod
    async def update_status(self, asset_id: str, status: str) -> None: ...


class ArchitectureRepository(ABC):
    """Network architectures and their revision history.

    History is a product feature stored here, independent of LangGraph checkpoints, which
    are an orchestration detail and may be pruned.
    """

    @abstractmethod
    async def save_revision(self, revision: ArchitectureRevision) -> ArchitectureRevision: ...

    @abstractmethod
    async def get_latest(self, architecture_id: str) -> NetworkArchitecture | None: ...

    @abstractmethod
    async def get_revision(
        self, architecture_id: str, revision: int
    ) -> ArchitectureRevision | None: ...

    @abstractmethod
    async def list_revisions(self, architecture_id: str) -> list[ArchitectureRevision]: ...

    @abstractmethod
    async def list_for_project(self, project_id: str) -> list[NetworkArchitecture]: ...


class CloudArchitectureRepository(ABC):
    @abstractmethod
    async def save(self, architecture: CloudArchitecture) -> CloudArchitecture: ...

    @abstractmethod
    async def get(self, cloud_architecture_id: str) -> CloudArchitecture | None: ...

    @abstractmethod
    async def get_for_network_architecture(
        self, network_architecture_id: str, revision: int | None = None
    ) -> CloudArchitecture | None: ...


class JobRepository(ABC):
    @abstractmethod
    async def save(self, job: Job) -> Job: ...

    @abstractmethod
    async def get(self, job_id: str) -> Job | None: ...

    @abstractmethod
    async def list_for_project(self, project_id: str) -> list[Job]: ...


class DeploymentRepository(ABC):
    @abstractmethod
    async def save(self, deployment: DeploymentState) -> DeploymentState: ...

    @abstractmethod
    async def get(self, deployment_id: str) -> DeploymentState | None: ...

    @abstractmethod
    async def record_approval(self, approval: ApprovalRecord) -> ApprovalRecord:
        """Persist a decision. Must be durable *before* any apply is attempted."""

    @abstractmethod
    async def get_approval(self, deployment_id: str) -> ApprovalRecord | None: ...


class AuditRepository(ABC):
    @abstractmethod
    async def record(self, event: AuditEvent) -> None: ...

    @abstractmethod
    async def list_for_subject(self, subject_type: str, subject_id: str) -> list[AuditEvent]: ...
