"""Pipeline stage timing.

Every stage named in the build specification (preprocessing, YOLO, OCR, edge extraction,
topology, validation, LLM, RAG, deployment validation/plan/execution) records a duration
through here. Durations are held in a bounded in-process registry for the ``/metrics``
endpoint and emitted as structured log events for scrapers.
"""

from __future__ import annotations

import threading
import time
from collections import defaultdict, deque
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

from src.shared.logging import get_logger
from src.shared.logging.context import current_context

__all__ = ["StageTimer", "record_stage_duration", "stage_timer", "stage_statistics", "reset_metrics"]

_logger = get_logger("topoforge.metrics")
_MAX_SAMPLES = 512
_lock = threading.Lock()
_samples: dict[str, deque[float]] = defaultdict(lambda: deque(maxlen=_MAX_SAMPLES))
_counters: dict[str, int] = defaultdict(int)


@dataclass(slots=True)
class StageTimer:
    stage: str
    started_at: float
    duration_ms: float | None = None
    attributes: dict[str, Any] | None = None

    def set(self, key: str, value: Any) -> None:
        if self.attributes is None:
            self.attributes = {}
        self.attributes[key] = value


def record_stage_duration(stage: str, duration_ms: float, **attributes: Any) -> None:
    with _lock:
        _samples[stage].append(duration_ms)
        _counters[stage] += 1
    _logger.info(
        "stage.duration",
        extra={
            "metric": "stage_duration_ms",
            "stage": stage,
            "duration_ms": round(duration_ms, 3),
            **current_context().as_dict(),
            **attributes,
        },
    )


@contextmanager
def stage_timer(stage: str, **attributes: Any) -> Iterator[StageTimer]:
    timer = StageTimer(stage=stage, started_at=time.perf_counter(), attributes=dict(attributes))
    try:
        yield timer
    finally:
        timer.duration_ms = (time.perf_counter() - timer.started_at) * 1000.0
        record_stage_duration(stage, timer.duration_ms, **(timer.attributes or {}))


def stage_statistics() -> dict[str, dict[str, float]]:
    """Count / mean / p95 / max per stage, computed from the retained samples."""
    with _lock:
        snapshot = {stage: list(values) for stage, values in _samples.items()}
        counts = dict(_counters)

    stats: dict[str, dict[str, float]] = {}
    for stage, values in snapshot.items():
        if not values:
            continue
        ordered = sorted(values)
        index = min(len(ordered) - 1, int(round(0.95 * (len(ordered) - 1))))
        stats[stage] = {
            "count": float(counts.get(stage, len(ordered))),
            "mean_ms": round(sum(ordered) / len(ordered), 3),
            "p95_ms": round(ordered[index], 3),
            "max_ms": round(ordered[-1], 3),
        }
    return stats


def reset_metrics() -> None:
    with _lock:
        _samples.clear()
        _counters.clear()
