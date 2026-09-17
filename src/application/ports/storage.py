"""Object storage and model loading ports."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import BinaryIO

__all__ = ["ModelLoader", "ObjectStorage", "StoredObject"]


@dataclass(frozen=True, slots=True)
class StoredObject:
    uri: str
    key: str
    size_bytes: int
    content_type: str | None = None
    checksum: str | None = None
    created_at: datetime | None = None
    metadata: dict[str, str] = field(default_factory=dict)


class ObjectStorage(ABC):
    """Where uploads and generated artifacts live.

    Large binaries never go into the database and never into workflow state — only URIs
    do.
    """

    @abstractmethod
    async def put(
        self,
        key: str,
        data: bytes | BinaryIO,
        *,
        content_type: str | None = None,
        metadata: dict[str, str] | None = None,
    ) -> StoredObject: ...

    @abstractmethod
    async def get(self, key: str) -> bytes: ...

    @abstractmethod
    async def exists(self, key: str) -> bool: ...

    @abstractmethod
    async def delete(self, key: str) -> None: ...

    @abstractmethod
    async def list(self, prefix: str) -> list[StoredObject]: ...

    @abstractmethod
    async def presigned_url(self, key: str, *, expires_in: int = 900) -> str: ...

    @abstractmethod
    def uri_for(self, key: str) -> str: ...

    @abstractmethod
    async def download_to(self, key: str, destination: Path) -> Path:
        """Materialise an object locally, for tools that need a real file."""


class ModelLoader(ABC):
    """Resolves model weights to a local path.

    Development loads a local file; production pulls from object storage into a cache.
    Either way the application only ever sees a path.
    """

    @abstractmethod
    async def ensure_local(self, model_name: str, version: str | None = None) -> Path: ...

    @abstractmethod
    async def describe(self, model_name: str, version: str | None = None) -> dict[str, str]: ...
