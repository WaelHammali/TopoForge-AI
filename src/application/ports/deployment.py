"""Deployment execution ports.

Two executors, one strict order:

``InfrastructureExecutor``  init → validate → plan → **approval** → apply → outputs
``ConfigurationExecutor``   syntax-check → (after apply, with a real inventory) → run

The approval gate is a hard precondition, not a convention: :meth:`InfrastructureExecutor.apply`
takes an :class:`~src.domain.deployment.models.ApprovalRecord` and must verify that the
record approved *this exact plan*.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

from src.domain.deployment.models import ApprovalRecord, DeploymentPlan
from src.domain.generation.models import AnsibleProject, GeneratedFile, TerraformProject

from src.application.ports.generation import DeploymentOutputs

__all__ = [
    "ConfigurationExecutor",
    "ExecutionResult",
    "InfrastructureExecutor",
    "WorkspaceRef",
]


@dataclass(frozen=True, slots=True)
class WorkspaceRef:
    """Where a materialised project lives for a tool to operate on.

    A workspace is a working directory, never the business source of truth: the authority
    is the stored project in object storage.
    """

    id: str
    uri: str
    project_fingerprint: str | None = None


@dataclass(frozen=True, slots=True)
class ExecutionResult:
    """Outcome of one tool invocation."""

    command: str
    succeeded: bool
    exit_code: int | None = None
    stdout: str = ""
    stderr: str = ""
    duration_ms: float | None = None
    artifacts: dict[str, str] = field(default_factory=dict)


class InfrastructureExecutor(ABC):
    """Runs the infrastructure toolchain. Only this component may mutate a cloud account."""

    @property
    @abstractmethod
    def name(self) -> str: ...

    @abstractmethod
    async def materialise(self, project: TerraformProject) -> WorkspaceRef:
        """Write the project into an isolated workspace."""

    @abstractmethod
    async def initialise(self, workspace: WorkspaceRef) -> ExecutionResult: ...

    @abstractmethod
    async def validate(self, workspace: WorkspaceRef) -> ExecutionResult: ...

    @abstractmethod
    async def plan(self, workspace: WorkspaceRef) -> DeploymentPlan:
        """Produce a reviewable plan. Never mutates infrastructure."""

    @abstractmethod
    async def apply(
        self, workspace: WorkspaceRef, plan: DeploymentPlan, approval: ApprovalRecord
    ) -> ExecutionResult:
        """Apply a plan a human approved.

        Implementations must raise :class:`~src.shared.errors.ApprovalRequiredError` when
        the approval does not match this plan's checksum, when it is not an approval, or
        when applying is disabled by configuration. The plan is re-checked at apply time:
        an approval for a plan that has since changed is worthless.
        """

    @abstractmethod
    async def outputs(self, workspace: WorkspaceRef) -> DeploymentOutputs: ...

    @abstractmethod
    async def destroy(
        self, workspace: WorkspaceRef, approval: ApprovalRecord
    ) -> ExecutionResult: ...


class ConfigurationExecutor(ABC):
    """Runs the configuration toolchain against already-provisioned machines."""

    @property
    @abstractmethod
    def name(self) -> str: ...

    @abstractmethod
    async def materialise(
        self, project: AnsibleProject, inventory: GeneratedFile | None = None
    ) -> WorkspaceRef: ...

    @abstractmethod
    async def syntax_check(self, workspace: WorkspaceRef) -> ExecutionResult: ...

    @abstractmethod
    async def run(
        self, workspace: WorkspaceRef, approval: ApprovalRecord, *, check_mode: bool = False
    ) -> ExecutionResult:
        """Configure the deployed hosts. Requires the same approval discipline as apply."""

    @abstractmethod
    async def health(self) -> dict[str, Any]: ...
