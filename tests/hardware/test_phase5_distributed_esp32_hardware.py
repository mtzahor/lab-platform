from __future__ import annotations

import asyncio
import hashlib
import os
import socket
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast
from uuid import UUID, uuid4

import pytest
import yaml
from httpx2 import AsyncClient
from lab_platform.agent import LabAgent, create_agent
from lab_platform.config import PlatformConfig, load_config
from lab_platform.models import ApiTokenScope

JsonObject = dict[str, object]

_ENABLE_ENV = "LAB_PLATFORM_ENABLE_DISTRIBUTED_HARDWARE_TESTS"
_BENCH_ENV = "LAB_PLATFORM_DISTRIBUTED_HARDWARE_BENCH"
_CONFIG_ENV = "LAB_PLATFORM_DISTRIBUTED_HARDWARE_CONFIG"
_FIRMWARE_ENV = "LAB_PLATFORM_DISTRIBUTED_HARDWARE_FIRMWARE"
_VERSION_ENV = "LAB_PLATFORM_DISTRIBUTED_HARDWARE_FIRMWARE_VERSION"
_TIMEOUT_ENV = "LAB_PLATFORM_DISTRIBUTED_HARDWARE_TIMEOUT_SECONDS"
_CREDENTIAL_ENV = "LAB_PLATFORM_DISTRIBUTED_HARDWARE_AGENT_CREDENTIAL"
_TERMINAL_OPERATION_STATUSES = frozenset({"succeeded", "failed", "cancelled"})


@dataclass(frozen=True, slots=True)
class _HardwareSettings:
    config: PlatformConfig
    local_bench_id: str
    firmware_path: Path
    firmware: bytes
    firmware_version: str
    timeout_seconds: float


def _hardware_settings() -> _HardwareSettings:
    if os.environ.get(_ENABLE_ENV) != "1":
        pytest.skip(f"set {_ENABLE_ENV}=1 to enable the distributed physical ESP32 smoke test")
    local_bench_id = os.environ.get(_BENCH_ENV)
    if not local_bench_id:
        pytest.skip(f"set {_BENCH_ENV} to the one allowed physical bench")
    firmware_name = os.environ.get(_FIRMWARE_ENV)
    if not firmware_name:
        pytest.skip(f"set {_FIRMWARE_ENV} to a reviewed raw ESP32 binary")
    firmware_version = os.environ.get(_VERSION_ENV)
    if not firmware_version:
        pytest.skip(f"set {_VERSION_ENV} to the version marker built into that binary")

    config_path = Path(os.environ.get(_CONFIG_ENV, "examples/esp32-local.yaml"))
    config = load_config(config_path)
    real_backends = [backend for backend in config.effective_backends if backend.type == "real"]
    if len(config.effective_backends) != 1 or len(real_backends) != 1:
        pytest.fail("the distributed hardware smoke config must contain exactly one real backend")
    configured = [bench.id for bench in real_backends[0].config.benches]
    if configured != [local_bench_id]:
        pytest.fail(f"{_BENCH_ENV}={local_bench_id!r} does not exactly match {configured!r}")
    if real_backends[0].config.benches[0].labels.get("board") != "esp32":
        pytest.fail("the one physical smoke bench must have the label board=esp32")

    firmware_path = Path(firmware_name)
    if not firmware_path.is_file():
        pytest.fail(f"reviewed firmware does not exist: {firmware_path}")
    firmware = firmware_path.read_bytes()
    if not firmware:
        pytest.fail("reviewed firmware must not be empty")
    try:
        timeout_seconds = float(os.environ.get(_TIMEOUT_ENV, "300"))
    except ValueError:
        pytest.fail(f"{_TIMEOUT_ENV} must be a number")
    if not 60 <= timeout_seconds <= 3600:
        pytest.fail(f"{_TIMEOUT_ENV} must be between 60 and 3600 seconds")
    return _HardwareSettings(
        config=config,
        local_bench_id=local_bench_id,
        firmware_path=firmware_path,
        firmware=firmware,
        firmware_version=firmware_version,
        timeout_seconds=timeout_seconds,
    )


