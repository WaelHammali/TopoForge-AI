"""Integration tests against a real checkout of the external net2tf RAG.

Skipped entirely when no checkout is found — these are not run by default and are not
part of the unit suite's guarantees. They exist to catch exactly the kind of drift that
happened once already during this build: the upstream project restructured itself, and
nothing short of actually importing and calling it would have noticed.

Location search order: ``TOPOFORGE_RAG_TEST_PATH`` (explicit override), then
``external/rag`` (the vendored copy), then the sibling checkout this repository was
developed against. No network access and no ``GROQ_API_KEY`` are required — these tests
only exercise the parts of the RAG that need neither: import, symbol presence, and lexical
(BM25-only) retrieval.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import pytest

from src.infrastructure.rag.loader import load_rag, reset_cache
from src.infrastructure.rag.network_mapper import NetworkArchitectureMapper
from src.shared.errors import RAGUnavailableError
from tests.fixtures.network_architectures import two_router_ospf

pytestmark = pytest.mark.requires_rag


def _candidate_paths() -> list[Path]:
    candidates: list[Path] = []
    override = os.environ.get("TOPOFORGE_RAG_TEST_PATH")
    if override:
        candidates.append(Path(override))
    project_root = Path(__file__).resolve().parents[3]
    candidates.append(project_root / "external" / "rag")
    candidates.append(
        Path.home() / "Projects" / "Advanced-RAG-For-Net_To_Cloud-Translation"
    )
    return candidates


def _find_rag() -> Path | None:
    for candidate in _candidate_paths():
        if (candidate / "app.py").is_file():
            return candidate
    return None


RAG_PATH = _find_rag()
skip_reason = (
    "no net2tf RAG checkout found (set TOPOFORGE_RAG_TEST_PATH, run "
    "scripts/sync_external_rag.sh, or check out the sibling project)"
)


@pytest.fixture(autouse=True)
def _reset_loader_cache() -> Iterator[None]:
    reset_cache()
    yield
    reset_cache()


@pytest.mark.skipif(RAG_PATH is None, reason=skip_reason)
class TestTheRealRAGIsCompatibleWithThisAdapter:
    """Everything here ran manually during a compatibility audit on 2026-09-18.

    It is captured as a test so the next time the RAG changes shape, this notices instead
    of requiring another manual read-through of its source.
    """

    def test_the_checkout_exposes_every_symbol_the_loader_requires(
        self, tmp_path: Path
    ) -> None:
        loaded = load_rag(RAG_PATH, index_dir=tmp_path / "index")
        assert callable(loaded.plan_architecture)
        assert callable(loaded.knowledge_retriever)
        description = loaded.describe()
        assert description["rag_kb_dir"]

    def test_legacy_is_never_imported(self, tmp_path: Path) -> None:
        # Matched on "legacy" as a whole path segment, not a substring: pytest's own
        # _pytest.legacypath is unrelated and must not make this test a false positive.
        import sys

        load_rag(RAG_PATH, index_dir=tmp_path / "index")
        offenders = [name for name in sys.modules if name.split(".")[0] == "legacy"]
        assert offenders == []

    def test_our_mapped_payload_survives_real_lexical_retrieval(
        self, tmp_path: Path
    ) -> None:
        """The strongest available check without a Groq key: real BM25 over the real KB."""
        loaded = load_rag(RAG_PATH, index_dir=tmp_path / "index")
        payload = NetworkArchitectureMapper().map(two_router_ospf()).payload

        retriever = loaded.knowledge_retriever(backend="lexical", top_k=6)
        knowledge = retriever.retrieve(payload)

        assert knowledge, "the real retriever returned nothing for a valid architecture"
        rule_ids = {entry["rule_id"] for entry in knowledge}
        # The KB's mandatory core rules must always be pinned, regardless of query.
        assert {"CORE-001", "CORE-002", "CORE-003"} <= rule_ids
        for entry in knowledge:
            assert {"rule_id", "source", "heading", "text"} <= set(entry)

    def test_a_malformed_architecture_is_rejected_by_the_real_app_not_by_us(
        self, tmp_path: Path
    ) -> None:
        loaded = load_rag(RAG_PATH, index_dir=tmp_path / "index")
        with pytest.raises(TypeError):
            loaded.plan_architecture(["not", "an", "object"])

    def test_flat_module_names_do_not_leak_after_loading(self, tmp_path: Path) -> None:
        import sys

        load_rag(RAG_PATH, index_dir=tmp_path / "index")
        assert not any(name in sys.modules for name in ("app", "config", "planner", "retriever"))


class TestUnavailableRAGIsReportedNotSilentlySkipped:
    """These run unconditionally: they prove the failure path itself works."""

    def test_a_missing_checkout_raises_a_typed_error(self, tmp_path: Path) -> None:
        with pytest.raises(RAGUnavailableError):
            load_rag(tmp_path / "does-not-exist")

    def test_a_directory_without_app_py_raises(self, tmp_path: Path) -> None:
        (tmp_path / "kb").mkdir()
        with pytest.raises(RAGUnavailableError, match="app.py"):
            load_rag(tmp_path)
