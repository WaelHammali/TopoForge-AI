"""Architectural fitness tests.

These enforce the dependency rule by walking the AST of every module. They are the reason
the layering in docs/ARCHITECTURE.md stays true as the codebase grows: a violation fails
the build rather than being noticed in review, or not.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SRC = PROJECT_ROOT / "src"

#: Vendor and framework packages the inner layers must never import.
INFRASTRUCTURE_PACKAGES = frozenset(
    {
        "fastapi", "starlette", "uvicorn", "sqlalchemy", "alembic", "asyncpg", "psycopg",
        "boto3", "botocore", "ultralytics", "cv2", "torch", "torchvision", "paddleocr",
        "paddle", "pytesseract", "openai", "anthropic", "groq", "langgraph", "langchain",
        "faiss", "sentence_transformers", "transformers", "jinja2", "redis", "httpx",
        "requests", "aiohttp", "sse_starlette",
    }
)

#: Packages a generator must never import: generation is deterministic, full stop.
AI_PACKAGES = frozenset(
    {
        "openai", "anthropic", "groq", "langchain", "langgraph", "transformers",
        "sentence_transformers", "faiss", "torch",
    }
)


def _modules_under(*relative: str) -> list[Path]:
    paths: list[Path] = []
    for part in relative:
        root = SRC / part
        if root.exists():
            paths.extend(sorted(root.rglob("*.py")))
    return paths


def _imported_roots(path: Path) -> set[str]:
    """Every top-level module name imported by ``path``."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            roots.add(node.module.split(".")[0])
    return roots


def _internal_imports(path: Path) -> set[str]:
    """Dotted ``src.*`` module paths imported by ``path``."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    imports: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.update(a.name for a in node.names if a.name.startswith("src."))
        elif isinstance(node, ast.ImportFrom) and node.module and node.module.startswith("src."):
            imports.add(node.module)
    return imports


class TestDomainPurity:
    """The domain knows networking. It knows nothing about how anything is delivered."""

    @pytest.mark.parametrize("path", _modules_under("domain"), ids=lambda p: p.name)
    def test_domain_imports_no_infrastructure(self, path: Path) -> None:
        offenders = _imported_roots(path) & INFRASTRUCTURE_PACKAGES
        assert not offenders, f"{path.relative_to(PROJECT_ROOT)} imports {sorted(offenders)}"

    @pytest.mark.parametrize("path", _modules_under("domain"), ids=lambda p: p.name)
    def test_domain_does_not_depend_on_outer_layers(self, path: Path) -> None:
        offenders = {
            module
            for module in _internal_imports(path)
            if module.startswith(("src.application", "src.infrastructure", "apps.", "workers."))
        }
        assert not offenders, f"{path.relative_to(PROJECT_ROOT)} imports {sorted(offenders)}"


class TestApplicationPurity:
    """The application orchestrates through ports; it never binds a concrete adapter."""

    @pytest.mark.parametrize("path", _modules_under("application"), ids=lambda p: p.name)
    def test_application_imports_no_vendor_sdk(self, path: Path) -> None:
        offenders = _imported_roots(path) & INFRASTRUCTURE_PACKAGES
        assert not offenders, f"{path.relative_to(PROJECT_ROOT)} imports {sorted(offenders)}"

    @pytest.mark.parametrize("path", _modules_under("application"), ids=lambda p: p.name)
    def test_application_does_not_import_infrastructure(self, path: Path) -> None:
        offenders = {
            module
            for module in _internal_imports(path)
            if module.startswith("src.infrastructure")
        }
        assert not offenders, f"{path.relative_to(PROJECT_ROOT)} imports {sorted(offenders)}"


class TestGeneratorsAreDeterministic:
    """The refactor's central rule, enforced mechanically.

    A code generator that could reach an LLM or the RAG would reintroduce exactly the
    coupling this architecture removed.
    """

    @pytest.mark.parametrize(
        "path", _modules_under("infrastructure/generators"), ids=lambda p: p.name
    )
    def test_generators_never_import_an_llm(self, path: Path) -> None:
        offenders = _imported_roots(path) & AI_PACKAGES
        assert not offenders, (
            f"{path.relative_to(PROJECT_ROOT)} imports {sorted(offenders)}; "
            "generators must be deterministic"
        )

    @pytest.mark.parametrize(
        "path", _modules_under("infrastructure/generators"), ids=lambda p: p.name
    )
    def test_generators_never_import_the_rag(self, path: Path) -> None:
        offenders = {
            module for module in _internal_imports(path) if "rag" in module.split(".")
        }
        assert not offenders, (
            f"{path.relative_to(PROJECT_ROOT)} imports {sorted(offenders)}; "
            "generated code must never flow back through the RAG"
        )


class TestRAGBoundary:
    """The RAG produces cloud architecture, never implementation syntax."""

    @pytest.mark.parametrize("path", _modules_under("infrastructure/rag"), ids=lambda p: p.name)
    def test_rag_adapter_does_not_import_a_generator(self, path: Path) -> None:
        offenders = {
            module
            for module in _internal_imports(path)
            if module.startswith("src.infrastructure.generators")
        }
        assert not offenders, (
            f"{path.relative_to(PROJECT_ROOT)} imports {sorted(offenders)}; "
            "the RAG must stop at cloud_architecture.json"
        )
