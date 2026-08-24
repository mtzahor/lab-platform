from __future__ import annotations

import json
import logging

from lab_platform.logging import HumanFormatter, StructuredFormatter, redact_log_text


def test_production_log_format_redacts_platform_tokens_bearers_and_dsn_passwords() -> None:
    token = "lpa_abcdefghijklmnopqrstuvwxyz0123456789"
    message = (
        f"Authorization: Bearer {token}; database="
        "postgresql://lab:database-secret@postgres/lab_platform password=plain-secret"
    )
    record = logging.LogRecord("control-plane", logging.INFO, __file__, 1, message, (), None)
    record.agent_id = "agent-1"

    payload = json.loads(StructuredFormatter().format(record))

    assert payload["agent_id"] == "agent-1"
    assert token not in payload["message"]
    assert "database-secret" not in payload["message"]
    assert "plain-secret" not in payload["message"]
    assert payload["message"].count("[REDACTED]") >= 3
    assert token not in HumanFormatter().format(record)


def test_redaction_does_not_hide_non_secret_operational_identifiers() -> None:
    value = "agent=lab-agent-01 operation=123e4567-e89b-12d3-a456-426614174000"

    assert redact_log_text(value) == value


def test_structured_context_redacts_nested_compound_secret_fields() -> None:
    record = logging.LogRecord("control-plane", logging.INFO, __file__, 1, "event", (), None)
    record.event_payload = {
        "database_password": "hidden-password",
        "agent_token_value": "hidden-token",
        "nested": {"client-secret": "hidden-secret", "agent_id": "agent-1"},
    }

    payload = json.loads(StructuredFormatter().format(record))

    assert payload["event_payload"] == {
        "database_password": "[REDACTED]",
        "agent_token_value": "[REDACTED]",
        "nested": {"client-secret": "[REDACTED]", "agent_id": "agent-1"},
    }
