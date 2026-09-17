"""Code generation ports.

Generators are **pure deterministic functions of a cloud architecture**. They never call
an LLM, never call the RAG, never read the network, and produce byte-identical output for
identical input. That is what makes golden-file testing possible and what keeps generated
code out of any model's context.

The set is open by design: a ``CloudFormationGenerator`` or ``PulumiGenerator`` is a new
implementation of :class:`CodeGenerator` and nothing upstream changes.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

from src.domain.cloud.models import CloudArchitecture
from src.domain.generation.models import (
    AnsibleProject,
    GeneratedFile,
    GeneratedProject,
    GeneratorValidationResult,
    ProjectKind,
    TerraformProject,
)

__all__ = [
    "AnsibleGenerator",
    "CodeGenerator",
    "DeploymentOutputs",
    "InventoryGenerator",
    "ProjectValidator",
    "TerraformGenerator",
]


@dataclass(frozen=True, slots=True)
class DeploymentOutputs:
    """Real values produced by a completed infrastructure deployment.

    This is the only source of runtime addresses in the system. Nothing before apply is
    allowed to invent one.
    """

    #: Raw output name -> value, as reported by the executor.
    values: dict[str, Any] = field(default_factory=dict)
    #: compute resource id -> reachable address, resolved from ``values``.
    compute_addresses: dict[str, str] = field(default_factory=dict)
    #: compute resource id -> private address, when different.
    compute_private_addresses: dict[str, str] = field(default_factory=dict)
    deployment_id: str | None = None

    def address_for(self, compute_id: str) -> str | None:
        return self.compute_addresses.get(compute_id)

    @property
    def is_empty(self) -> bool:
        return not self.values and not self.compute_addresses


class CodeGenerator(ABC):
    """Turns a cloud architecture into a set of files."""

    @property
    @abstractmethod
    def name(self) -> str: ...

    @property
    @abstractmethod
    def version(self) -> str:
        """Bumped whenever output changes. Golden tests pin this."""

    @property
    @abstractmethod
    def kind(self) -> ProjectKind: ...

    @abstractmethod
    def generate(self, architecture: CloudArchitecture) -> GeneratedProject:
        """Generate deterministically.

        Synchronous on purpose: this is CPU-bound template rendering with no I/O, so there
        is nothing to await, and keeping it sync makes it trivially testable.

        Raises :class:`~src.shared.errors.GenerationError` when the architecture cannot be
        represented at all. Partial representability is reported as warnings on the
        returned project.
        """


class TerraformGenerator(CodeGenerator):
    """Provisioning. Consumes ``architecture.infrastructure``."""

    @abstractmethod
    def generate(self, architecture: CloudArchitecture) -> TerraformProject: ...


class AnsibleGenerator(CodeGenerator):
    """Configuration. Consumes ``architecture.configuration``.

    Does **not** produce an inventory with addresses: those do not exist until apply.
    """

    @abstractmethod
    def generate(self, architecture: CloudArchitecture) -> AnsibleProject: ...


class InventoryGenerator(ABC):
    """Builds a real Ansible inventory from real deployment outputs.

    Runs *after* apply, never before. This is the component that keeps the rest of the
    system honest about the difference between a plan and a deployed reality.
    """

    @property
    @abstractmethod
    def name(self) -> str: ...

    @abstractmethod
    def generate(
        self, architecture: CloudArchitecture, outputs: DeploymentOutputs
    ) -> GeneratedFile: ...


class ProjectValidator(ABC):
    """Validates a generated project with the real toolchain where it is available.

    Failures come back as structured data. They never trigger an automatic LLM repair
    loop: that would send generated code back into a model, which this architecture
    forbids.
    """

    @property
    @abstractmethod
    def name(self) -> str: ...

    @abstractmethod
    async def validate(self, project: GeneratedProject) -> GeneratorValidationResult: ...
