"""Background work and event publication.

Long-running stages — YOLO inference, OCR, connector detection, RAG translation,
generation, planning, deployment — must never block an API request. They run as jobs.

The port is provider-neutral: an in-process queue in development, SQS in production, with
no change above this line.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any

__all__ = ["EventPublisher", "Job", "JobQueue", "JobStatus", "JobType", "WorkflowEvent"]


class JobStatus(str, Enum):
    """Lifecycle of a unit of work, as shown in the UI's progress display."""

    CREATED = "CREATED"
    UPLOADED = "UPLOADED"
    PREPROCESSING = "PREPROCESSING"
    DETECTING_COMPONENTS = "DETECTING_COMPONENTS"
    EXTRACTING_TEXT = "EXTRACTING_TEXT"
    DETECTING_CONNECTIONS = "DETECTING_CONNECTIONS"
    BUILDING_TOPOLOGY = "BUILDING_TOPOLOGY"
    ANALYZING_COMPLETENESS = "ANALYZING_COMPLETENESS"
    NEEDS_USER_INPUT = "NEEDS_USER_INPUT"
    ENRICHING_NETWORK = "ENRICHING_NETWORK"
    VALIDATING_NETWORK = "VALIDATING_NETWORK"
    NETWORK_ARCHITECTURE_READY = "NETWORK_ARCHITECTURE_READY"
    RUNNING_RAG = "RUNNING_RAG"
    CLOUD_ARCHITECTURE_READY = "CLOUD_ARCHITECTURE_READY"
    GENERATING_TERRAFORM = "GENERATING_TERRAFORM"
    GENERATING_ANSIBLE = "GENERATING_ANSIBLE"
    VALIDATING_TERRAFORM = "VALIDATING_TERRAFORM"
    VALIDATING_ANSIBLE = "VALIDATING_ANSIBLE"
    PLANNING_DEPLOYMENT = "PLANNING_DEPLOYMENT"
    PLAN_READY = "PLAN_READY"
    AWAITING_DEPLOYMENT_APPROVAL = "AWAITING_DEPLOYMENT_APPROVAL"
    DEPLOYING_INFRASTRUCTURE = "DEPLOYING_INFRASTRUCTURE"
    GENERATING_INVENTORY = "GENERATING_INVENTORY"
    CONFIGURING_HOSTS = "CONFIGURING_HOSTS"
    DEPLOYED = "DEPLOYED"
    REJECTED = "REJECTED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"

    @property
    def is_terminal(self) -> bool:
        return self in {
            JobStatus.DEPLOYED,
            JobStatus.REJECTED,
            JobStatus.FAILED,
            JobStatus.CANCELLED,
        }

    @property
    def is_waiting_for_user(self) -> bool:
        return self in {JobStatus.NEEDS_USER_INPUT, JobStatus.AWAITING_DEPLOYMENT_APPROVAL}


class JobType(str, Enum):
    IMAGE_EXTRACTION = "image_extraction"
    PROMPT_ARCHITECTURE = "prompt_architecture"
    RAG_TRANSLATION = "rag_translation"
    CODE_GENERATION = "code_generation"
    DEPLOYMENT_PLAN = "deployment_plan"
    DEPLOYMENT_APPLY = "deployment_apply"
    CONFIGURATION_RUN = "configuration_run"


@dataclass(frozen=True, slots=True)
class Job:
    id: str
    type: JobType
    status: JobStatus
    project_id: str | None = None
    architecture_id: str | None = None
    workflow_id: str | None = None
    payload: dict[str, Any] = field(default_factory=dict)
    attempts: int = 0
    max_attempts: int = 3
    progress: float = 0.0
    message: str | None = None
    error: dict[str, Any] | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


class JobQueue(ABC):
    @abstractmethod
    async def enqueue(self, job: Job) -> str: ...

    @abstractmethod
    async def dequeue(self, *, timeout_seconds: float = 20.0) -> Job | None: ...

    @abstractmethod
    async def complete(self, job_id: str) -> None: ...

    @abstractmethod
    async def fail(self, job_id: str, error: dict[str, Any], *, retry: bool = True) -> None: ...

    @abstractmethod
    async def heartbeat(self, job_id: str) -> None: ...


@dataclass(frozen=True, slots=True)
class WorkflowEvent:
    """An event streamed to the browser over SSE."""

    type: str
    workflow_id: str
    payload: dict[str, Any] = field(default_factory=dict)
    occurred_at: datetime | None = None
    sequence: int | None = None


class EventPublisher(ABC):
    """Publishes workflow progress. Fan-out to connected browsers."""

    @abstractmethod
    async def publish(self, event: WorkflowEvent) -> None: ...

    @abstractmethod
    def subscribe(self, workflow_id: str) -> AsyncIterator[WorkflowEvent]: ...
