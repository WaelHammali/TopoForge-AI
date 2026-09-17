"""Correlation context propagated through every log record.

Identifiers are stored in a :mod:`contextvars` variable so they survive across ``await``
boundaries and are inherited by tasks spawned inside a scope.
"""

from __future__ import annotations

import contextvars
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass, replace
from typing import Any

__all__ = [
    "CorrelationContext",
    "bind_context",
    "clear_context",
    "correlation_scope",
    "current_context",
]

CONTEXT_FIELDS = (
    "request_id",
    "correlation_id",
    "user_id",
    "project_id",
    "architecture_id",
    "workflow_id",
    "job_id",
    "stage",
)


@dataclass(frozen=True, slots=True)
class CorrelationContext:
    request_id: str | None = None
    correlation_id: str | None = None
    user_id: str | None = None
    project_id: str | None = None
    architecture_id: str | None = None
    workflow_id: str | None = None
    job_id: str | None = None
    stage: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {key: value for key, value in asdict(self).items() if value is not None}


_context: contextvars.ContextVar[CorrelationContext] = contextvars.ContextVar(
    "topoforge_correlation_context", default=CorrelationContext()
)


def current_context() -> CorrelationContext:
    return _context.get()


def bind_context(**values: Any) -> contextvars.Token[CorrelationContext]:
    """Merge ``values`` into the active context. Returns a token for ``reset``."""
    unknown = set(values) - set(CONTEXT_FIELDS)
    if unknown:
        raise ValueError(f"unknown correlation fields: {sorted(unknown)}")
    merged = replace(_context.get(), **{k: v for k, v in values.items() if v is not None})
    return _context.set(merged)


def clear_context() -> None:
    _context.set(CorrelationContext())


@contextmanager
def correlation_scope(**values: Any) -> Iterator[CorrelationContext]:
    """Bind identifiers for the duration of a block, then restore the previous context."""
    token = bind_context(**values)
    try:
        yield _context.get()
    finally:
        _context.reset(token)
