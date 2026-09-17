"""RAG port — network architecture in, cloud architecture out.

This is the whole contract. The RAG:

* receives a validated :class:`~src.domain.architecture.models.NetworkArchitecture`;
* retrieves cloud knowledge and reasons about it;
* returns a :class:`~src.domain.cloud.models.CloudArchitecture`;
* and then stops.

It does not write Terraform. It does not write Ansible. It does not touch a filesystem,
run a command, or produce implementation syntax of any kind. Generated code is never sent
back to it — a test in ``tests/unit/test_layering.py`` enforces both directions of that
boundary.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

from src.domain.architecture.models import NetworkArchitecture
from src.domain.cloud.models import CloudArchitecture

__all__ = ["RAGProvider", "RAGTranslationRequest", "RAGTranslationResult", "RetrievalHit"]


@dataclass(frozen=True, slots=True)
class RetrievalHit:
    """One knowledge-base chunk the retriever returned."""

    source: str
    heading: str
    text: str
    score: float
    vector_score: float | None = None
    rerank_score: float | None = None


@dataclass(frozen=True, slots=True)
class RAGTranslationRequest:
    """Everything the translator is allowed to see."""

    network_architecture: NetworkArchitecture
    #: Target cloud, e.g. "aws". Kept explicit rather than assumed.
    provider: str = "aws"
    region: str = "eu-west-1"
    #: Operator preferences that are not part of the network architecture itself,
    #: e.g. {"allow_auto_addressing": True}.
    options: dict[str, Any] = field(default_factory=dict)
    correlation_id: str | None = None


@dataclass(frozen=True, slots=True)
class RAGTranslationResult:
    """The translation, plus enough context to explain it.

    ``retrieval`` and ``diagnostics`` exist so the UI can show *why* a decision was made.
    Raw model reasoning is deliberately not carried here.
    """

    cloud_architecture: CloudArchitecture
    retrieval: tuple[RetrievalHit, ...] = ()
    #: Structured, non-sensitive diagnostics: the advisory plan, guard findings, timings.
    diagnostics: dict[str, Any] = field(default_factory=dict)
    #: Network concepts that could not be represented in the cloud model.
    unmapped: tuple[str, ...] = ()
    duration_ms: float | None = None


class RAGProvider(ABC):
    """Translates a validated network architecture into a cloud architecture."""

    @property
    @abstractmethod
    def provider_name(self) -> str: ...

    @abstractmethod
    async def translate(self, request: RAGTranslationRequest) -> RAGTranslationResult:
        """Produce a cloud architecture. Must not write files or emit IaC syntax."""

    @abstractmethod
    async def health_check(self) -> dict[str, Any]:
        """Report readiness: knowledge base present, index built, credentials configured."""