@pytest.mark.hardware
def test_distributed_esp32_control_plane_workflow_and_restart_history(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Exercise one destructive ESP32 run through the complete Phase 5 transport."""

    settings = _hardware_settings()
    asyncio.run(_distributed_scenario(settings, tmp_path, monkeypatch))


async def _distributed_scenario(
    settings: _HardwareSettings,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    port = _unused_loopback_port()
    base_url = f"http://127.0.0.1:{port}"
    control_plane_config = _write_control_plane_config(
        settings,
        tmp_path,
        port=port,
        base_url=base_url,
    )
    control_plane_log_path = tmp_path / "control-plane.log"
    control_plane_log = control_plane_log_path.open("wb")
    control_plane = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "lab_platform.control_plane.cli",
            "--config",
            str(control_plane_config),
        ],
        cwd=Path(__file__).resolve().parents[2],
        stdout=control_plane_log,
        stderr=subprocess.STDOUT,
    )
    agent: LabAgent | None = None
    try:
        async with AsyncClient(base_url=base_url, timeout=settings.timeout_seconds) as client:
            await _wait_for_health(client, control_plane, control_plane_log_path)
            headers = await _bootstrap_admin(client)
            enrollment = await _enroll_agent(client, headers)
            raw_agent = cast(JsonObject, enrollment["agent"])
            agent_id = UUID(cast(str, raw_agent["id"]))
            global_bench_id = f"{raw_agent['slug']}/{settings.local_bench_id}"
            credential = cast(str, enrollment["credential"])
            monkeypatch.setenv(_CREDENTIAL_ENV, credential)

            agent_config_path = _write_distributed_agent_config(
                settings,
                tmp_path,
                gateway_url=cast(str, enrollment["gateway_url"]),
                agent_id=agent_id,
            )
            agent = create_agent(agent_config_path)
            await agent.start()

            initial_agent = await _eventually(
                client,
                f"/api/v1/agents/{agent_id}",
                headers,
                lambda body: (
                    body.get("status") == "ONLINE"
                    and len(cast(list[object], body.get("benches", []))) == 1
                ),
                timeout_seconds=30,
                description="Agent enrollment, connection, and inventory",
            )
            initial_connection = cast(JsonObject, initial_agent["connection"])
            initial_boot_id = initial_connection["boot_id"]
            initial_benches = cast(list[JsonObject], initial_agent["benches"])
            assert [bench["id"] for bench in initial_benches] == [global_bench_id]
            assert initial_benches[0]["kind"] == "PHYSICAL"

            discovered = await _request_json(
                client,
                "GET",
                "/api/v1/benches",
                200,
                headers=headers,
                params={
                    "kind": "PHYSICAL",
                    "online": "true",
                    "label": "board=esp32",
                },
            )
            discovered_items = cast(list[JsonObject], discovered["items"])
            assert [bench["id"] for bench in discovered_items] == [global_bench_id]

            # Exercise the public reservation contract independently before the workflow
            # takes its own lifecycle-managed reservation.
            preflight = await _request_json(
                client,
                "POST",
                "/api/v1/reservations",
                201,
                headers=headers,
                json={
                    "bench_id": global_bench_id,
                    "owner": "phase5-distributed-hardware-smoke",
                    "idempotency_key": "phase5-hardware-preflight-reservation",
                    "reservation_duration_seconds": 300,
                    "lease_ttl_seconds": 120,
                    "metadata": {"purpose": "physical-distributed-smoke"},
                },
            )
            assert preflight["state"] == "ACTIVE"
            preflight_reservation = cast(JsonObject, preflight["reservation"])
            preflight_lease = cast(JsonObject, preflight["lease"])
            preflight_id = cast(str, preflight_reservation["id"])
            released = await _request_json(
                client,
                "POST",
                f"/api/v1/reservations/{preflight_id}/release",
                200,
                headers=headers,
                json={
                    "owner": "phase5-distributed-hardware-smoke",
                    "expected_lease_version": preflight_lease["lease_version"],
                    "idempotency_key": "phase5-hardware-preflight-release",
                },
            )
            assert released["state"] == "RELEASED"

            workflow = _workflow_definition()
            await _request_json(
                client,
                "POST",
                "/api/v1/workflows",
                201,
                headers=headers,
                json=workflow,
            )
            firmware_sha256 = hashlib.sha256(settings.firmware).hexdigest()
            firmware_record = await _request_json(
                client,
                "POST",
                "/api/v1/artifacts",
                201,
                headers=headers,
                data={
                    "owner_type": "operation",
                    "owner_id": str(uuid4()),
                    "artifact_type": "firmware",
                    "expected_sha256": firmware_sha256,
                    "idempotency_key": "phase5-distributed-hardware-firmware",
                },
                files={
                    "file": (
                        settings.firmware_path.name,
                        settings.firmware,
                        "application/octet-stream",
                    )
                },
            )
            assert firmware_record["sha256"] == firmware_sha256

            workflow_run = await _request_json(
                client,
                "POST",
                f"/api/v1/workflows/{workflow['name']}/runs",
                202,
                headers=headers,
                json={
                    "version": workflow["version"],
                    "owner": "phase5-distributed-hardware-smoke",
                    "idempotency_key": "phase5-distributed-hardware-workflow",
                    "inputs": {
                        "firmware": {"artifact_id": firmware_record["id"]},
                        "expected_version": settings.firmware_version,
                    },
                    "bench_id": global_bench_id,
                    "kind": "PHYSICAL",
                    "reservation_duration_seconds": min(
                        86_400,
                        int(settings.timeout_seconds) + 120,
                    ),
                    "lease_ttl_seconds": min(
                        3_600,
                        int(settings.timeout_seconds) + 120,
                    ),
                    "command_timeout_seconds": int(settings.timeout_seconds),
                },
            )
            assert cast(JsonObject, workflow_run["bench"])["id"] == global_bench_id
            assert cast(JsonObject, workflow_run["reservation"])["state"] == "ACTIVE"
            command_id = cast(str, cast(JsonObject, workflow_run["command"])["id"])
            operation_id = cast(str, cast(JsonObject, workflow_run["operation"])["id"])
            workflow_reservation = cast(JsonObject, workflow_run["reservation"])
            workflow_reservation_id = cast(
                str,
                cast(JsonObject, workflow_reservation["reservation"])["id"],
            )

            operation = await _eventually(
                client,
                f"/api/v1/operations/{operation_id}",
                headers,
                lambda body: str(body.get("status", "")).casefold() in _TERMINAL_OPERATION_STATUSES,
                timeout_seconds=settings.timeout_seconds,
                description="distributed ESP32 workflow operation",
            )
            assert operation["status"] == "SUCCEEDED", (
                operation.get("error_code"),
                operation.get("error_message"),
            )
            await _eventually(
                client,
                f"/api/v1/reservations/{workflow_reservation_id}",
                headers,
                lambda body: body.get("state") == "RELEASED",
                timeout_seconds=30,
                description="automatic distributed workflow reservation release",
            )

            remote_artifacts = await _eventually(
                client,
                "/api/v1/artifacts",
                headers,
                lambda body: any(
                    artifact.get("artifact_type") == "serial_log"
                    and artifact.get("uploaded_at") is not None
                    for artifact in cast(list[JsonObject], body.get("items", []))
                ),
                timeout_seconds=30,
                description="Agent-to-control-plane serial artifact synchronization",
                params={"command_id": command_id},
            )
            artifact_items = cast(list[JsonObject], remote_artifacts["items"])
            downloaded_before_restart = await _download_remote_logs(
                client,
                headers,
                artifact_items,
            )
            combined_logs = b"\n".join(downloaded_before_restart.values()).decode(
                "utf-8",
                errors="replace",
            )
            assert "READY" in combined_logs
            assert "SELF_TEST=PASS" in combined_logs
            assert f"FIRMWARE_VERSION={settings.firmware_version}" in combined_logs

            # Reconstruct the Agent around the same SQLite state. This produces a new
            # boot identity and forces gateway reconciliation without issuing another
            # hardware command.
            await agent.shutdown()
            agent = None
            await _eventually(
                client,
                f"/api/v1/agents/{agent_id}",
                headers,
                lambda body: body.get("status") == "OFFLINE",
                timeout_seconds=30,
                description="Agent disconnect",
            )
            agent = create_agent(agent_config_path)
            await agent.start()
            reconnected_agent = await _eventually(
                client,
                f"/api/v1/agents/{agent_id}",
                headers,
                lambda body: (
                    body.get("status") == "ONLINE"
                    and len(cast(list[object], body.get("benches", []))) == 1
                ),
                timeout_seconds=30,
                description="Agent restart and reconciliation",
            )
            reconnected_connection = cast(JsonObject, reconnected_agent["connection"])
            assert reconnected_connection["boot_id"] != initial_boot_id
            assert [
                bench["id"] for bench in cast(list[JsonObject], reconnected_agent["benches"])
            ] == [global_bench_id]

            history = await _request_json(
                client,
                "GET",
                f"/api/v1/agents/{agent_id}/timeline",
                200,
                headers=headers,
                params={"limit": 500},
            )
            event_types = [item["event_type"] for item in cast(list[JsonObject], history["items"])]
            assert event_types.count("AGENT_CONNECTED") >= 2
            assert "AGENT_DISCONNECTED" in event_types

            operation_after_restart = await _request_json(
                client,
                "GET",
                f"/api/v1/operations/{operation_id}",
                200,
                headers=headers,
            )
            assert operation_after_restart["status"] == operation["status"] == "SUCCEEDED"
            assert operation_after_restart.get("result") == operation.get("result")
            listed_operations = await _request_json(
                client,
                "GET",
                "/api/v1/operations",
                200,
                headers=headers,
                params={"bench_id": global_bench_id},
            )
            matching_operations = [
                item
                for item in cast(list[JsonObject], listed_operations["items"])
                if item["id"] == operation_id
            ]
            assert len(matching_operations) == 1

            assert agent.distributed_runtime is not None
            journal = await agent.distributed_runtime.journal_entries(limit=10_000)
            matching_journal = [entry for entry in journal if str(entry.command_id) == command_id]
            assert len(matching_journal) == 1
            assert matching_journal[0].status.value == "SUCCEEDED"

            artifacts_after_restart = await _request_json(
                client,
                "GET",
                "/api/v1/artifacts",
                200,
                headers=headers,
                params={"command_id": command_id},
            )
            downloaded_after_restart = await _download_remote_logs(
                client,
                headers,
                cast(list[JsonObject], artifacts_after_restart["items"]),
            )
            assert downloaded_after_restart == downloaded_before_restart

            reservations = await _request_json(
                client,
                "GET",
                "/api/v1/reservations",
                200,
                headers=headers,
                params={
                    "bench_id": global_bench_id,
                    "owner": "phase5-distributed-hardware-smoke",
                },
            )
            reservation_items = cast(list[JsonObject], reservations["items"])
            assert len(reservation_items) == 2
            assert {item["state"] for item in reservation_items} == {"RELEASED"}
    finally:
        try:
            if agent is not None:
                await agent.shutdown()
        finally:
            try:
                await asyncio.to_thread(_stop_control_plane, control_plane)
            finally:
                control_plane_log.close()


async def _bootstrap_admin(client: AsyncClient) -> dict[str, str]:
    issued = await _request_json(
        client,
        "POST",
        "/api/v1/tokens",
        201,
        json={
            "name": "distributed-hardware-smoke-admin",
            "owner": "phase5-distributed-hardware-smoke",
            "scopes": [scope.value for scope in ApiTokenScope],
        },
    )
    return {"Authorization": f"Bearer {issued['token']}"}


async def _enroll_agent(client: AsyncClient, headers: dict[str, str]) -> JsonObject:
    enrollment_token = await _request_json(
        client,
        "POST",
        "/api/v1/agents/enrollment-tokens",
        201,
        headers=headers,
        json={
            "name": "distributed-physical-esp32",
            "expires_in_seconds": 300,
            "allowed_labels": {"environment": "physical-smoke"},
        },
    )
    return await _request_json(
        client,
        "POST",
        "/api/v1/agents/enroll",
        201,
        json={
            "enrollment_token": enrollment_token["token"],
            "agent_version": "0.6.0-alpha",
            "protocol_version": "1.0",
            "location": "physical-smoke",
        },
    )


def _write_distributed_agent_config(
    settings: _HardwareSettings,
    root: Path,
    *,
    gateway_url: str,
    agent_id: UUID,
) -> Path:
    payload = settings.config.model_dump(mode="json")
    agent = cast(dict[str, object], payload["agent"])
    agent.update(
        {
            "name": "distributed-physical-esp32",
            "data_directory": str(root / "agent-data"),
            "log_level": "WARNING",
        }
    )
    payload["control_plane"] = {
        "enabled": True,
        "url": gateway_url,
        "heartbeat_interval_seconds": 1,
        "maximum_clock_skew_seconds": 30,
        "maximum_message_size_mb": 2,
        "outgoing_queue_size": 256,
        "event_batch_size": 500,
        "event_ack_timeout_seconds": 30,
        "allow_insecure_loopback": True,
        "reconnect": {
            "initial_delay_seconds": 0.1,
            "maximum_delay_seconds": 1.0,
            "jitter": False,
            "stability_seconds": 1.0,
        },
    }
    payload["identity"] = {
        "agent_id": str(agent_id),
        "credential_env_var": _CREDENTIAL_ENV,
        "certificate_path": None,
        "private_key_path": None,
        "ca_path": None,
    }
    payload["database"] = {"url": f"sqlite:///{root / 'agent.db'}"}
    artifacts = cast(dict[str, object], payload["artifacts"])
    artifacts["directory"] = str(root / "agent-artifacts")
    workflow_directory = root / "agent-workflows"
    workflow_directory.mkdir()
    payload["workflows"] = {
        "definitions_directory": str(workflow_directory),
        "definition_paths": [],
    }
    path = root / "distributed-hardware-agent.yaml"
    path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")
    return path


def _write_control_plane_config(
    settings: _HardwareSettings,
    root: Path,
    *,
    port: int,
    base_url: str,
) -> Path:
    payload = {
        "control_plane": {
            "host": "127.0.0.1",
            "port": port,
            "public_url": base_url,
            "log_level": "WARNING",
        },
        "database": {"url": f"sqlite:///{root / 'control-plane.db'}"},
        "agent_gateway": {
            "heartbeat_interval_seconds": 1,
            "heartbeat_timeout_seconds": 5,
            "offline_timeout_seconds": 10,
            "monitor_interval_seconds": 0.1,
        },
        "artifacts": {
            "directory": str(root / "control-plane-artifacts"),
            "max_upload_size_mb": max(
                10,
                settings.config.artifacts.max_upload_size_mb,
            ),
        },
        "development": {"allow_insecure_agent_transport": True},
    }
    path = root / "distributed-hardware-control-plane.yaml"
    path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")
    return path


def _workflow_definition() -> JsonObject:
    path = Path(__file__).resolve().parents[2] / "examples/workflows/esp32-ci-test.yaml"
    payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise AssertionError(f"workflow definition is not a mapping: {path}")
    return cast(JsonObject, payload)


async def _download_remote_logs(
    client: AsyncClient,
    headers: dict[str, str],
    artifacts: list[JsonObject],
) -> dict[str, bytes]:
    logs: dict[str, bytes] = {}
    for artifact in artifacts:
        if artifact.get("artifact_type") not in {"flash_log", "serial_log"}:
            continue
        artifact_id = cast(str, artifact["id"])
        response = await client.get(
            f"/api/v1/artifacts/{artifact_id}/content",
            headers=headers,
        )
        assert response.status_code == 200, response.text
        assert hashlib.sha256(response.content).hexdigest() == artifact["sha256"]
        logs[artifact_id] = response.content
    assert logs, "the remote workflow did not synchronize any serial-bearing artifact"
    return logs


async def _wait_for_health(
    client: AsyncClient,
    control_plane: subprocess.Popen[bytes],
    log_path: Path,
) -> None:
    deadline = time.monotonic() + 20
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        if control_plane.poll() is not None:
            pytest.fail(
                "control-plane test process exited before becoming healthy; log tail:\n"
                f"{_log_tail(log_path)}"
            )
        try:
            response = await client.get("/api/v1/health")
            if response.status_code == 200 and response.json().get("status") == "healthy":
                return
        except Exception as exc:  # The loopback listener may not be accepting yet.
            last_error = exc
        await asyncio.sleep(0.1)
    pytest.fail(
        "control-plane test server did not become healthy: "
        f"{last_error}; log tail:\n{_log_tail(log_path)}"
    )


async def _eventually(
    client: AsyncClient,
    path: str,
    headers: dict[str, str],
    predicate: Callable[[JsonObject], bool],
    *,
    timeout_seconds: float,
    description: str,
    params: dict[str, str | int | float | bool | None] | None = None,
) -> JsonObject:
    deadline = time.monotonic() + timeout_seconds
    last: JsonObject | None = None
    while time.monotonic() < deadline:
        response = await client.get(path, headers=headers, params=params)
        assert response.status_code == 200, response.text
        last = cast(JsonObject, response.json())
        if predicate(last):
            return last
        await asyncio.sleep(0.25)
    pytest.fail(f"timed out waiting for {description}; last response: {last}")


async def _request_json(
    client: AsyncClient,
    method: str,
    path: str,
    expected_status: int,
    **kwargs: Any,
) -> JsonObject:
    response = await client.request(method, path, **kwargs)
    assert response.status_code == expected_status, response.text
    payload = response.json()
    assert isinstance(payload, dict)
    return cast(JsonObject, payload)


def _unused_loopback_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return cast(int, listener.getsockname()[1])


def _stop_control_plane(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


def _log_tail(path: Path, maximum_characters: int = 4000) -> str:
    try:
        content = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return f"<could not read log: {exc}>"
    return content[-maximum_characters:]
