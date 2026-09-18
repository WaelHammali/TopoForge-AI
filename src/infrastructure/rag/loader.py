"""Guarded loading of the external net2tf RAG.

That project uses flat top-level module names (``app``, ``config``, ``planner``,
``retriever``) which would collide with almost any host application, so it is imported
under a lock, with its directory on ``sys.path`` only for the duration of the import, and
the resulting modules are cached.

Nothing in the checkout is modified. Configuration is passed through the ``NET2TF_*``
environment variables the project itself reads.

``legacy/`` is never imported: it is the archived implementation and is explicitly marked
unused by the active API.
"""

from __future__ import annotations

import importlib
import importlib.util
import os
import subprocess
import sys
import threading
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Any

from src.shared.errors import RAGUnavailableError
from src.shared.logging import get_logger

__all__ = ["LoadedRAG", "load_rag"]

_logger = get_logger("topoforge.rag.loader")
_lock = threading.Lock()
_cache: dict[str, LoadedRAG] = {}

#: Module-level symbols the adapter calls. Asserted at load time so drift upstream is a
#: clear error here rather than an AttributeError deep inside a workflow.
_REQUIRED_SYMBOLS: tuple[tuple[str, str], ...] = (
    ("app", "plan_architecture"),
    ("retriever", "KnowledgeRetriever"),
    ("planner", "plan_with_rag"),
)


@dataclass(frozen=True, slots=True)
class LoadedRAG:
    """The imported RAG, plus what it is, for reproducibility."""

    root: Path
    modules: dict[str, ModuleType]
    revision: str | None
    kb_dir: Path
    index_dir: Path

    @property
    def plan_architecture(self) -> Any:
        return self.modules["app"].plan_architecture

    @property
    def knowledge_retriever(self) -> Any:
        return self.modules["retriever"].KnowledgeRetriever

    def describe(self) -> dict[str, str]:
        return {
            "rag_root": str(self.root),
            "rag_revision": self.revision or "unknown",
            "rag_kb_dir": str(self.kb_dir),
            "plan_model": str(getattr(self.modules.get("config"), "PLAN_MODEL", "unknown")),
            "embed_model": str(getattr(self.modules.get("config"), "EMBED_MODEL", "unknown")),
        }


def load_rag(
    root: Path,
    *,
    kb_dir: Path | None = None,
    index_dir: Path | None = None,
) -> LoadedRAG:
    """Import the RAG at ``root``. Cached per resolved path.

    Raises :class:`~src.shared.errors.RAGUnavailableError` when the checkout is missing,
    incomplete, or does not expose the expected API.
    """
    resolved = root.expanduser().resolve()
    cache_key = str(resolved)

    with _lock:
        cached = _cache.get(cache_key)
        if cached is not None:
            return cached

        if not resolved.is_dir():
            raise RAGUnavailableError(
                f"no RAG checkout at {resolved}. Set TOPOFORGE_RAG__PATH, or run "
                "scripts/sync_external_rag.sh to vendor one into external/rag.",
                path=str(resolved),
            )
        if not (resolved / "app.py").is_file():
            raise RAGUnavailableError(
                f"{resolved} does not look like the net2tf RAG: app.py is missing",
                path=str(resolved),
            )

        resolved_kb = (kb_dir or resolved / "kb").expanduser().resolve()
        if not resolved_kb.is_dir():
            raise RAGUnavailableError(
                f"the RAG knowledge base directory {resolved_kb} does not exist",
                path=str(resolved_kb),
            )
        resolved_index = (index_dir or resolved / "index").expanduser().resolve()
        resolved_index.mkdir(parents=True, exist_ok=True)

        # The RAG reads these at import time. Setting them here configures the checkout
        # without editing a single file in it.
        os.environ.setdefault("NET2TF_KB_DIR", str(resolved_kb))
        os.environ["NET2TF_INDEX_DIR"] = str(resolved_index)

        loaded = LoadedRAG(
            root=resolved,
            modules=_import_modules(resolved),
            revision=_git_revision(resolved),
            kb_dir=resolved_kb,
            index_dir=resolved_index,
        )
        _cache[cache_key] = loaded
        _logger.info("rag.loaded", extra=loaded.describe())
        return loaded


def _import_modules(root: Path) -> dict[str, ModuleType]:
    """Import the RAG's flat modules with its directory temporarily on ``sys.path``.

    The path entry is removed immediately afterwards so that later imports elsewhere in
    the process cannot accidentally resolve ``config`` or ``models`` to this checkout.
    """
    entry = str(root)
    # Anything already imported under these names belongs to someone else; shadowing is
    # what the whole guard exists to prevent, so remember and restore.
    shadowed = {name: sys.modules.pop(name, None) for name, _ in _REQUIRED_SYMBOLS}
    shadowed["config"] = sys.modules.pop("config", None)

    sys.path.insert(0, entry)
    try:
        modules: dict[str, ModuleType] = {}
        for name in ("config", "retriever", "planner", "app"):
            try:
                modules[name] = importlib.import_module(name)
            except ImportError as error:
                # A missing heavy dependency (sentence-transformers, or groq for
                # planner.py) is expected in lean environments; the lexical retrieval
                # backend needs neither, and both modules import lazily inside their
                # functions rather than at module level, so retrieval-only use works
                # even when groq is not installed at all.
                if name in {"retriever", "planner"}:
                    _logger.warning(
                        "rag.optional_module_unavailable",
                        extra={"module": name, "error": str(error)},
                    )
                    continue
                raise RAGUnavailableError(
                    f"failed to import the RAG module {name!r} from {root}: {error}",
                    module=name,
                ) from error

        for module_name, symbol in _REQUIRED_SYMBOLS:
            module = modules.get(module_name)
            if module is None or not hasattr(module, symbol):
                raise RAGUnavailableError(
                    f"the RAG at {root} does not expose {module_name}.{symbol}; the "
                    "adapter targets its documented API and will not guess at another",
                    module=module_name,
                    symbol=symbol,
                )
        return modules
    finally:
        # Take our path entry back out, then unregister the RAG's flat module names and
        # restore anything we displaced. The module objects stay alive through our own
        # references and each other's, so the RAG keeps working while the names "app",
        # "config", "planner" and "retriever" mean nothing in this process again.
        with contextlib_suppress(ValueError):
            sys.path.remove(entry)
        for name in ("config", "retriever", "planner", "app"):
            sys.modules.pop(name, None)
        for name, module in shadowed.items():
            if module is not None:
                sys.modules[name] = module


def _git_revision(root: Path) -> str | None:
    """Short revision of the checkout, recorded in ``producers`` for reproducibility."""
    try:
        result = subprocess.run(  # noqa: S603 - fixed argv, no shell, no user input
            ["git", "-C", str(root), "rev-parse", "--short", "HEAD"],  # noqa: S607
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout.strip() or None if result.returncode == 0 else None


class contextlib_suppress:
    """Local, dependency-free ``contextlib.suppress`` equivalent."""

    def __init__(self, *exceptions: type[BaseException]) -> None:
        self._exceptions = exceptions

    def __enter__(self) -> None:
        return None

    def __exit__(self, exc_type: type[BaseException] | None, *_: object) -> bool:
        return exc_type is not None and issubclass(exc_type, self._exceptions)


def reset_cache() -> None:
    """Drop cached imports. Tests use this; production never needs it."""
    with _lock:
        _cache.clear()
