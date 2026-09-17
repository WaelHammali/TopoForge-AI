"""Structured logging.

Emits one JSON object per record with the active correlation identifiers merged in.
``structlog`` is used when available; otherwise an equivalent stdlib formatter is
installed so that log shape is identical in both cases.
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import UTC, datetime
from typing import Any

from src.shared.logging.context import current_context

__all__ = ["configure_logging", "get_logger"]

_RESERVED = frozenset(
    {
        "args", "asctime", "created", "exc_info", "exc_text", "filename", "funcName",
        "levelname", "levelno", "lineno", "module", "msecs", "message", "msg", "name",
        "pathname", "process", "processName", "relativeCreated", "stack_info",
        "thread", "threadName", "taskName",
    }
)

_configured = False


class JSONFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        payload.update(current_context().as_dict())
        payload.update(
            {k: v for k, v in record.__dict__.items() if k not in _RESERVED and not k.startswith("_")}
        )
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


class ConsoleFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        ctx = current_context().as_dict()
        extra = {
            k: v for k, v in record.__dict__.items() if k not in _RESERVED and not k.startswith("_")
        }
        suffix = " ".join(f"{k}={v}" for k, v in {**ctx, **extra}.items())
        base = f"{record.levelname:<8} {record.name} :: {record.getMessage()}"
        line = f"{base} {suffix}".rstrip()
        if record.exc_info:
            line = f"{line}\n{self.formatException(record.exc_info)}"
        return line


def configure_logging(level: str = "INFO", fmt: str = "json", *, force: bool = False) -> None:
    """Install the root handler. Idempotent unless ``force`` is set."""
    global _configured
    if _configured and not force:
        return

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JSONFormatter() if fmt == "json" else ConsoleFormatter())

    root = logging.getLogger()
    for existing in list(root.handlers):
        root.removeHandler(existing)
    root.addHandler(handler)
    root.setLevel(level.upper())

    # Third-party noise floors.
    for noisy in ("httpx", "httpcore", "urllib3", "botocore", "asyncio", "sentence_transformers"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    _configured = True


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
