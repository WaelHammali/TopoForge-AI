"""Provenance.

Every machine-derived fact in a :class:`~src.domain.architecture.models.NetworkArchitecture`
carries an :class:`Evidence` record saying where it came from, how confident the producer
was, and — for vision-derived facts — where on the image it was seen.

Provenance is never lost when facts are merged. :func:`merge_evidence` keeps both sides
and records the winner, so the UI can always explain "why does R1 have this IP?".
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import Enum
from typing import Annotated, Any, Generic, TypeVar

from pydantic import BaseModel, ConfigDict, Field

from src.domain.common.geometry import BoundingBox

__all__ = ["Confidence", "Evidence", "ProvenanceSource", "Sourced", "merge_evidence"]

T = TypeVar("T")

#: Confidence is a probability-like score in [0, 1].
Confidence = Annotated[float, Field(ge=0.0, le=1.0)]


class ProvenanceSource(str, Enum):
    """Who produced a fact.

    The ordering in :data:`SOURCE_PRIORITY` decides conflicts: an explicit user statement
    always beats a machine inference, and a machine inference always beats a default.
    """

    USER = "USER"
    YOLO = "YOLO"
    OCR = "OCR"
    EDGE_DETECTOR = "EDGE_DETECTOR"
    LLM = "LLM"
    RAG = "RAG"
    DERIVED = "DERIVED"
    DEFAULT = "DEFAULT"


#: Higher wins. USER is authoritative; DEFAULT is the weakest possible claim.
SOURCE_PRIORITY: dict[ProvenanceSource, int] = {
    ProvenanceSource.USER: 100,
    ProvenanceSource.DERIVED: 70,
    ProvenanceSource.OCR: 60,
    ProvenanceSource.YOLO: 60,
    ProvenanceSource.EDGE_DETECTOR: 55,
    ProvenanceSource.LLM: 50,
    ProvenanceSource.RAG: 50,
    ProvenanceSource.DEFAULT: 10,
}


class Evidence(BaseModel):
    """Why the system believes a fact."""

    model_config = ConfigDict(frozen=True)

    source: ProvenanceSource
    confidence: Confidence = 1.0
    #: Free-text explanation intended for the user, e.g. "OCR text 3 px below R1's box".
    detail: str | None = None
    #: Where on the image the evidence was observed, when applicable.
    bbox: BoundingBox | None = None
    #: Identifier of the producing artefact: a model version, an LLM model id, a user id.
    producer: str | None = None
    #: Identifiers of the raw observations this fact was built from.
    observation_ids: tuple[str, ...] = ()
    #: Which asset (image) the observation belongs to.
    asset_id: str | None = None
    recorded_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @property
    def priority(self) -> tuple[int, float]:
        return (SOURCE_PRIORITY.get(self.source, 0), self.confidence)

    @classmethod
    def from_user(cls, detail: str | None = None, producer: str | None = None) -> Evidence:
        return cls(
            source=ProvenanceSource.USER, confidence=1.0, detail=detail, producer=producer
        )

    @classmethod
    def derived(cls, detail: str, confidence: float = 1.0) -> Evidence:
        return cls(source=ProvenanceSource.DERIVED, confidence=confidence, detail=detail)

    @classmethod
    def default_value(cls, detail: str) -> Evidence:
        return cls(source=ProvenanceSource.DEFAULT, confidence=0.5, detail=detail)


class Sourced(BaseModel, Generic[T]):
    """A value bound to the evidence that produced it, plus everything it superseded."""

    model_config = ConfigDict(frozen=True)

    value: T
    evidence: Evidence
    #: Evidence for values that lost a merge. Never discarded — this is the audit trail.
    superseded: tuple[Evidence, ...] = ()

    @classmethod
    def from_user(cls, value: T, detail: str | None = None) -> Sourced[T]:
        return cls(value=value, evidence=Evidence.from_user(detail))

    @property
    def confidence(self) -> float:
        return self.evidence.confidence

    @property
    def source(self) -> ProvenanceSource:
        return self.evidence.source

    def is_at_least(self, threshold: float) -> bool:
        return self.evidence.confidence >= threshold


def merge_evidence(current: Sourced[Any] | None, candidate: Sourced[Any]) -> Sourced[Any]:
    """Combine two claims about the same field, keeping the losing evidence.

    The winner is the claim with the higher ``(source priority, confidence)``. Ties keep
    the incumbent, so replaying the same extraction is idempotent.
    """
    if current is None:
        return candidate
    if candidate.evidence.priority > current.evidence.priority:
        return candidate.model_copy(
            update={
                "superseded": (*candidate.superseded, current.evidence, *current.superseded)
            }
        )
    return current.model_copy(
        update={"superseded": (*current.superseded, candidate.evidence, *candidate.superseded)}
    )
