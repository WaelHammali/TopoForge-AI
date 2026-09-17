"""LLM provider port.

Used by prompt mode only. The RAG has its own provider inside it, and the generators use
no model at all.

Structured output is mandatory: :meth:`LLMProvider.generate_structured` returns a validated
pydantic instance, never a dictionary and never raw text. An unvalidatable response is an
error, not something to be patched up downstream.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, TypeVar

from pydantic import BaseModel

__all__ = ["ChatMessage", "LLMProvider", "LLMResponse", "LLMUsage", "MessageRole"]

ModelT = TypeVar("ModelT", bound=BaseModel)


class MessageRole(str, Enum):
    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"


@dataclass(frozen=True, slots=True)
class ChatMessage:
    role: MessageRole
    content: str


@dataclass(frozen=True, slots=True)
class LLMUsage:
    input_tokens: int = 0
    output_tokens: int = 0

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens


@dataclass(frozen=True, slots=True)
class LLMResponse:
    text: str
    model: str
    usage: LLMUsage = field(default_factory=LLMUsage)
    finish_reason: str | None = None
    latency_ms: float | None = None
    raw: dict[str, Any] = field(default_factory=dict)


class LLMProvider(ABC):
    """A text model. Vendor-neutral by construction."""

    @property
    @abstractmethod
    def provider_name(self) -> str: ...

    @property
    @abstractmethod
    def model(self) -> str: ...

    @abstractmethod
    async def complete(
        self,
        messages: list[ChatMessage],
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> LLMResponse: ...

    @abstractmethod
    async def generate_structured(
        self,
        messages: list[ChatMessage],
        schema: type[ModelT],
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> ModelT:
        """Return an instance of ``schema``.

        Implementations must validate before returning and raise
        :class:`~src.shared.errors.LLMResponseValidationError` if the model's output does
        not conform. Unvalidated model output never enters the domain.
        """
