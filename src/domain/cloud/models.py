"""``cloud_architecture.json`` — the RAG's output and the generators' input.

This is the second canonical contract in the system. The RAG produces it and then stops:
it writes no HCL, no YAML and no shell. Deterministic generators consume it.

The model is deliberately provider-neutral in structure with an AWS-first vocabulary, so
that a future Azure or GCP mapping is a new set of ``provider_type`` values and a new
generator, not a schema rewrite.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from enum import Enum
from typing import Any, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from src.domain.architecture.issues import UnresolvedField, ValidationReport
from src.domain.cloud.configuration import Configuration
from src.domain.cloud.resources import CloudProvider, Infrastructure
from src.domain.common.identifiers import new_id
from src.domain.common.provenance import Evidence
from src.shared.errors import SchemaVersionError

__all__ = [
    "CLOUD_SCHEMA_VERSION",
    "SUPPORTED_CLOUD_SCHEMA_VERSIONS",
    "Assumption",
    "CloudArchitecture",
    "CloudArchitectureMetadata",
    "CloudWarning",
    "IAMRequirement",
    "Relationship",
    "RelationshipType",
    "RetrievedReference",
    "WarningSeverity",
]

CLOUD_SCHEMA_VERSION = "1.0"
SUPPORTED_CLOUD_SCHEMA_VERSIONS = frozenset({"1.0"})


class RelationshipType(str, Enum):
    CONTAINS = "contains"
    ATTACHED_TO = "attached_to"
    ROUTES_VIA = "routes_via"
    PEERED_WITH = "peered_with"
    SECURED_BY = "secured_by"
    TARGETS = "targets"
    CONFIGURED_BY = "configured_by"
    DERIVED_FROM = "derived_from"


class Relationship(BaseModel):
    """A typed edge between two cloud resources.

    Relationships are *architecture*, not ordering hints. Terraform dependency ordering is
    derived by the generator from real references; it is not transcribed from here.
    """

    model_config = ConfigDict(frozen=True)

    id: str
    type: RelationshipType
    source_id: str
    target_id: str
    description: str | None = None
    attributes: dict[str, Any] = Field(default_factory=dict)


class IAMRequirement(BaseModel):
    """An identity/permission the architecture needs. Never a credential."""

    model_config = ConfigDict(frozen=True)

    id: str
    name: str
    description: str
    #: Resource ids that assume or use this identity.
    principal_resource_ids: tuple[str, ...] = ()
    #: Coarse capability statements, e.g. ("s3:GetObject on artifacts bucket",).
    capabilities: tuple[str, ...] = ()
    managed_policy_refs: tuple[str, ...] = ()


class Assumption(BaseModel):
    """Something the RAG decided in the absence of an explicit instruction."""

    model_config = ConfigDict(frozen=True)

    id: str
    statement: str
    rationale: str | None = None
    #: Knowledge-base chunk headings that supported the assumption.
    supported_by: tuple[str, ...] = ()
    affected_resource_ids: tuple[str, ...] = ()
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)


class WarningSeverity(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class CloudWarning(BaseModel):
    """A non-blocking concern about the translation."""

    model_config = ConfigDict(frozen=True)

    id: str
    code: str
    message: str
    severity: WarningSeverity = WarningSeverity.MEDIUM
    affected_resource_ids: tuple[str, ...] = ()
    remediation: str | None = None


class RetrievedReference(BaseModel):
    """A knowledge-base chunk the RAG used. Enough to explain, never raw reasoning."""

    model_config = ConfigDict(frozen=True)

    source: str
    heading: str
    score: float | None = None
    excerpt: str | None = None


class CloudArchitectureMetadata(BaseModel):
    model_config = ConfigDict(frozen=True)

    cloud_architecture_id: str = Field(default_factory=lambda: new_id("cloud"))
    name: str = "Untitled cloud architecture"
    provider: CloudProvider = CloudProvider.AWS
    region: str = "eu-west-1"
    #: The exact network architecture this was translated from.
    network_architecture_id: str | None = None
    network_architecture_revision: int | None = None
    project_id: str | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    revision: int = Field(default=1, ge=1)
    #: Which RAG / knowledge-base / model versions produced this. Reproducibility.
    producers: dict[str, str] = Field(default_factory=dict)
    #: The RAG's chosen deployment pattern, e.g. "multi_vpc_peering".
    deployment_pattern: str | None = None
    #: The RAG's confidence in its own translation.
    confidence: str | None = None


class CloudArchitecture(BaseModel):
    """The validated cloud translation of a network architecture.

    Split into :attr:`infrastructure` (what Terraform provisions) and
    :attr:`configuration` (what Ansible configures), with no overlap between them.
    """

    model_config = ConfigDict(frozen=True)

    schema_version: str = CLOUD_SCHEMA_VERSION
    metadata: CloudArchitectureMetadata = Field(default_factory=CloudArchitectureMetadata)

    infrastructure: Infrastructure = Field(default_factory=Infrastructure)
    configuration: Configuration = Field(default_factory=Configuration)

    relationships: tuple[Relationship, ...] = ()
    iam: tuple[IAMRequirement, ...] = ()

    assumptions: tuple[Assumption, ...] = ()
    warnings: tuple[CloudWarning, ...] = ()
    unresolved: tuple[UnresolvedField, ...] = ()

    #: Knowledge the RAG retrieved, for the "why did it decide that" view.
    references: tuple[RetrievedReference, ...] = ()
    #: Network-architecture concepts that could not be represented in the cloud model.
    #: Reported, never silently dropped.
    unmapped: tuple[str, ...] = ()

    validation: ValidationReport = Field(default_factory=ValidationReport)
    evidence: Evidence | None = None
    extensions: dict[str, Any] = Field(default_factory=dict)

    # ------------------------------------------------------------------ #
    @field_validator("schema_version")
    @classmethod
    def _known_version(cls, value: str) -> str:
        if value not in SUPPORTED_CLOUD_SCHEMA_VERSIONS:
            raise SchemaVersionError(
                f"unsupported cloud architecture schema version {value!r}",
                supported=sorted(SUPPORTED_CLOUD_SCHEMA_VERSIONS),
            )
        return value

    @model_validator(mode="after")
    def _referential_integrity(self) -> Self:
        """Structural integrity only; cloud *correctness* is the validators' job."""
        resource_ids = {resource.id for resource in self.infrastructure.all_resources()}
        if len(resource_ids) != len(self.infrastructure.all_resources()):
            raise ValueError("duplicate resource ids in cloud infrastructure")

        network_ids = {network.id for network in self.infrastructure.networks}
        for subnet in self.infrastructure.subnets:
            if subnet.network_id not in network_ids:
                raise ValueError(
                    f"subnet {subnet.id!r} references unknown network {subnet.network_id!r}"
                )

        subnet_ids = {subnet.id for subnet in self.infrastructure.subnets}
        for instance in self.infrastructure.compute:
            if instance.subnet_id is not None and instance.subnet_id not in subnet_ids:
                raise ValueError(
                    f"compute {instance.id!r} references unknown subnet {instance.subnet_id!r}"
                )
            for nic in instance.interfaces:
                if nic.subnet_id not in subnet_ids:
                    raise ValueError(
                        f"interface {nic.id!r} references unknown subnet {nic.subnet_id!r}"
                    )

        compute_ids = {instance.id for instance in self.infrastructure.compute}
        for host in self.configuration.hosts:
            if host.compute_id not in compute_ids:
                raise ValueError(
                    f"configuration host {host.id!r} references unknown compute "
                    f"{host.compute_id!r}"
                )

        for relationship in self.relationships:
            for endpoint in (relationship.source_id, relationship.target_id):
                if endpoint not in resource_ids:
                    raise ValueError(
                        f"relationship {relationship.id!r} references unknown resource "
                        f"{endpoint!r}"
                    )
        return self

    # ------------------------------------------------------------------ #
    @property
    def cloud_architecture_id(self) -> str:
        return self.metadata.cloud_architecture_id

    @property
    def provider(self) -> CloudProvider:
        return self.metadata.provider

    @property
    def region(self) -> str:
        return self.metadata.region

    @property
    def is_valid(self) -> bool:
        return self.validation.valid and not self.validation.errors

    @property
    def can_generate(self) -> bool:
        """Gate between the RAG and the generators.

        A cloud architecture must be valid and have something to provision before any
        generator runs.
        """
        return self.is_valid and not self.infrastructure.is_empty

    @property
    def requires_configuration_stage(self) -> bool:
        """False for pure-infrastructure translations; the Ansible node is then skipped."""
        return not self.configuration.is_empty

    def relationships_of(self, resource_id: str) -> tuple[Relationship, ...]:
        return tuple(
            item
            for item in self.relationships
            if resource_id in (item.source_id, item.target_id)
        )

    def with_validation(self, report: ValidationReport) -> CloudArchitecture:
        return self.model_copy(update={"validation": report})

    # ------------------------------------------------------------------ #
    def to_json_dict(self) -> dict[str, Any]:
        return self.model_dump(mode="json")

    def to_json(self, *, indent: int = 2) -> str:
        return json.dumps(self.to_json_dict(), indent=indent, default=str)

    @classmethod
    def from_json_dict(cls, payload: dict[str, Any]) -> CloudArchitecture:
        version = payload.get("schema_version", CLOUD_SCHEMA_VERSION)
        if version not in SUPPORTED_CLOUD_SCHEMA_VERSIONS:
            raise SchemaVersionError(
                f"cannot load cloud architecture with schema version {version!r}",
                supported=sorted(SUPPORTED_CLOUD_SCHEMA_VERSIONS),
            )
        return cls.model_validate(payload)

    @classmethod
    def from_json(cls, text: str) -> CloudArchitecture:
        return cls.from_json_dict(json.loads(text))

    def summary(self) -> dict[str, Any]:
        return {
            "cloud_architecture_id": self.cloud_architecture_id,
            "provider": self.provider.value,
            "region": self.region,
            "network_architecture_id": self.metadata.network_architecture_id,
            "network_architecture_revision": self.metadata.network_architecture_revision,
            "deployment_pattern": self.metadata.deployment_pattern,
            **self.infrastructure.counts(),
            **{f"config_{key}": value for key, value in self.configuration.counts().items()},
            "relationships": len(self.relationships),
            "assumptions": len(self.assumptions),
            "warnings": len(self.warnings),
            "unmapped": len(self.unmapped),
            "errors": len(self.validation.errors),
            "can_generate": self.can_generate,
            "requires_configuration_stage": self.requires_configuration_stage,
        }
