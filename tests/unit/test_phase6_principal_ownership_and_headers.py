from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from fastapi.testclient import TestClient
from lab_platform.control_plane import ControlPlaneConfig, ControlPlaneRuntime, create_app
from lab_platform.control_plane import api as control_plane_api
from lab_platform.models import (
    AuthenticationContext,
    Principal,
    PrincipalType,
    Reservation,
)
from lab_platform.persistence import SQLiteDatabase, SQLiteTimedReservationRepository


def _runtime(tmp_path: Path) -> ControlPlaneRuntime:
    return ControlPlaneRuntime(
        ControlPlaneConfig.model_validate(
            {
                "control_plane": {
                    "host": "127.0.0.1",
                    "port": 8443,
                    "public_url": "http://127.0.0.1:8443",
                },
                "database": {"url": f"sqlite:///{tmp_path / 'control-plane.db'}"},
                "artifacts": {"directory": tmp_path / "artifacts"},
                "development": {
                    "enabled": True,
                    "allow_insecure_agent_transport": True,
                },
            }
        )
    )


def test_phase6_reservation_owner_comes_from_authenticated_principal() -> None:
    principal = Principal(
        id=uuid4(),
        type=PrincipalType.USER,
        organisation_id=uuid4(),
        display_name="Alice Operator",
    )

    owner, owner_principal = control_plane_api._reservation_identity(
        AuthenticationContext(principal=principal),
        "attacker-selected-owner",
    )

    assert owner == "Alice Operator"
    assert owner_principal is not None
    assert owner_principal.principal_id == principal.id
    assert owner_principal.principal_type is PrincipalType.USER


def test_reservation_principal_reference_round_trips(tmp_path: Path) -> None:
    async def scenario() -> None:
        database = SQLiteDatabase(tmp_path / "reservation.db")
        database.initialize()
        repository = SQLiteTimedReservationRepository(database)
        principal_id = uuid4()
        reservation = Reservation(
            id=uuid4(),
            bench_id="home-lab/esp32-01",
            owner="CI Runner",
            owner_principal_id=principal_id,
            owner_principal_type="SERVICE_ACCOUNT",
            created_at=datetime.now(UTC),
        )

        await repository.create(reservation)
        stored = await repository.get(reservation.id)

        assert stored is not None
        assert stored.owner == "CI Runner"
        assert stored.owner_principal_id == principal_id
        assert stored.owner_principal_type == "SERVICE_ACCOUNT"
        database.close()

    asyncio.run(scenario())


def test_security_headers_cover_success_and_authentication_errors(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    with TestClient(create_app(runtime), base_url="https://lab.example.test") as client:
        health = client.get("/api/v1/health")
        unauthenticated = client.get("/api/v1/auth/me")

    for response in (health, unauthenticated):
        assert response.headers["x-content-type-options"] == "nosniff"
        assert response.headers["x-frame-options"] == "DENY"
        assert response.headers["referrer-policy"] == "no-referrer"
        assert response.headers["content-security-policy"].startswith("default-src 'none'")
        assert response.headers["strict-transport-security"].startswith("max-age=31536000")
    assert unauthenticated.status_code == 401
    assert unauthenticated.headers["cache-control"] == "no-store"
