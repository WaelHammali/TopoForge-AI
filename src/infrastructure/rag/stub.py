"""An offline stand-in for the RAG.

This is a **test and development double**, not a second translation pipeline. It
implements the same port, is selected only by explicit configuration
(``TOPOFORGE_RAG__BACKEND=stub``), and exists so that the platform's own behaviour —
the workflow, the generators, the approval gate, the UI — can be exercised end to end
without a Groq key, a knowledge base or a downloaded embedding model.

It contains no intelligence and makes no cloud-architecture decisions of its own beyond
the minimum needed to produce a well-formed plan: every component is represented, nothing
is invented, and its assumptions say plainly that a stub produced them.
"""

from __future__ import annotations

import time
from typing import Any

from src.application.ports.rag import RAGProvider, RAGTranslationRequest, RAGTranslationResult
from src.domain.topology.models import NodeType
from src.infrastructure.rag.cloud_assembler import CloudArchitectureAssembler
from src.infrastructure.rag.network_mapper import NetworkArchitectureMapper
from src.shared.errors import ValidationError

__all__ = ["StubRAGProvider"]

#: Source role -> the representation a plan would name. Mirrors the knowledge base's
#: documented mappings so that the stub's output is structurally realistic.
_REPRESENTATION: dict[NodeType, str] = {
    NodeType.ROUTER: "EC2 instance (router appliance) inside a VPC",
    NodeType.GATEWAY: "EC2 instance (router appliance) inside a VPC",
    NodeType.SWITCH: "VPC subnet",
    NodeType.FIREWALL: "security group",
    NodeType.SERVER: "EC2 instance",
    NodeType.DATABASE: "EC2 instance",
    NodeType.PC: "EC2 instance",
    NodeType.LOAD_BALANCER: "load balancer",
}


class StubRAGProvider(RAGProvider):
    """Deterministic, offline. Same contract, no model."""

    def __init__(self, *, provider: str = "aws", region: str = "eu-west-1"):
        self._provider = provider
        self._region = region

    @property
    def provider_name(self) -> str:
        return "stub"

    async def translate(self, request: RAGTranslationRequest) -> RAGTranslationResult:
        architecture = request.network_architecture
        if not architecture.can_proceed_to_rag:
            raise ValidationError(
                "refusing to translate an unvalidated network architecture",
                issues=list(architecture.validation.errors),
            )

        started = time.perf_counter()
        mapped = NetworkArchitectureMapper(
            provider=request.provider, region=request.region
        ).map(architecture)
        plan = self._plan(architecture)

        cloud_architecture = CloudArchitectureAssembler(
            provider=request.provider, region=request.region
        ).assemble(
            plan,
            architecture,
            producers={"adapter": "stub@1.0.0", "rag_revision": "stub"},
            extra_unmapped=mapped.unmapped,
        )

        return RAGTranslationResult(
            cloud_architecture=cloud_architecture,
            retrieval=(),
            diagnostics={
                "cloud_plan": plan["cloud_plan"],
                "ansible_plan": plan["ansible_plan"],
                "stub": True,
            },
            unmapped=cloud_architecture.unmapped,
            duration_ms=(time.perf_counter() - started) * 1000.0,
        )

    def _plan(self, architecture: Any) -> dict[str, Any]:
        mapping = [
            {
                "component_id": node.id,
                "role": node.type.value,
                "cloud_representation": _REPRESENTATION.get(node.type, "unmapped"),
                "configuration": {},
                "rule_ids": [],
            }
            for node in architecture.nodes
        ]
        targets = [
            {"component_id": node.id, "connection": {}, "variables": {}}
            for node in architecture.nodes
            if node.attributes.get("services")
        ]
        return {
            "cloud_plan": {
                "translation_mode": "behavioral_lab",
                "component_mapping": mapping,
                "networking": {"links": [], "addressing": [], "routing": [], "security": []},
                "dependencies": [],
            },
            "ansible_plan": {"targets": targets, "tasks": [], "dependencies": []},
            "rule_ids": [],
            "limitations": [
                "Produced by the offline stub translator: no knowledge base was consulted "
                "and no cloud reasoning was performed. Configure a real RAG backend before "
                "relying on this translation."
            ],
            "knowledge": [],
        }

    async def health_check(self) -> dict[str, Any]:
        return {"healthy": True, "stub": True, "reason": "offline deterministic translator"}
