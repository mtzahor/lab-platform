from __future__ import annotations

from datetime import UTC, datetime, timedelta
from functools import partial
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from lab_platform.control_plane import ControlPlaneConfig, ControlPlaneRuntime, create_app
from lab_platform.control_plane import api as control_plane_api
from lab_platform.control_plane import cli as control_plane_cli
from lab_platform.control_plane.rate_limits import InMemoryRateLimiter, RateLimitPolicy
from lab_platform.control_plane.server import ControlPlaneHttpServer
from lab_platform.control_plane_core.reservations import (
    CoordinatedReservationLease,
    ReservationLeaseState,
)
from lab_platform.core.errors import (
    CapabilityNotSupportedError,
    ReservationNotActiveError,
    ReservationOwnerMismatchError,
)
from lab_platform.models import (
    AgentRecord,
    ApiTokenScope,
    DistributedOperation,
    GlobalBenchKind,
    GlobalBenchRecord,
    GlobalBenchStatus,
    HealthStatus,
    RemoteCommand,
    Reservation,
    ReservationLease,
)

from tests.support.control_plane import make_control_plane_runtime as _runtime


def _bootstrap(client: TestClient) -> dict[str, str]:
    response = client.post(
        "/api/v1/tokens",
        json={
            "name": "action-admin",
            "owner": "alice",
            "scopes": [scope.value for scope in ApiTokenScope],
        },
    )
    assert response.status_code == 201
    return {"Authorization": f"Bearer {response.json()['token']}"}


async def _seed_bench(runtime: ControlPlaneRuntime, agent: AgentRecord) -> GlobalBenchRecord:
    now = datetime.now(UTC)
    bench = GlobalBenchRecord(
        id=f"{agent.slug}/bench-01",
        agent_id=agent.id,
        agent_slug=agent.slug,
        local_bench_id="bench-01",
        name="Action bench",
        backend_id="simlab",
        kind=GlobalBenchKind.SIMULATED,
        status=GlobalBenchStatus.ONLINE,
        health=HealthStatus.HEALTHY,
        capabilities=frozenset({"flash", "probe", "reset", "serial"}),
        created_at=now,
        updated_at=now,
        last_seen_at=now,
    )
    await runtime.inventory_repository.reconcile_agent_snapshot(
        agent,
        (bench,),
        observed_at=now,
    )
    return bench


def _reservation(agent_id: UUID, bench_id: str) -> CoordinatedReservationLease:
    now = datetime.now(UTC)
    reservation = Reservation(
        id=uuid4(),
        bench_id=bench_id,
        owner="alice",
        created_at=now,
        starts_at=now,
        ends_at=now + timedelta(hours=1),
        idempotency_key="action-reservation",
    )
    return CoordinatedReservationLease(
        reservation=reservation,
        lease=ReservationLease(
            reservation_id=reservation.id,
            agent_id=agent_id,
            bench_id=bench_id,
            owner="alice",
            valid_from=now,
            valid_until=now + timedelta(hours=1),
            lease_version=1,
        ),
        state=ReservationLeaseState.ACTIVE,
        revision=1,
    )


