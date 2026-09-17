"""The RAG adapter: validated network architecture in, cloud architecture out.

This is the only place in the system that talks to the external RAG, and it stops exactly
where the contract says: it returns a :class:`~src.domain.cloud.models.CloudArchitecture`
and never a file, a template or a line of HCL or YAML.

The call is run on a worker thread because the RAG is synchronous and CPU/network bound
(embedding models, a cross-encoder, and one Groq request); blocking the event loop with it
would stall every other request in the process.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any

from src.application.ports.rag import (
    RAGProvider,
    RAGTranslationRequest,
    RAGTranslationResult,
    RetrievalHit,
)
from src.infrastructure.rag.cloud_assembler import CloudArchitectureAssembler
from src.infrastructure.rag.loader import LoadedRAG, load_rag
from src.infrastructure.rag.network_mapper import NetworkArchitectureMapper
from src.shared.errors import RAGError, RAGUnavailableError, ValidationError
from src.shared.logging import get_logger
from src.shared.telemetry import stage_timer

__all__ = ["Net2TFRAGProvider"]

_logger = get_logger("topoforge.rag.provider")


class Net2TFRAGProvider(RAGProvider):
    """Wraps the external net2tf RAG's ``plan_architecture`` API."""

    def __init__(
        self,
        *,
        rag_path: Path,
        kb_dir: Path | None = None,
        index_dir: Path | None = None,
        retrieval_backend: str = "hybrid",
        top_k: int = 8,
        timeout_seconds: float = 900.0,
    ):
        self._rag_path = rag_path
        self._kb_dir = kb_dir
        self._index_dir = index_dir
        self._retrieval_backend = retrieval_backend
        self._top_k = top_k
        self._timeout_seconds = timeout_seconds
        self._loaded: LoadedRAG | None = None

    @property
    def provider_name(self) -> str:
        return "net2tf"

    # ------------------------------------------------------------------ #
    async def translate(self, request: RAGTranslationRequest) -> RAGTranslationResult:
        architecture = request.network_architecture

        # The gate. This is the same condition the workflow checks before routing here;
        # it is re-checked because the RAG performs no validation of its own, so this
        # adapter is the last place an invalid architecture can be stopped.
        if not architecture.can_proceed_to_rag:
            raise ValidationError(
                "refusing to send an unvalidated network architecture to the RAG",
                issues=list(architecture.validation.errors),
                blocking_reasons=list(architecture.blocking_reasons),
            )

        loaded = await asyncio.to_thread(
            load_rag, self._rag_path, kb_dir=self._kb_dir, index_dir=self._index_dir
        )
        self._loaded = loaded

        mapper = NetworkArchitectureMapper(provider=request.provider, region=request.region)
        mapped = mapper.map(
            architecture,
            translation_mode=str(request.options.get("translation_mode", "behavioral_lab")),
            options={
                key: value
                for key, value in request.options.items()
                if key != "translation_mode"
            },
        )

        started = time.perf_counter()
        with stage_timer(
            "rag_translation",
            components=mapped.component_count,
            backend=self._retrieval_backend,
        ):
            plan = await self._invoke(loaded, mapped.payload)
        duration_ms = (time.perf_counter() - started) * 1000.0

        producers = {
            **loaded.describe(),
            "adapter": f"{self.provider_name}@1.0.0",
        }
        cloud_architecture = CloudArchitectureAssembler(
            provider=request.provider, region=request.region
        ).assemble(
            plan,
            architecture,
            producers=producers,
            extra_unmapped=mapped.unmapped,
        )

        _logger.info(
            "rag.translated",
            extra={
                "architecture_id": architecture.architecture_id,
                "revision": architecture.revision,
                "duration_ms": round(duration_ms, 1),
                **cloud_architecture.summary(),
            },
        )

        return RAGTranslationResult(
            cloud_architecture=cloud_architecture,
            retrieval=self._retrieval_hits(plan),
            diagnostics={
                "cloud_plan": plan.get("cloud_plan", {}),
                "ansible_plan": plan.get("ansible_plan", {}),
                "rule_ids": plan.get("rule_ids", []),
                "limitations": plan.get("limitations", []),
                "producers": producers,
            },
            unmapped=cloud_architecture.unmapped,
            duration_ms=duration_ms,
        )

    async def _invoke(self, loaded: LoadedRAG, payload: dict[str, Any]) -> dict[str, Any]:
        """Run the synchronous RAG off the event loop, with a timeout."""

        def call() -> Any:
            retriever = loaded.knowledge_retriever(
                backend=self._retrieval_backend, top_k=self._top_k
            )
            return loaded.plan_architecture(payload, retriever=retriever)

        try:
            plan = await asyncio.wait_for(
                asyncio.to_thread(call), timeout=self._timeout_seconds
            )
        except TimeoutError as error:
            raise RAGError(
                f"the RAG did not return within {self._timeout_seconds:.0f}s",
                timeout_seconds=self._timeout_seconds,
            ) from error
        except RAGUnavailableError:
            raise
        except Exception as error:  # noqa: BLE001 - deliberate boundary translation
            # Everything the external project can raise — transport failure, invalid JSON,
            # truncation — becomes one application error rather than leaking upward.
            raise RAGError(
                f"the RAG failed to produce a plan: {error}", error_type=type(error).__name__
            ) from error

        if not isinstance(plan, dict):
            raise RAGError(
                f"the RAG returned {type(plan).__name__}, not a JSON object",
            )
        return plan

    @staticmethod
    def _retrieval_hits(plan: dict[str, Any]) -> tuple[RetrievalHit, ...]:
        entries = plan.get("knowledge")
        hits: list[RetrievalHit] = []
        for entry in entries if isinstance(entries, list) else []:
            if not isinstance(entry, dict):
                continue
            hits.append(
                RetrievalHit(
                    source=str(entry.get("source", "")),
                    heading=str(entry.get("heading", "")),
                    # `plan` returns citations only; full text needs the `context` command.
                    text=str(entry.get("text", "")),
                    score=float(entry.get("score", 0.0) or 0.0),
                )
            )
        return tuple(hits)

    # ------------------------------------------------------------------ #
    async def health_check(self) -> dict[str, Any]:
        try:
            loaded = await asyncio.to_thread(
                load_rag, self._rag_path, kb_dir=self._kb_dir, index_dir=self._index_dir
            )
        except RAGUnavailableError as error:
            return {"healthy": False, "reason": error.message, **error.details}

        import os

        knowledge_files = len(list(loaded.kb_dir.rglob("*.md")))
        return {
            "healthy": knowledge_files > 0,
            "knowledge_documents": knowledge_files,
            "retrieval_backend": self._retrieval_backend,
            # Presence only. The value is never read into a response.
            "groq_api_key_configured": bool(os.environ.get("GROQ_API_KEY")),
            **loaded.describe(),
        }
