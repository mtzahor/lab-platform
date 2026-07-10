from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from typing import Any

_HANDLER_NAME = "lab-platform-structured-console"


class StructuredFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created, UTC).isoformat(),
            "component": record.name,
            "level": record.levelname,
            "message": record.getMessage(),
        }
        if record.exc_info is not None:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, sort_keys=True)


def get_logger(name: str, level: str | int = "INFO") -> logging.Logger:
    logger = logging.getLogger(name)
    logger.setLevel(level)
    logger.propagate = False

    if not any(handler.get_name() == _HANDLER_NAME for handler in logger.handlers):
        console_handler = logging.StreamHandler()
        console_handler.set_name(_HANDLER_NAME)
        console_handler.setFormatter(StructuredFormatter())
        logger.addHandler(console_handler)

    for existing_handler in logger.handlers:
        if existing_handler.get_name() == _HANDLER_NAME:
            existing_handler.setLevel(level)

    return logger