def test_direct_bench_action_routes_stage_only_durable_artifact_metadata(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _runtime(tmp_path)
    captured: list[dict[str, Any]] = []

    with TestClient(create_app(runtime)) as client:
        headers = _bootstrap(client)
        enrollment_token = client.post(
            "/api/v1/agents/enrollment-tokens",
            headers=headers,
            json={"name": "action-agent", "expires_in_seconds": 300},
        ).json()["token"]
        enrolled = client.post(
            "/api/v1/agents/enroll",
            json={
                "enrollment_token": enrollment_token,
                "agent_version": "0.6.0-alpha",
                "protocol_version": "1.0",
            },
        ).json()
        assert client.portal is not None
        agent = client.portal.call(runtime.presence.get_agent, UUID(enrolled["agent"]["id"]))
        bench = client.portal.call(_seed_bench, runtime, agent)
        reservation = _reservation(agent.id, bench.id)

        async def create_command(**kwargs: Any) -> tuple[RemoteCommand, DistributedOperation]:
            captured.append(kwargs)
            lease = kwargs.get("reservation_lease")
            command = RemoteCommand(
                agent_id=kwargs["agent_id"],
                bench_id=kwargs["bench_id"],
                command_type=kwargs["command_type"],
                payload=dict(kwargs["payload"]),
                expires_at=kwargs["expires_at"],
                idempotency_key=kwargs["idempotency_key"],
                reservation_id=lease.reservation_id if lease is not None else None,
                lease_version=lease.lease_version if lease is not None else None,
            )
            operation = DistributedOperation(
                remote_command_id=command.id,
                agent_id=command.agent_id,
                bench_id=command.bench_id,
                reservation_id=command.reservation_id,
                operation_type=str(kwargs["operation_type"]),
            )
            return command.model_copy(update={"operation_id": operation.id}), operation

        monkeypatch.setattr(runtime.commands, "create", create_command)
        monkeypatch.setattr(
            control_plane_api,
            "_action_reservation",
            AsyncMock(return_value=reservation),
        )

        probe = client.post(
            f"/api/v1/benches/{bench.id}/actions/probe",
            headers={**headers, "Idempotency-Key": "probe-once"},
            json={"owner": "alice"},
        )
        assert probe.status_code == 202
        assert probe.json()["status"] == "CREATED"

        reset = client.post(
            f"/api/v1/benches/{bench.id}/actions/reset",
            headers=headers,
            json={"owner": "alice"},
        )
        assert reset.status_code == 202

        serial = client.post(
            f"/api/v1/benches/{bench.id}/actions/read-serial",
            headers=headers,
            json={
                "owner": "alice",
                "timeout_seconds": 2.5,
                "until_pattern": "READY",
                "max_lines": 12,
            },
        )
        assert serial.status_code == 202
        assert captured[-1]["payload"]["request"] == {
            "timeout_seconds": 2.5,
            "until_pattern": "READY",
            "max_lines": 12,
            "include_timestamps": True,
        }

        flashed = client.post(
            f"/api/v1/benches/{bench.id}/actions/flash",
            headers={**headers, "Idempotency-Key": "flash-once"},
            data={"owner": "alice", "version": "2.0.0"},
            files={"firmware": ("firmware.bin", b"firmware-v2", "application/octet-stream")},
        )
        assert flashed.status_code == 202, flashed.text
        assert flashed.json()["input_artifact"]["sha256"]
        durable = captured[-1]["payload"]["artifact"]
        assert set(durable) == {
            "input_name",
            "agent_id",
            "artifact_id",
            "sha256",
            "size_bytes",
            "target_path",
        }
        assert "transfer_token" not in repr(captured)
        assert "download_url" not in repr(captured)

        missing = client.post(
            "/api/v1/benches/missing/bench/actions/probe",
            headers=headers,
            json={"owner": "alice"},
        )
        assert missing.status_code == 404

        with pytest.raises(CapabilityNotSupportedError):
            client.portal.call(
                partial(
                    control_plane_api._action_bench,
                    runtime,
                    bench.id,
                    capability="power",
                )
            )


def test_direct_action_reservation_helper_fences_absent_wrong_and_inactive_leases(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime: Any = SimpleNamespace(
        reservation_repository=SimpleNamespace(get_current_for_bench=AsyncMock(return_value=None))
    )

    async def scenario() -> None:
        with pytest.raises(ReservationNotActiveError, match="require an active"):
            await control_plane_api._action_reservation(runtime, "agent/bench", "alice")

        active = _reservation(UUID(int=1), "agent/bench")
        runtime.reservation_repository.get_current_for_bench = AsyncMock(return_value=active)
        with pytest.raises(ReservationOwnerMismatchError, match="another owner"):
            await control_plane_api._action_reservation(runtime, "agent/bench", "bob")

        inactive = CoordinatedReservationLease(
            reservation=active.reservation,
            lease=active.lease,
            state=ReservationLeaseState.UNKNOWN,
            revision=2,
            unknown_since=datetime.now(UTC),
            reconciliation_deadline=datetime.now(UTC) + timedelta(minutes=1),
        )
        runtime.reservation_repository.get_current_for_bench = AsyncMock(return_value=inactive)
        with pytest.raises(ReservationNotActiveError, match="lease is not active"):
            await control_plane_api._action_reservation(runtime, "agent/bench", "alice")

    import asyncio

    asyncio.run(scenario())


def _config(tmp_path: Path, *, tls: bool = False) -> ControlPlaneConfig:
    control_plane: dict[str, object] = {
        "host": "127.0.0.1",
        "port": 8443,
        "public_url": "https://127.0.0.1:8443" if tls else "http://127.0.0.1:8443",
    }
    if tls:
        control_plane.update(
            {
                "tls_certificate_path": tmp_path / "server.crt",
                "tls_private_key_path": tmp_path / "server.key",
            }
        )
    return ControlPlaneConfig.model_validate(
        {
            "control_plane": control_plane,
            "database": {"url": f"sqlite:///{tmp_path / 'control-plane.db'}"},
            "artifacts": {"directory": tmp_path / "artifacts"},
            "development": {"allow_insecure_agent_transport": not tls},
        }
    )


def test_control_plane_once_initializes_and_reports_metrics(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config_path = tmp_path / "control-plane.yaml"
    config_path.write_text(
        "control_plane:\n"
        "  host: 127.0.0.1\n"
        "  port: 8443\n"
        "  public_url: http://127.0.0.1:8443\n"
        f"database:\n  url: sqlite:///{tmp_path / 'once.db'}\n"
        f"artifacts:\n  directory: {tmp_path / 'once-artifacts'}\n"
        "development:\n  allow_insecure_agent_transport: true\n",
        encoding="utf-8",
    )

    assert control_plane_cli.main(["--config", str(config_path), "--once"]) == 0
    output = capsys.readouterr().out
    assert "Configuration loaded" in output
    assert "Database migrations applied" in output
    assert "0 Agents online" in output
    assert "0 benches in unified inventory" in output


def test_control_plane_migrate_command_applies_schema_without_starting_services(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config_path = tmp_path / "control-plane.yaml"
    database_path = tmp_path / "migrated.db"
    config_path.write_text(
        "control_plane:\n"
        "  host: 127.0.0.1\n"
        "  port: 8443\n"
        "  public_url: http://127.0.0.1:8443\n"
        f"database:\n  url: sqlite:///{database_path}\n"
        f"artifacts:\n  directory: {tmp_path / 'artifacts'}\n"
        "development:\n  allow_insecure_agent_transport: true\n",
        encoding="utf-8",
    )

    assert control_plane_cli.main(["migrate", "--config", str(config_path)]) == 0
    assert database_path.exists()
    output = capsys.readouterr().out
    assert "Database migrations applied" in output
    assert "Agents online" not in output


def test_control_plane_main_serving_path_handles_interrupt_and_shutdown(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config = _config(tmp_path, tls=True)
    events: list[Any] = []

    class FakeServer:
        def __init__(self, runtime: ControlPlaneRuntime, *, host: str, port: int) -> None:
            events.append((runtime.config, host, port))

        def serve_forever(self) -> None:
            events.append("serve")
            raise KeyboardInterrupt

        def shutdown(self) -> None:
            events.append("shutdown")

    monkeypatch.setattr(control_plane_cli, "load_control_plane_config", lambda _path: config)
    monkeypatch.setattr(control_plane_cli, "ControlPlaneHttpServer", FakeServer)

    assert control_plane_cli.main(["--host", "0.0.0.0", "--port", "9443"]) == 0
    assert events[0][1:] == ("0.0.0.0", 9443)
    assert events[1:] == ["serve", "shutdown"]
    output = capsys.readouterr().out
    assert "Listening on https://127.0.0.1:8443" in output
    assert "Shutting down" in output
    assert control_plane_cli._with_bind_host(config, None) is config


def test_control_plane_http_server_binds_runs_and_closes_socket(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeSocket:
        def __init__(self, port: int) -> None:
            self.port = port
            self.closed = False

        def getsockname(self) -> tuple[str, int]:
            return "127.0.0.1", self.port

        def shutdown(self, _direction: int) -> None:
            return None

        def close(self) -> None:
            self.closed = True

    with pytest.raises(ValueError, match="must match the validated configuration host"):
        ControlPlaneHttpServer(
            ControlPlaneRuntime(_config(tmp_path / "mismatched")),
            host="0.0.0.0",
            port=0,
        )

    sockets = [FakeSocket(18080), FakeSocket(18081)]
    monkeypatch.setattr(
        "lab_platform.control_plane.server.uvicorn.Config.bind_socket",
        lambda _config: sockets.pop(0),
    )
    runtime = ControlPlaneRuntime(_config(tmp_path))
    server = ControlPlaneHttpServer(runtime, host="127.0.0.1", port=0)
    assert server.host == "127.0.0.1"
    assert server.port == 18080
    server.shutdown()

    second_runtime = ControlPlaneRuntime(_config(tmp_path / "second"))
    tls_server = ControlPlaneHttpServer(second_runtime, host="127.0.0.1", port=0)
    observed: dict[str, Any] = {}

    def run(*, sockets: list[object]) -> None:
        observed["sockets"] = sockets

    monkeypatch.setattr(tls_server._server, "run", run)
    tls_server.serve_forever()
    assert observed["sockets"] == [tls_server._socket]
    tls_server.shutdown()


def test_with_bind_host_rejects_invalid_serialized_shape(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    malformed: Any = SimpleNamespace(model_dump=lambda **_kwargs: {"control_plane": "bad"})
    with pytest.raises(TypeError, match="serialize as a mapping"):
        control_plane_cli._with_bind_host(malformed, "127.0.0.1")


def test_sliding_window_rate_limiter_reports_retry_and_recovers() -> None:
    now = [10.0]
    limiter = InMemoryRateLimiter(maximum_keys=2, clock=lambda: now[0])
    policy = RateLimitPolicy(requests=2, window_seconds=10)

    assert limiter.check("workflow-create", "client-a", policy).remaining == 1
    assert limiter.check("workflow-create", "client-a", policy).remaining == 0
    rejected = limiter.check("workflow-create", "client-a", policy)
    assert not rejected.allowed
    assert rejected.retry_after_seconds == 10

    now[0] = 20.0
    recovered = limiter.check("workflow-create", "client-a", policy)
    assert recovered.allowed
    assert recovered.remaining == 1


def test_limiter_bounds_attacker_controlled_client_keys() -> None:
    limiter = InMemoryRateLimiter(maximum_keys=2, clock=lambda: 1.0)
    policy = RateLimitPolicy(requests=1, window_seconds=10)
    for key in ("one", "two", "three"):
        assert limiter.check("login", key, policy).allowed

    assert list(limiter._entries) == [("login", "two"), ("login", "three")]


@pytest.mark.parametrize(
    "policy",
    [
        {"requests": 0, "window_seconds": 1},
        {"requests": 1, "window_seconds": 0},
    ],
)
def test_invalid_rate_limit_policy_is_rejected(policy: dict[str, int]) -> None:
    with pytest.raises(ValueError):
        RateLimitPolicy(**policy)


def test_limiter_requires_positive_key_capacity() -> None:
    with pytest.raises(ValueError, match="maximum rate-limit keys"):
        InMemoryRateLimiter(maximum_keys=0)


@pytest.mark.parametrize(
    ("category", "client_key", "message"),
    [
        ("", "client", "category"),
        (" " * 2, "client", "category"),
        ("x" * 101, "client", "category"),
        ("login", "", "client key"),
        ("login", " " * 2, "client key"),
        ("login", "x" * 501, "client key"),
    ],
)
def test_limiter_rejects_unbounded_or_empty_keys(
    category: str,
    client_key: str,
    message: str,
) -> None:
    limiter = InMemoryRateLimiter()
    with pytest.raises(ValueError, match=message):
        limiter.check(category, client_key, RateLimitPolicy(requests=1, window_seconds=1))


@pytest.mark.parametrize("clock_value", [float("nan"), float("inf"), float("-inf")])
def test_limiter_rejects_non_finite_clock(clock_value: float) -> None:
    limiter = InMemoryRateLimiter(clock=lambda: clock_value)
    with pytest.raises(ValueError, match="clock must be finite"):
        limiter.check("login", "client", RateLimitPolicy(requests=1, window_seconds=1))
