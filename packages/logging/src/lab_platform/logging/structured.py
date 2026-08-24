from __future__ import annotations

import json
import logging
import re
from datetime import UTC, datetime
from typing import Any

_HANDLER_NAME = "lab-platform-structured-console"
_CONTEXT_FIELDS = (
    "event_type",
    "event_payload",
    "request_id",
    "method",
    "path",
    "status_code",
    "duration_ms",
    "operation_id",
    "workflow_id",
    "workflow_run_id",
    "agent_id",
    "ci_session_id",
    "organisation_id",
    "correlation_id",
    "bench_id",
    "operation_type",
    "owner",
    "status",
    "backend_type",
    "capability",
    "action",
    "result",
)
_SENSITIVE_FIELD_NAMES = frozenset(
    {
        "access_token",
        "authorization",
        "client_secret",
        "credential",
        "database_url",
        "password",
        "private_key",
        "refresh_token",
        "secret",
        "token",
    }
)
_SENSITIVE_FIELD_COMPONENTS = frozenset(
    {"authorization", "credential", "password", "secret", "token"}
)
_BEARER_PATTERN = re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._~+/=-]{12,}")
_LAB_TOKEN_PATTERN = re.compile(r"\b(lp(?:a|c|e|s|t)?_)[A-Za-z0-9_-]{16,}\b")
_POSTGRES_PASSWORD_PATTERN = re.compile(r"(?i)(postgresql(?:\+psycopg)?://[^\s:/@]+:)[^\s@/]+(@)")
_ASSIGNMENT_SECRET_PATTERN = re.compile(r"(?i)\b(password|secret|token|credential)=([^\s,;]+)")


class StructuredFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created, UTC).isoformat(),
            "component": record.name,
            "level": record.levelname,
            "message": redact_log_text(record.getMessage()),
        }
        if record.exc_info is not None:
            payload["exception"] = redact_log_text(self.formatException(record.exc_info))
        for field in _CONTEXT_FIELDS:
            if hasattr(record, field):
                payload[field] = _redact_value(getattr(record, field), field_name=field)
        return json.dumps(payload, sort_keys=True)


class HumanFormatter(logging.Formatter):
    """Compact development formatter with the same secret redaction as JSON logs."""

    def format(self, record: logging.LogRecord) -> str:
        message = redact_log_text(record.getMessage())
        rendered = f"{record.levelname:<8} {record.name}: {message}"
        if record.exc_info is not None:
            rendered += "\n" + redact_log_text(self.formatException(record.exc_info))
        return rendered


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


def configure_logging(
    *,
    level: str | int = "INFO",
    json_output: bool = True,
) -> None:
    """Configure the process root once for production or development output."""

    root = logging.getLogger()
    root.setLevel(level)
    handler = next(
        (item for item in root.handlers if item.get_name() == _HANDLER_NAME),
        None,
    )
    if handler is None:
        handler = logging.StreamHandler()
        handler.set_name(_HANDLER_NAME)
        root.addHandler(handler)
    handler.setLevel(level)
    handler.setFormatter(StructuredFormatter() if json_output else HumanFormatter())


def redact_log_text(value: str) -> str:
    """Remove credential-like values without obscuring useful operational context."""

    redacted = _BEARER_PATTERN.sub(r"\1[REDACTED]", value)
    redacted = _LAB_TOKEN_PATTERN.sub(r"\1[REDACTED]", redacted)
    redacted = _POSTGRES_PASSWORD_PATTERN.sub(r"\1[REDACTED]\2", redacted)
    return _ASSIGNMENT_SECRET_PATTERN.sub(r"\1=[REDACTED]", redacted)


def _redact_value(value: Any, *, field_name: str | None = None) -> Any:
    if field_name is not None and _sensitive_field_name(field_name):
        return "[REDACTED]"
    if isinstance(value, str):
        return redact_log_text(value)
    if isinstance(value, dict):
        return {str(key): _redact_value(item, field_name=str(key)) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_redact_value(item) for item in value]
    return value


def _sensitive_field_name(value: str) -> bool:
    normalized = value.casefold().replace("-", "_")
    return normalized in _SENSITIVE_FIELD_NAMES or bool(
        _SENSITIVE_FIELD_COMPONENTS.intersection(normalized.split("_"))
    )


__all__ = [
    "HumanFormatter",
    "StructuredFormatter",
    "configure_logging",
    "get_logger",
    "redact_log_text",
]
