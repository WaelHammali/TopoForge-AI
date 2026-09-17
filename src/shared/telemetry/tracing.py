"""OpenTelemetry-shaped tracing facade.

The application always codes against this facade. When ``opentelemetry-sdk`` is installed
and telemetry is enabled, spans are delegated to it; otherwise the no-op implementation
costs a dictionary allocation and nothing else. No layer above infrastructure imports
OpenTelemetry directly.
"""

from __future__ import annotations

import functools
import time
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from typing import Any, TypeVar

from src.shared.logging import get_logger
from src.shared.logging.context import current_context

__all__ = ["Span", "Tracer", "get_tracer", "traced"]

_logger = get_logger("topoforge.telemetry")
F = TypeVar("F", bound=Callable[..., Any])


class Span:
    """A single timed unit of work."""

    __slots__ = ("name", "attributes", "_start", "_delegate", "duration_ms", "status")

    def __init__(self, name: str, attributes: Mapping[str, Any] | None = None) -> None:
        self.name = name
        self.attributes: dict[str, Any] = dict(attributes or {})
        self._start = time.perf_counter()
        self._delegate: Any = None
        self.duration_ms: float | None = None
        self.status: str = "ok"

    def set_attribute(self, key: str, value: Any) -> None:
        self.attributes[key] = value
        if self._delegate is not None:  # pragma: no cover - requires the SDK
            self._delegate.set_attribute(key, value)

    def record_exception(self, exc: BaseException) -> None:
        self.status = "error"
        self.attributes["error.type"] = type(exc).__name__
        self.attributes["error.message"] = str(exc)
        if self._delegate is not None:  # pragma: no cover
            self._delegate.record_exception(exc)

    def finish(self) -> float:
        self.duration_ms = (time.perf_counter() - self._start) * 1000.0
        return self.duration_ms


class Tracer:
    """Creates spans and logs their duration as structured events."""

    def __init__(self, service_name: str = "topoforge", delegate: Any = None) -> None:
        self.service_name = service_name
        self._delegate = delegate

    @contextmanager
    def span(self, name: str, **attributes: Any) -> Iterator[Span]:
        span = Span(name, attributes)
        delegate_cm = None
        if self._delegate is not None:  # pragma: no cover - requires the SDK
            delegate_cm = self._delegate.start_as_current_span(name)
            span._delegate = delegate_cm.__enter__()
            for key, value in span.attributes.items():
                span._delegate.set_attribute(key, value)
        try:
            yield span
        except BaseException as exc:
            span.record_exception(exc)
            raise
        finally:
            duration = span.finish()
            if delegate_cm is not None:  # pragma: no cover
                delegate_cm.__exit__(None, None, None)
            _logger.info(
                "span.finished",
                extra={
                    "span": name,
                    "duration_ms": round(duration, 3),
                    "span_status": span.status,
                    **current_context().as_dict(),
                    **{f"attr.{k}": v for k, v in span.attributes.items()},
                },
            )


_tracer: Tracer | None = None


def get_tracer(service_name: str = "topoforge") -> Tracer:
    global _tracer
    if _tracer is None:
        _tracer = Tracer(service_name)
    return _tracer


def traced(name: str | None = None, **attributes: Any) -> Callable[[F], F]:
    """Decorator that wraps a sync or async callable in a span."""

    def decorator(func: F) -> F:
        span_name = name or f"{func.__module__}.{func.__qualname__}"

        if _is_coroutine(func):

            @functools.wraps(func)
            async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
                with get_tracer().span(span_name, **attributes):
                    return await func(*args, **kwargs)

            return async_wrapper  # type: ignore[return-value]

        @functools.wraps(func)
        def sync_wrapper(*args: Any, **kwargs: Any) -> Any:
            with get_tracer().span(span_name, **attributes):
                return func(*args, **kwargs)

        return sync_wrapper  # type: ignore[return-value]

    return decorator


def _is_coroutine(func: Callable[..., Any]) -> bool:
    import inspect

    return inspect.iscoroutinefunction(func)
