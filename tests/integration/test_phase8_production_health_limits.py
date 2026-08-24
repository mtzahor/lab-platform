from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from lab_platform.control_plane.api import create_app
from lab_platform.control_plane.config import ControlPlaneConfig
from lab_platform.control_plane.runtime import ControlPlaneRuntime
from lab_platform.persistence.database_management import (
    MINIMUM_SUPPORTED_SCHEMA_VERSION,
    SchemaCompatibilityError,
    SchemaStatus,
)
from lab_platform.persistence.migrations import SCHEMA_VERSION


def _development_config(tmp_path: Path) -> ControlPlaneConfig:
    return ControlPlaneConfig.model_validate(
        {
            "control_plane": {
                "host": "127.0.0.1",
                "public_url": "http://127.0.0.1:8443",
            },
            "database": {"url": f"sqlite:///{tmp_path / 'control-plane.db'}"},
            "artifacts": {"directory": tmp_path / "artifacts"},
            "security": {
                "api_rate_limits": {"agent_enrollment": {"requests": 1, "window_seconds": 60}}
            },
            "resource_limits": {
                "maximum_sse_streams": 1,
                "maximum_artifact_size_mb": 2,
            },
            "development": {"allow_insecure_agent_transport": True},
        }
    )


def test_liveness_readiness_and_public_edge_rate_limit(tmp_path: Path) -> None:
    runtime = ControlPlaneRuntime(_development_config(tmp_path))
    with TestClient(create_app(runtime)) as client:
        live = client.get("/health/live")
        ready = client.get("/health/ready")
        assert live.status_code == 200
        assert live.json()["status"] == "live"
        assert ready.status_code == 200
        assert ready.json()["checks"]["database"] == {
            "ready": True,
            "schema_version": SCHEMA_VERSION,
            "target_schema_version": SCHEMA_VERSION,
        }
        body = {
            "enrollment_token": "invalid-token",
            "agent_version": "0.9.0-beta",
            "protocol_version": "1.0",
        }
        first = client.post("/api/v1/agents/enroll", json=body)
        second = client.post("/api/v1/agents/enroll", json=body)
        assert first.status_code != 429
        assert first.headers["X-RateLimit-Remaining"] == "0"
        assert second.status_code == 429
        assert second.json()["error"]["code"] == "API_RATE_LIMIT_EXCEEDED"
        assert second.headers["Retry-After"] == "60"


def test_readiness_fails_closed_without_breaking_liveness(tmp_path: Path) -> None:
    runtime = ControlPlaneRuntime(_development_config(tmp_path))

    class UnavailableStorage:
        async def exists(self, _key: str) -> bool:
            raise OSError("storage unavailable")

    with TestClient(create_app(runtime)) as client:
        runtime.artifact_storage = UnavailableStorage()  # type: ignore[assignment]
        assert client.get("/health/live").status_code == 200
        response = client.get("/health/ready")
        assert response.status_code == 503
        assert response.json()["checks"]["artifact_storage"] == {
            "ready": False,
            "error": "OSError",
        }


def test_sse_capacity_is_bounded_and_recoverable(tmp_path: Path) -> None:
    async def scenario() -> None:
        runtime = ControlPlaneRuntime(_development_config(tmp_path))
        try:
            assert await runtime.acquire_sse_stream() is True
            assert await runtime.acquire_sse_stream() is False
            await runtime.release_sse_stream()
            assert await runtime.acquire_sse_stream() is True
            await runtime.release_sse_stream()
            assert runtime.maximum_artifact_size_bytes == 2 * 1024 * 1024
        finally:
            runtime.database.close()

    asyncio.run(scenario())


def test_production_startup_rejects_an_unmigrated_schema(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = ControlPlaneConfig.model_validate(
        {
            "profile": "production",
            "control_plane": {
                "host": "127.0.0.1",
                "public_url": "https://lab.example.com",
            },
            "database": {"url": "postgresql://lab:secret@database/lab"},
            "artifacts": {"directory": tmp_path / "artifacts"},
            "web": {"public_url": "https://lab.example.com"},
            "proxy": {"enabled": True, "trusted_networks": ["127.0.0.1/32"]},
            "development": {
                "enabled": False,
                "allow_insecure_agent_transport": False,
                "allow_tls_termination_proxy": False,
            },
        }
    )
    runtime = ControlPlaneRuntime(config)
    status = SchemaStatus(
        backend="postgresql",
        current_version=SCHEMA_VERSION - 1,
        target_version=SCHEMA_VERSION,
        minimum_supported_version=MINIMUM_SUPPORTED_SCHEMA_VERSION,
        applied_versions=tuple(range(2, SCHEMA_VERSION)),
        state="upgrade_required",
    )
    monkeypatch.setattr(
        "lab_platform.control_plane.runtime.inspect_database_schema",
        lambda _url: status,
    )

    with pytest.raises(SchemaCompatibilityError, match="must be migrated"):
        asyncio.run(runtime.start())
    assert runtime.started is False
