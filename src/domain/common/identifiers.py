"""Identifier helpers used across the domain."""

from __future__ import annotations

import hashlib
import re
import uuid

__all__ = ["new_id", "slugify", "stable_id"]

_SLUG_STRIP = re.compile(r"[^a-z0-9]+")


def new_id(prefix: str = "") -> str:
    """A fresh opaque identifier, optionally prefixed for readability in logs."""
    value = uuid.uuid4().hex
    return f"{prefix}_{value}" if prefix else value


def slugify(value: str, *, max_length: int = 48) -> str:
    """Lowercase, hyphen-separated slug. Empty input yields ``"unnamed"``."""
    slug = _SLUG_STRIP.sub("-", value.strip().lower()).strip("-")
    return (slug[:max_length].rstrip("-")) or "unnamed"


def stable_id(*parts: object, prefix: str = "") -> str:
    """Deterministic id derived from ``parts``.

    Used wherever an identifier must be reproducible across runs — for example a node id
    derived from a detection's class and position, so re-running extraction on the same
    image yields the same ids and diffs stay meaningful.
    """
    digest = hashlib.sha256("\x1f".join(str(part) for part in parts).encode()).hexdigest()[:16]
    return f"{prefix}_{digest}" if prefix else digest
