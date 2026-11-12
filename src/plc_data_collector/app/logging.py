"""Logging configuration for the bootstrap runtime."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

# Attributes every LogRecord has; anything else on a record came from ``extra``.
_STANDARD_RECORD_ATTRIBUTES = frozenset(
    vars(logging.LogRecord("", logging.INFO, "", 0, "", None, None))
) | {"message", "asctime", "taskName", "color_message"}


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

        for key, value in vars(record).items():
            if key not in _STANDARD_RECORD_ATTRIBUTES and key not in payload:
                payload[key] = value

        if record.exc_info is not None:
            payload["exception"] = self.formatException(record.exc_info)

        return json.dumps(payload, sort_keys=True, default=str)


def configure_logging(
    level_name: str = "INFO",
    *,
    log_file: str | Path | None = None,
) -> None:
    """Configure root logging with JSON output."""
    level = logging.getLevelName(level_name.upper())
    if not isinstance(level, int):
        msg = f"Unsupported log level: {level_name}"
        raise ValueError(msg)

    handlers: list[logging.Handler] = [logging.StreamHandler()]
    if log_file is not None:
        log_path = Path(log_file)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.FileHandler(log_path, encoding="utf-8"))

    for handler in handlers:
        handler.setFormatter(JsonFormatter())

    root_logger = logging.getLogger()
    root_logger.handlers.clear()
    root_logger.setLevel(level)
    for handler in handlers:
        root_logger.addHandler(handler)
