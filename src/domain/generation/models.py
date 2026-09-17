"""Generated artifacts.

Generators return typed projects, never loose strings or a directory path. That makes
their output comparable (checksums), storable (object storage), testable (golden files)
and renderable (a file tree in the UI) without anyone touching a filesystem.

Determinism is a contract, not an aspiration: :meth:`GeneratedProject.fingerprint` is a
stable hash over every file, and the golden tests assert it does not move for a fixed
input.
"""

from __future__ import annotations

import hashlib
import posixpath
from datetime import UTC, datetime
from enum import Enum
from typing import Any, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

__all__ = [
    "AnsibleProject",
    "GeneratedFile",
    "GeneratedProject",
    "GeneratorError",
    "GeneratorValidationResult",
    "GeneratorWarning",
    "ProjectKind",
    "ValidationCheck",
    "WarningSeverity",
]


class ProjectKind(str, Enum):
    TERRAFORM = "terraform"
    ANSIBLE = "ansible"
    INVENTORY = "inventory"
    CLOUDFORMATION = "cloudformation"
    PULUMI = "pulumi"
    BICEP = "bicep"


class WarningSeverity(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class GeneratorWarning(BaseModel):
    """Something the generator could not do perfectly, but did not fail on."""

    model_config = ConfigDict(frozen=True)

    code: str
    message: str
    severity: WarningSeverity = WarningSeverity.MEDIUM
    #: Cloud resource ids concerned.
    subject_ids: tuple[str, ...] = ()
    file_path: str | None = None


class GeneratorError(BaseModel):
    """A structured generation failure. Raised as ``GenerationError`` by the port."""

    model_config = ConfigDict(frozen=True)

    code: str
    message: str
    subject_ids: tuple[str, ...] = ()
    remediation: str | None = None


class GeneratedFile(BaseModel):
    """One file in a generated project."""

    model_config = ConfigDict(frozen=True)

    #: POSIX-style path relative to the project root, e.g. "roles/nginx/tasks/main.yml".
    path: str
    content: str
    #: Unix mode, for scripts that must be executable.
    mode: str = "0644"
    #: Advisory content type for the UI's syntax highlighting.
    language: str | None = None

    @field_validator("path")
    @classmethod
    def _safe_relative_path(cls, value: str) -> str:
        """Reject anything that could escape the project root.

        Generated paths end up joined onto a workspace directory and an object-storage
        key. A traversal here would be a write-anywhere primitive.
        """
        cleaned = value.strip().replace("\\", "/")
        if not cleaned:
            raise ValueError("file path must not be empty")
        if cleaned.startswith("/") or cleaned.startswith("~"):
            raise ValueError(f"file path must be relative: {value!r}")
        if ".." in cleaned.split("/"):
            raise ValueError(f"file path must not traverse upwards: {value!r}")
        if cleaned != posixpath.normpath(cleaned):
            raise ValueError(f"file path must be normalised: {value!r}")
        return cleaned

    @property
    def checksum(self) -> str:
        """SHA-256 of the content. Used for change detection and golden tests."""
        return hashlib.sha256(self.content.encode("utf-8")).hexdigest()

    @property
    def size_bytes(self) -> int:
        return len(self.content.encode("utf-8"))

    @property
    def directory(self) -> str:
        return posixpath.dirname(self.path)


class GeneratedProject(BaseModel):
    """A complete set of generated files plus the metadata to reproduce them."""

    model_config = ConfigDict(frozen=True)

    kind: ProjectKind
    files: tuple[GeneratedFile, ...] = ()
    warnings: tuple[GeneratorWarning, ...] = ()
    generator_name: str
    generator_version: str
    #: The cloud architecture this was generated from.
    cloud_architecture_id: str | None = None
    cloud_architecture_revision: int | None = None
    #: Where the project root lives once persisted, e.g.
    #: "generated/{architecture_id}/{revision}/terraform".
    storage_prefix: str | None = None
    generated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    metadata: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _unique_paths(self) -> Self:
        paths = [file.path for file in self.files]
        duplicates = sorted({path for path in paths if paths.count(path) > 1})
        if duplicates:
            raise ValueError(f"duplicate generated file paths: {duplicates}")
        return self

    def file(self, path: str) -> GeneratedFile | None:
        return next((item for item in self.files if item.path == path), None)

    @property
    def file_paths(self) -> tuple[str, ...]:
        return tuple(sorted(file.path for file in self.files))

    @property
    def total_bytes(self) -> int:
        return sum(file.size_bytes for file in self.files)

    @property
    def is_empty(self) -> bool:
        return not self.files

    def fingerprint(self) -> str:
        """Stable hash over the whole project.

        Path-sorted so it does not depend on generation order. Excludes timestamps, so
        two runs over identical input fingerprint identically — that is the determinism
        contract the golden tests enforce.
        """
        digest = hashlib.sha256()
        for file in sorted(self.files, key=lambda item: item.path):
            digest.update(file.path.encode("utf-8"))
            digest.update(b"\0")
            digest.update(file.checksum.encode("ascii"))
            digest.update(b"\0")
        return digest.hexdigest()

    def tree(self) -> dict[str, Any]:
        """Nested dict of the file layout, for the UI's file-tree component."""
        root: dict[str, Any] = {}
        for file in sorted(self.files, key=lambda item: item.path):
            cursor = root
            parts = file.path.split("/")
            for part in parts[:-1]:
                cursor = cursor.setdefault(part, {})
            cursor[parts[-1]] = {"checksum": file.checksum, "size": file.size_bytes}
        return root

    def summary(self) -> dict[str, Any]:
        return {
            "kind": self.kind.value,
            "generator": f"{self.generator_name}@{self.generator_version}",
            "files": len(self.files),
            "bytes": self.total_bytes,
            "warnings": len(self.warnings),
            "fingerprint": self.fingerprint(),
        }


class TerraformProject(GeneratedProject):
    """A Terraform project. ``kind`` is fixed."""

    kind: ProjectKind = ProjectKind.TERRAFORM
    #: Directory inside the project that terraform commands run in.
    working_directory: str = "."
    #: Declared required providers, e.g. {"aws": "~> 5.0"}.
    required_providers: dict[str, str] = Field(default_factory=dict)
    #: Variables the operator must supply that have no default.
    required_variables: tuple[str, ...] = ()
    #: Outputs the project declares; the inventory generator consumes these after apply.
    declared_outputs: tuple[str, ...] = ()


class AnsibleProject(GeneratedProject):
    """An Ansible project. ``kind`` is fixed."""

    kind: ProjectKind = ProjectKind.ANSIBLE
    playbook_path: str = "site.yml"
    inventory_path: str | None = None
    #: True until a real inventory has been generated from Terraform outputs.
    #: The UI uses it to explain why the inventory has no addresses yet.
    inventory_pending: bool = True
    role_names: tuple[str, ...] = ()
    group_names: tuple[str, ...] = ()


class ValidationCheck(BaseModel):
    """One tool invocation in a validation run, e.g. ``terraform validate``."""

    model_config = ConfigDict(frozen=True)

    name: str
    passed: bool
    #: False when the tool is not installed — distinct from a failed check.
    executed: bool = True
    exit_code: int | None = None
    stdout: str | None = None
    stderr: str | None = None
    duration_ms: float | None = None
    skipped_reason: str | None = None


class GeneratorValidationResult(BaseModel):
    """The structured outcome of validating a generated project.

    Deliberately structured rather than a log dump: failures come back to the application
    as data, and never trigger an automatic LLM repair loop.
    """

    model_config = ConfigDict(frozen=True)

    project_kind: ProjectKind
    valid: bool
    checks: tuple[ValidationCheck, ...] = ()
    errors: tuple[GeneratorError, ...] = ()
    warnings: tuple[GeneratorWarning, ...] = ()
    #: Fingerprint of the project that was validated, so a stale result is detectable.
    project_fingerprint: str | None = None

    @classmethod
    def from_checks(
        cls,
        project_kind: ProjectKind,
        checks: tuple[ValidationCheck, ...],
        project_fingerprint: str | None = None,
        warnings: tuple[GeneratorWarning, ...] = (),
    ) -> GeneratorValidationResult:
        errors = tuple(
            GeneratorError(
                code=f"{check.name.replace(' ', '_')}_failed",
                message=(check.stderr or check.stdout or "check failed").strip()[:4000],
            )
            for check in checks
            if check.executed and not check.passed
        )
        return cls(
            project_kind=project_kind,
            valid=not errors,
            checks=checks,
            errors=errors,
            warnings=warnings,
            project_fingerprint=project_fingerprint,
        )

    @property
    def executed_checks(self) -> tuple[ValidationCheck, ...]:
        return tuple(check for check in self.checks if check.executed)

    @property
    def skipped_checks(self) -> tuple[ValidationCheck, ...]:
        return tuple(check for check in self.checks if not check.executed)
