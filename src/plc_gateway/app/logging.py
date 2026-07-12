"""Logging configuration for the bootstrap runtime."""

from __future__ import annotations

import json
import logging
from typing import Any


class JsonFormatter(logging.Formatter):
    """Format log records as compact JSON objects."""

    def format(self, record: logging.LogRecord) -> str:
        """Format a log record."""
        payload: dict[str, Any] = {
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "timestamp": self.formatTime(record, self.datefmt),
        }

        if record.exc_info is not None:
            payload["exception"] = self.formatException(record.exc_info)

        return json.dumps(payload, sort_keys=True)


def configure_logging(level_name: str = "INFO") -> None:
    """Configure root logging with JSON output."""
    level = logging.getLevelName(level_name.upper())
    if not isinstance(level, int):
        msg = f"Unsupported log level: {level_name}"
        raise ValueError(msg)

    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())

    root_logger = logging.getLogger()
    root_logger.handlers.clear()
    root_logger.setLevel(level)
    root_logger.addHandler(handler)
