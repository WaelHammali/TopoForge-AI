"""The canonical architecture: ``architecture.json``.

This is the single most important data contract in the system. Both input modes — image
extraction and prompt generation — converge here, and this is the *only* thing the RAG
ever receives.

Design rules:

* Strongly typed all the way down. No untyped dictionaries in the hot path.
* Versioned (:data:`CURRENT_SCHEMA_VERSION`) with an explicit upgrade seam.
* Immutable. Every change produces a new revision; history is never destroyed.
* Provenance-carrying. Anything a machine decided says so.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from enum import Enum
from typing import Any, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from src.domain.architecture.issues import (
    EnrichmentOption,
    MissingInformation,
    UnresolvedField,
    ValidationReport,
)
from src.domain.common.identifiers import new_id
from src.domain.network.models import Interface, Network, VLAN
from src.domain.routing.models import ACL, NATRule, RoutingPlan
from src.domain.topology.models import Edge, Node, Topology
from src.shared.errors import SchemaVersionError

__all__ = [
    "ArchitectureMetadata",
    "ArchitectureRevision",
    "ArchitectureSource",
    "ArchitectureStatus",
    "CURRENT_SCHEMA_VERSION",
    "NetworkArchitecture",
    "RevisionChangeKind",
    "SUPPORTED_SCHEMA_VERSIONS",
]

CURRENT_SCHEMA_VERSION = "1.0"
SUPPORTED_SCHEMA_VERSIONS = frozenset({"1.0"})


class ArchitectureSource(str, Enum):
    IMAGE = "image"
    PROMPT = "prompt"
    #: An image-derived architecture later modified conversationally.
    IMAGE_PROMPT = "image_prompt"
    MANUAL = "manual"
    IMPORT = "import"


class ArchitectureStatus(str, Enum):
    DRAFT = "draft"
    NEEDS_INPUT = "needs_input"
    VALIDATED = "validated"
    INVALID = "invalid"


class ArchitectureMetadata(BaseModel):
    model_config = ConfigDict(frozen=True)

    architecture_id: str = Field(default_factory=lambda: new_id("arch"))
    name: str = "Untitled architecture"
    description: str | None = None
    source: ArchitectureSource = ArchitectureSource.MANUAL
    project_id: str | None = None
    #: Asset (image) ids this architecture was extracted from.
    asset_ids: tuple[str, ...] = ()
    #: The prompt text, when the source is prompt-driven.
    prompt: str | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    revision: int = Field(default=1, ge=1)
    created_by: str | None = None
    #: Versions of the components that produced this architecture, for reproducibility.
    producers: dict[str, str] = Field(default_factory=dict)
    tags: tuple[str, ...] = ()


class NetworkArchitecture(BaseModel):
    """The canonical, validated description of a network.

    Serialises to ``architecture.json``. Every consumer — the JSON editor, the RAG
    adapter, the revision store, the API — reads this exact shape.
    """

    model_config = ConfigDict(frozen=True)

    schema_version: str = CURRENT_SCHEMA_VERSION
    metadata: ArchitectureMetadata = Field(default_factory=ArchitectureMetadata)

    nodes: tuple[Node, ...] = ()
    edges: tuple[Edge, ...] = ()
    interfaces: tuple[Interface, ...] = ()
    networks: tuple[Network, ...] = ()
    vlans: tuple[VLAN, ...] = ()

    routing: RoutingPlan = Field(default_factory=RoutingPlan)
    nat: tuple[NATRule, ...] = ()
    acls: tuple[ACL, ...] = ()

    #: Fields explicitly known to be unknown. Never silently defaulted.
    unresolved: tuple[UnresolvedField, ...] = ()
    #: Gaps the completeness analyzer found, retained for the UI and for resume.
    missing_information: tuple[MissingInformation, ...] = ()
    #: Enrichment the user may still choose to apply.
    enrichment_options: tuple[EnrichmentOption, ...] = ()

    validation: ValidationReport = Field(default_factory=ValidationReport)
    status: ArchitectureStatus = ArchitectureStatus.DRAFT

    #: Anything a producer wants to carry that is not (yet) first-class schema.
    extensions: dict[str, Any] = Field(default_factory=dict)

    # ------------------------------------------------------------------ #
    # Validation of the document itself (not of the network it describes)
    # ------------------------------------------------------------------ #
    @field_validator("schema_version")
    @classmethod
    def _known_version(cls, value: str) -> str:
        if value not in SUPPORTED_SCHEMA_VERSIONS:
            raise SchemaVersionError(
                f"unsupported architecture schema version {value!r}",
                supported=sorted(SUPPORTED_SCHEMA_VERSIONS),
            )
        return value

    @model_validator(mode="after")
    def _referential_integrity(self) -> Self:
        """Structural integrity only.

        Network *correctness* is the validators' job. This check exists so that a
        structurally impossible document (an edge to a node that does not exist) can never
        be constructed at all.
        """
        node_ids = {node.id for node in self.nodes}
        if len(node_ids) != len(self.nodes):
            raise ValueError("duplicate node ids in architecture")

        interface_ids = {iface.id for iface in self.interfaces}
        if len(interface_ids) != len(self.interfaces):
            raise ValueError("duplicate interface ids in architecture")

        for iface in self.interfaces:
            if iface.node_id not in node_ids:
                raise ValueError(f"interface {iface.id!r} references unknown node {iface.node_id!r}")

        for edge in self.edges:
            for endpoint in (edge.source, edge.target):
                if endpoint.node_id not in node_ids:
                    raise ValueError(
                        f"edge {edge.id!r} references unknown node {endpoint.node_id!r}"
                    )
                if endpoint.interface_id is not None and endpoint.interface_id not in interface_ids:
                    raise ValueError(
                        f"edge {edge.id!r} references unknown interface {endpoint.interface_id!r}"
                    )
        return self

    # ------------------------------------------------------------------ #
    # Lookups
    # ------------------------------------------------------------------ #
    @property
    def architecture_id(self) -> str:
        return self.metadata.architecture_id

    @property
    def revision(self) -> int:
        return self.metadata.revision

    def node(self, node_id: str) -> Node | None:
        return next((node for node in self.nodes if node.id == node_id), None)

    def interface(self, interface_id: str) -> Interface | None:
        return next((iface for iface in self.interfaces if iface.id == interface_id), None)

    def interfaces_of(self, node_id: str) -> tuple[Interface, ...]:
        return tuple(iface for iface in self.interfaces if iface.node_id == node_id)

    def edges_of(self, node_id: str) -> tuple[Edge, ...]:
        return tuple(edge for edge in self.edges if edge.connects(node_id))

    def as_topology(self) -> Topology:
        return Topology(
            nodes=self.nodes,
            edges=self.edges,
            interfaces=self.interfaces,
            networks=self.networks,
            vlans=self.vlans,
        )

    # ------------------------------------------------------------------ #
    # Gates
    # ------------------------------------------------------------------ #
    @property
    def is_valid(self) -> bool:
        return self.validation.valid and not self.validation.errors

    @property
    def can_proceed_to_rag(self) -> bool:
        """The one gate in the system.

        An architecture reaches the RAG only when validation has run and produced no
        errors, and no REQUIRED information is still missing and unanswered.
        """
        if self.validation.blocks_rag:
            return False
        return not any(item.blocks_progress for item in self.missing_information)

    @property
    def blocking_reasons(self) -> tuple[str, ...]:
        reasons = [f"{issue.code}: {issue.message}" for issue in self.validation.errors]
        reasons.extend(
            f"{item.code}: {item.summary}"
            for item in self.missing_information
            if item.blocks_progress
        )
        return tuple(reasons)

    # ------------------------------------------------------------------ #
    # Copy-on-write helpers
    # ------------------------------------------------------------------ #
    def touched(self, **metadata_updates: Any) -> NetworkArchitecture:
        """Bump ``updated_at`` (and optionally other metadata) without changing content."""
        metadata = self.metadata.model_copy(
            update={"updated_at": datetime.now(UTC), **metadata_updates}
        )
        return self.model_copy(update={"metadata": metadata})

    def next_revision(self, **metadata_updates: Any) -> NetworkArchitecture:
        """Return a copy whose metadata is advanced to the next revision number."""
        metadata = self.metadata.model_copy(
            update={
                "revision": self.metadata.revision + 1,
                "updated_at": datetime.now(UTC),
                **metadata_updates,
            }
        )
        return self.model_copy(update={"metadata": metadata})

    def with_validation(self, report: ValidationReport) -> NetworkArchitecture:
        status = ArchitectureStatus.VALIDATED if report.valid else ArchitectureStatus.INVALID
        if report.valid and any(item.blocks_progress for item in self.missing_information):
            status = ArchitectureStatus.NEEDS_INPUT
        return self.model_copy(update={"validation": report, "status": status})

    # ------------------------------------------------------------------ #
    # Serialisation
    # ------------------------------------------------------------------ #
    def to_json_dict(self) -> dict[str, Any]:
        return self.model_dump(mode="json", exclude_none=False)

    def to_json(self, *, indent: int = 2) -> str:
        return json.dumps(self.to_json_dict(), indent=indent, sort_keys=False, default=str)

    @classmethod
    def from_json_dict(cls, payload: dict[str, Any]) -> NetworkArchitecture:
        version = payload.get("schema_version", CURRENT_SCHEMA_VERSION)
        if version not in SUPPORTED_SCHEMA_VERSIONS:
            raise SchemaVersionError(
                f"cannot load architecture with schema version {version!r}",
                supported=sorted(SUPPORTED_SCHEMA_VERSIONS),
            )
        return cls.model_validate(payload)

    @classmethod
    def from_json(cls, text: str) -> NetworkArchitecture:
        return cls.from_json_dict(json.loads(text))

    def summary(self) -> dict[str, Any]:
        """Compact description used in logs, list views and workflow state."""
        return {
            "architecture_id": self.architecture_id,
            "revision": self.revision,
            "source": self.metadata.source.value,
            "status": self.status.value,
            "nodes": len(self.nodes),
            "edges": len(self.edges),
            "interfaces": len(self.interfaces),
            "networks": len(self.networks),
            "vlans": len(self.vlans),
            "routing_mode": self.routing.mode.value,
            "errors": len(self.validation.errors),
            "warnings": len(self.validation.warnings),
            "missing_required": sum(
                1 for item in self.missing_information if item.blocks_progress
            ),
            "can_proceed_to_rag": self.can_proceed_to_rag,
        }


class RevisionChangeKind(str, Enum):
    EXTRACTION = "extraction"
    PROMPT_GENERATION = "prompt_generation"
    USER_CORRECTION = "user_correction"
    CLARIFICATION_ANSWER = "clarification_answer"
    ENRICHMENT = "enrichment"
    MANUAL_JSON_EDIT = "manual_json_edit"
    CONVERSATIONAL_MODIFICATION = "conversational_modification"
    VALIDATION = "validation"
    RAG_ANNOTATION = "rag_annotation"
    RESTORE = "restore"


class ArchitectureRevision(BaseModel):
    """One immutable point in an architecture's history.

    Revision history is a product feature, not an orchestration detail: it is stored in
    the business database and survives independently of LangGraph checkpoints, which are
    prunable.
    """

    model_config = ConfigDict(frozen=True)

    id: str = Field(default_factory=lambda: new_id("rev"))
    architecture_id: str
    revision: int = Field(ge=1)
    change_kind: RevisionChangeKind
    change_reason: str
    architecture: NetworkArchitecture
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    created_by: str | None = None
    #: Revision this one was derived from. ``None`` only for revision 1.
    parent_revision: int | None = None
    #: Human-readable list of what changed, computed at creation time.
    changed_paths: tuple[str, ...] = ()

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        if self.architecture.metadata.revision != self.revision:
            raise ValueError(
                "revision number mismatch between the record and the architecture it holds: "
                f"{self.revision} vs {self.architecture.metadata.revision}"
            )
        if self.architecture.metadata.architecture_id != self.architecture_id:
            raise ValueError("architecture id mismatch between the record and its architecture")
        if self.revision == 1 and self.parent_revision is not None:
            raise ValueError("revision 1 cannot have a parent")
        if self.revision > 1 and self.parent_revision is None:
            raise ValueError(f"revision {self.revision} must record its parent")
        return self

    @classmethod
    def initial(
        cls,
        architecture: NetworkArchitecture,
        change_kind: RevisionChangeKind,
        change_reason: str,
        created_by: str | None = None,
    ) -> ArchitectureRevision:
        first = architecture.model_copy(
            update={"metadata": architecture.metadata.model_copy(update={"revision": 1})}
        )
        return cls(
            architecture_id=first.architecture_id,
            revision=1,
            change_kind=change_kind,
            change_reason=change_reason,
            architecture=first,
            created_by=created_by,
        )

    def succeed(
        self,
        architecture: NetworkArchitecture,
        change_kind: RevisionChangeKind,
        change_reason: str,
        created_by: str | None = None,
        changed_paths: tuple[str, ...] = (),
    ) -> ArchitectureRevision:
        """Create the next revision from this one. The current revision is untouched."""
        advanced = architecture.model_copy(
            update={
                "metadata": architecture.metadata.model_copy(
                    update={
                        "revision": self.revision + 1,
                        "architecture_id": self.architecture_id,
                        "updated_at": datetime.now(UTC),
                    }
                )
            }
        )
        return ArchitectureRevision(
            architecture_id=self.architecture_id,
            revision=self.revision + 1,
            change_kind=change_kind,
            change_reason=change_reason,
            architecture=advanced,
            created_by=created_by,
            parent_revision=self.revision,
            changed_paths=changed_paths,
        )
