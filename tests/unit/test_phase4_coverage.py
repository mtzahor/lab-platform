from __future__ import annotations

import asyncio
import importlib
import json
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from lab_platform.agent.api.app import create_app
from lab_platform.agent.api.artifacts import (
    _authorize_artifact,
    _public_artifact,
    _require_session_owner,
)
from lab_platform.agent.api.results import _require_workflow_owner
from lab_platform.agent.api.tokens import CreateTokenRequest
from lab_platform.cli.ci_environment import CiEnvironment, CiProvider
from lab_platform.cli.ci_exit_codes import CiExitCode
from lab_platform.cli.client import AgentApiError, AgentClient, AgentConnectionError
from lab_platform.core.errors import PermissionDeniedError
from lab_platform.models import (
    ApiToken,
    ApiTokenScope,
    ArtifactOwnerType,
    ArtifactRecord,
    BenchRequest,
    CiSession,
    CiSessionStatus,
)
from lab_platform.models import (
    CiProvider as ModelCiProvider,
)
from lab_platform.persistence import SQLiteCiSessionRepository, SQLiteDatabase
from pydantic import ValidationError

cli = importlib.import_module("lab_platform.cli.main")
agent_cli = importlib.import_module("lab_platform.agent.cli")
agent_api = importlib.import_module("lab_platform.agent.api.app")


def _ci_environment() -> CiEnvironment:
    return CiEnvironment(
        provider=CiProvider.GITHUB_ACTIONS,
        external_run_id="run-42",
        repository="owner/repository",
        ref="refs/heads/main",
        commit_sha="abc123",
        actor="octocat",
        attempt="2",
    )


def _completed_session(**updates: object) -> dict[str, object]:
    session: dict[str, object] = {
        "id": "session-1",
        "status": "completed",
        "outcome": "succeeded",
        "cleanup_status": "succeeded",
        "bench_id": "bench-01",
        "workflow_run_id": "workflow-1",
    }
    session.update(updates)
    return session


def test_token_cli_create_list_and_revoke_render_table_and_json(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    token = {
        "id": "token-1",
        "name": "hardware-ci",
        "owner": "github-actions",
        "token": "lp_plaintext_once",
        "revoked_at": None,
    }
    revoked = {**token, "token": None, "revoked_at": "2026-07-23T10:00:00Z"}
    posted: list[tuple[str, dict[str, object]]] = []

    def post(
        self: AgentClient,
        path: str,
        payload: dict[str, object],
        **_kwargs: object,
    ) -> object:
        posted.append((path, payload))
        return revoked if path.endswith("/revoke") else token

    def get(
        self: AgentClient,
        path: str,
        query: dict[str, object] | None = None,
        **_kwargs: object,
    ) -> object:
        assert path == "/api/v1/tokens"
        return {"items": [token, revoked]}

    monkeypatch.setattr(AgentClient, "post", post)
    monkeypatch.setattr(AgentClient, "get", get)

    create = [
        "token",
        "create",
        "--name",
        "hardware-ci",
        "--owner",
        "github-actions",
        "--scope",
        "ci:sessions",
        "--scope",
        "artifacts:read",
        "--expires-at",
        "2026-08-01T00:00:00Z",
    ]
    assert cli.main(create) == 0
    assert "Token:    lp_plaintext_once" in capsys.readouterr().out
    assert cli.main([*create, "--output", "json"]) == 0
    assert json.loads(capsys.readouterr().out)["id"] == "token-1"
    assert posted[0] == (
        "/api/v1/tokens",
        {
            "name": "hardware-ci",
            "owner": "github-actions",
            "scopes": ["ci:sessions", "artifacts:read"],
            "expires_at": "2026-08-01T00:00:00Z",
        },
    )

    assert cli.main(["token", "list"]) == 0
    table = capsys.readouterr().out
    assert "hardware-ci" in table
    assert "Active" in table
    assert "Revoked" in table
    assert cli.main(["token", "list", "--output", "json"]) == 0
    assert len(json.loads(capsys.readouterr().out)["items"]) == 2

    assert cli.main(["token", "revoke", "token-1"]) == 0
    assert "Revoked API token token-1" in capsys.readouterr().out
    assert cli.main(["token", "revoke", "token-1", "--output", "json"]) == 0
    assert json.loads(capsys.readouterr().out)["revoked_at"] is not None


def test_artifact_and_result_cli_commands_cover_binary_text_and_file_outputs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    firmware = tmp_path / "firmware.bin"
    firmware.write_bytes(b"firmware")
    artifact = {
        "id": "artifact-1",
        "name": "serial.log",
        "artifact_type": "serial_log",
        "size_bytes": 6,
        "sha256": "a" * 64,
    }
    uploaded: list[dict[str, object]] = []

    def upload_artifact(
        self: AgentClient,
        path: str,
        artifact_path: Path,
        **kwargs: object,
    ) -> object:
        assert path == "/api/v1/artifacts"
        assert artifact_path == firmware
        uploaded.append(kwargs)
        return artifact

    def get(
        self: AgentClient,
        path: str,
        query: dict[str, object] | None = None,
        **_kwargs: object,
    ) -> object:
        if path.endswith("/artifacts"):
            return {"items": [artifact]}
        if path.endswith("/results"):
            return {
                "workflow_run_id": "workflow-1",
                "results": [{"name": "Self test", "status": "passed"}],
            }
        raise AssertionError(path)

    monkeypatch.setattr(AgentClient, "upload_artifact", upload_artifact)
    monkeypatch.setattr(AgentClient, "get", get)
    monkeypatch.setattr(AgentClient, "download", lambda self, path: b"READY\n")
    monkeypatch.setattr(
        AgentClient,
        "get_text",
        lambda self, path: "<?xml version='1.0'?><testsuite tests='1'/>\n",
    )

    upload = [
        "ci",
        "upload",
        "session-1",
        str(firmware),
        "--name",
        "firmware.bin",
        "--artifact-type",
        "firmware",
        "--idempotency-key",
        "upload-1",
    ]
    assert cli.main(upload) == 0
    assert "Artifact uploaded: artifact-1" in capsys.readouterr().out
    assert cli.main([*upload, "--sha256", "a" * 64, "--output", "json"]) == 0
    assert json.loads(capsys.readouterr().out)["id"] == "artifact-1"
    assert uploaded[0]["checksum"] != "a" * 64
    assert uploaded[1]["checksum"] == "a" * 64

    assert cli.main(["ci", "artifacts", "session-1"]) == 0
    assert "serial.log" in capsys.readouterr().out
    assert cli.main(["ci", "artifacts", "session-1", "--output", "json"]) == 0
    assert json.loads(capsys.readouterr().out)["items"][0]["id"] == "artifact-1"

    download = tmp_path / "downloads" / "serial.log"
    assert cli.main(["ci", "download", "artifact-1", "--output", str(download)]) == 0
    assert download.read_bytes() == b"READY\n"
    assert "Downloaded artifact" in capsys.readouterr().out

    assert cli.main(["workflow", "results", "workflow-1", "--format", "junit"]) == 0
    assert "<testsuite" in capsys.readouterr().out
    junit = tmp_path / "reports" / "hardware.xml"
    assert (
        cli.main(
            [
                "workflow",
                "results",
                "workflow-1",
                "--format",
                "junit",
                "--output",
                str(junit),
            ]
        )
        == 0
    )
    assert "<testsuite" in junit.read_text(encoding="utf-8")

    assert cli.main(["workflow", "results", "workflow-1"]) == 0
    assert json.loads(capsys.readouterr().out)["results"][0]["status"] == "passed"
    json_result = tmp_path / "reports" / "hardware.json"
    assert (
        cli.main(
            [
                "workflow",
                "results",
                "workflow-1",
                "--output",
                str(json_result),
            ]
        )
        == 0
    )
    assert json.loads(json_result.read_text(encoding="utf-8"))["workflow_run_id"] == "workflow-1"


def test_ci_session_cli_create_show_watch_cancel_and_finalize(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    requests: list[tuple[str, dict[str, object], str | None]] = []
    watch_replies: Iterator[dict[str, object]] = iter(
        [
            {"id": "session-1", "status": "running", "bench_id": "bench-01"},
            _completed_session(),
        ]
    )
    cancellation_pending = False

    def post(
        self: AgentClient,
        path: str,
        payload: dict[str, object],
        **kwargs: object,
    ) -> object:
        nonlocal cancellation_pending
        idempotency_key = kwargs.get("idempotency_key")
        assert idempotency_key is None or isinstance(idempotency_key, str)
        requests.append((path, payload, idempotency_key))
        if path == "/api/v1/ci/sessions":
            return {"id": "session-1", "status": "reserved", "bench_id": "bench-01"}
        if path.endswith("/heartbeat"):
            return {"id": "session-1", "status": "running"}
        if path.endswith("/cancel"):
            cancellation_pending = True
            return {"id": "session-1", "status": "cancel_requested"}
        if path.endswith("/finalize"):
            return _completed_session(outcome="cancelled" if cancellation_pending else "succeeded")
        raise AssertionError(path)

    def get(
        self: AgentClient,
        path: str,
        query: dict[str, object] | None = None,
        **_kwargs: object,
    ) -> object:
        if cancellation_pending:
            return {"id": "session-1", "status": "cancelled", "outcome": "cancelled"}
        return next(watch_replies, _completed_session())

    monkeypatch.setattr(cli, "detect_ci_environment", _ci_environment)
    monkeypatch.setattr(AgentClient, "post", post)
    monkeypatch.setattr(AgentClient, "get", get)
    monkeypatch.setattr(cli.time, "sleep", lambda _seconds: None)

    create = [
        "ci",
        "session",
        "create",
        "--external-run-id",
        "override-run",
        "--repository",
        "override/repository",
        "--ref",
        "refs/tags/v1",
        "--commit-sha",
        "def456",
        "--actor",
        "robot",
        "--require",
        "capability=serial",
        "--label",
        "board=esp32",
        "--prefer-label",
        "location=simulation",
        "--no-allow-physical",
        "--wait-timeout",
        "2m",
        "--reservation-duration",
        "30m",
        "--idempotency-key",
        "manual-key",
    ]
    assert cli.main(create) == 0
    assert "Session" in capsys.readouterr().out
    created_path, created_payload, created_key = requests[0]
    assert created_path == "/api/v1/ci/sessions"
    assert created_key == "manual-key"
    assert created_payload["external_run_id"] == "override-run"
    assert created_payload["repository"] == "override/repository"
    assert created_payload["bench_request"] == {
        "explicit_bench_id": None,
        "required_capabilities": ["serial"],
        "required_labels": {"board": "esp32"},
        "preferred_labels": {"location": "simulation"},
        "allow_simulated": True,
        "allow_physical": False,
        "maximum_wait_seconds": 120,
        "reservation_duration_seconds": 1800,
    }

    assert cli.main(["ci", "session", "show", "session-1"]) == 0
    assert "Running" in capsys.readouterr().out
    assert cli.main(["ci", "session", "watch", "session-1", "--interval", "0.001"]) == 0
    assert "Completed" in capsys.readouterr().out

    assert (
        cli.main(
            [
                "ci",
                "session",
                "cancel",
                "session-1",
                "--idempotency-key",
                "cancel-key",
                "--output",
                "json",
            ]
        )
        == CiExitCode.WORKFLOW_CANCELLED
    )
    assert json.loads(capsys.readouterr().out)["outcome"] == "cancelled"

    cancellation_pending = False
    assert (
        cli.main(
            [
                "ci",
                "session",
                "finalize",
                "session-1",
                "--idempotency-key",
                "finalize-key",
                "--output",
                "json",
            ]
        )
        == CiExitCode.SUCCESS
    )
    assert json.loads(capsys.readouterr().out)["cleanup_status"] == "succeeded"


@pytest.mark.parametrize(
    ("command", "error", "expected"),
    [
        (
            ["version"],
            AgentApiError(404, "NOT_FOUND", "missing"),
            3,
        ),
        (
            ["version"],
            AgentApiError(403, "FORBIDDEN", "forbidden"),
            5,
        ),
        (
            ["version"],
            AgentApiError(409, "CONFLICT", "conflict"),
            4,
        ),
        (
            ["version"],
            AgentApiError(503, "UNAVAILABLE", "unavailable"),
            6,
        ),
        (
            ["version"],
            AgentConnectionError("offline"),
            6,
        ),
        (
            ["ci", "artifacts", "session-1"],
            AgentApiError(401, "AUTHENTICATION_REQUIRED", "authenticate"),
            CiExitCode.AUTHENTICATION_FAILED,
        ),
        (
            ["ci", "artifacts", "session-1"],
            AgentConnectionError("offline"),
            CiExitCode.CLIENT_OR_PROTOCOL_ERROR,
        ),
    ],
)
def test_cli_maps_api_and_connection_failures(
    command: list[str],
    error: Exception,
    expected: int,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def fail(
        self: AgentClient,
        path: str,
        query: dict[str, object] | None = None,
        **_kwargs: object,
    ) -> object:
        raise error

    monkeypatch.setattr(AgentClient, "get", fail)
    assert cli.main(command) == expected
    assert "error" in capsys.readouterr().err


def test_api_ownership_helpers_cover_each_supported_artifact_owner() -> None:
    now = datetime(2026, 7, 23, 12, tzinfo=UTC)
    workflow_run_id = uuid4()
    session_id = uuid4()
    token = ApiToken(
        name="ci",
        token_hash="a" * 64,
        owner="alice",
        scopes={ApiTokenScope.ARTIFACTS_READ, ApiTokenScope.OPERATIONS_READ},
        created_at=now,
    )

    class WorkflowService:
        async def get_run(self, run_id: object) -> object:
            assert run_id == workflow_run_id
            return SimpleNamespace(owner="alice")

    class CiSessionService:
        async def get(self, requested_id: object, *, synchronize: bool) -> object:
            assert requested_id == session_id
            assert synchronize is False
            return SimpleNamespace(requested_by="alice")

    agent = SimpleNamespace(
        workflow_service=WorkflowService(),
        ci_session_service=CiSessionService(),
    )

    async def scenario() -> None:
        workflow_artifact = ArtifactRecord(
            owner_type=ArtifactOwnerType.WORKFLOW_RUN,
            owner_id=workflow_run_id,
            name="serial.log",
            artifact_type="serial_log",
            path="/private/generated/content",
            size_bytes=5,
            sha256="b" * 64,
            created_at=now,
        )
        typed_agent = cast(Any, agent)
        await _authorize_artifact(typed_agent, workflow_artifact, token)
        public = _public_artifact(workflow_artifact)
        assert public["id"] == str(workflow_artifact.id)
        assert "path" not in public

        session_artifact = workflow_artifact.model_copy(
            update={
                "id": uuid4(),
                "owner_type": ArtifactOwnerType.CI_SESSION,
                "owner_id": session_id,
            }
        )
        await _authorize_artifact(typed_agent, session_artifact, token)

        wrong_owner = token.model_copy(update={"owner": "bob"})
        with pytest.raises(PermissionDeniedError, match="workflow artifact"):
            await _authorize_artifact(typed_agent, workflow_artifact, wrong_owner)

        operation_artifact = workflow_artifact.model_copy(
            update={"id": uuid4(), "owner_type": ArtifactOwnerType.OPERATION}
        )
        with pytest.raises(PermissionDeniedError, match="not available"):
            await _authorize_artifact(typed_agent, operation_artifact, token)

    asyncio.run(scenario())

    _require_session_owner("alice", token)
    _require_workflow_owner("alice", token)
    with pytest.raises(PermissionDeniedError, match="CI session"):
        _require_session_owner("bob", token)
    with pytest.raises(PermissionDeniedError, match="workflow run"):
        _require_workflow_owner("bob", token)

    with pytest.raises(ValidationError, match="timezone"):
        CreateTokenRequest(
            name="invalid-expiry",
            owner="alice",
            scopes={ApiTokenScope.CI_SESSIONS},
            expires_at=datetime(2026, 8, 1),
        )


def test_ci_repository_lookup_filter_and_missing_resource_behavior(tmp_path: Path) -> None:
    async def scenario() -> None:
        database = SQLiteDatabase(tmp_path / "ci-coverage.db")
        database.initialize()
        repository = SQLiteCiSessionRepository(database)
        now = datetime(2026, 7, 23, 12, tzinfo=UTC)
        session = CiSession(
            provider=ModelCiProvider.GITHUB_ACTIONS,
            external_run_id="run-coverage",
            repository="owner/repository",
            requested_by="alice",
            created_at=now,
        )
        missing_id = uuid4()
        try:
            assert await repository.create(session, idempotency_key="create-coverage") == session
            assert await repository.get(missing_id) is None
            assert await repository.get_by_idempotency_key("create-coverage") == session
            assert await repository.get_by_idempotency_key("missing") is None
            assert await repository.get_by_workflow_launch_idempotency_key("missing") is None
            assert await repository.get_by_finalize_idempotency_key("missing") is None

            assert await repository.list(status=CiSessionStatus.CREATED) == [session]
            assert await repository.list(provider=ModelCiProvider.GITHUB_ACTIONS) == [session]
            assert await repository.list(provider=ModelCiProvider.JENKINS) == []
            with pytest.raises(ValueError, match="limit"):
                await repository.list(limit=0)
            with pytest.raises(ValueError, match="limit"):
                await repository.list_stale(heartbeat_before=now, now=now, limit=0)

            assert await repository.errors(missing_id) == []
            assert not await repository.append_error(missing_id, "missing")
            assert await repository.get_cleanup(missing_id) is None
            assert (
                await repository.assign_compatible_bench(
                    missing_id,
                    BenchRequest(required_capabilities={"serial"}),
                    now=now,
                )
                is None
            )

            assert (
                await repository.attach_workflow_run(
                    session.id,
                    uuid4(),
                    idempotency_key="launch-before-reservation",
                    started_at=now,
                )
                is None
            )
            finalized = session.model_copy(update={"status": CiSessionStatus.COMPLETED})
            assert (
                await repository.mark_finalized(
                    finalized.model_copy(update={"id": missing_id}),
                    idempotency_key="finalize-missing",
                )
                is None
            )
        finally:
            database.close()

    asyncio.run(scenario())


def test_agent_cli_serves_with_overrides_and_shuts_down_on_interrupt(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    lifecycle: list[str] = []

    class FakeAgent:
        started = True
        config = SimpleNamespace(
            agent=SimpleNamespace(host="127.0.0.1", port=8080),
            effective_backends=[
                SimpleNamespace(id="virtual", type="simlab"),
                SimpleNamespace(id="physical", type="real"),
            ],
            simlab=SimpleNamespace(enabled=True),
        )

        async def start(self) -> None:
            lifecycle.append("agent-start")

        async def shutdown(self) -> None:
            lifecycle.append("agent-shutdown")

        def benches(self) -> list[object]:
            return [object(), object()]

        def plugins(self) -> list[object]:
            return [object()]

    class FakeServer:
        def __init__(self, *, agent: object, host: str, port: int) -> None:
            assert isinstance(agent, FakeAgent)
            self.host = host
            self.port = port

        def serve_forever(self) -> None:
            lifecycle.append("serve")
            raise KeyboardInterrupt

        def shutdown(self) -> None:
            lifecycle.append("server-shutdown")

    monkeypatch.setattr(agent_cli, "create_agent", lambda _path: FakeAgent())
    monkeypatch.setattr(agent_cli, "AgentHttpServer", FakeServer)

    assert (
        agent_cli.main(
            [
                "--config",
                "custom-agent.yaml",
                "--host",
                "0.0.0.0",
                "--port",
                "9090",
            ]
        )
        == 0
    )
    assert lifecycle == ["serve", "server-shutdown"]
    output = capsys.readouterr().out
    assert "Agent lifecycle delegated to the HTTP server" in output
    assert "Listening on http://0.0.0.0:9090" in output
    assert "Shutting down Lab Agent" in output


def test_agent_cli_reports_real_backend_in_once_mode(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    class FakeRealAgent:
        config = SimpleNamespace(
            effective_backends=[SimpleNamespace(id="physical", type="real")],
            simlab=SimpleNamespace(enabled=False),
        )

        async def start(self) -> None:
            return None

        async def shutdown(self) -> None:
            return None

        def benches(self) -> list[object]:
            return []

        def plugins(self) -> list[object]:
            return []

    monkeypatch.setattr(agent_cli, "create_agent", lambda _path: FakeRealAgent())
    assert agent_cli.main(["--once"]) == 0
    assert "RealLabBackend started" in capsys.readouterr().out


@pytest.mark.parametrize(
    ("path", "error_code", "message"),
    [
        (
            "/api/v1/artifacts",
            "ARTIFACT_TOO_LARGE",
            "Artifact request exceeds the configured upload limit.",
        ),
        (
            "/api/v1/benches/bench-01/actions/flash",
            "FIRMWARE_FILE_TOO_LARGE",
            "Firmware request exceeds the configured upload limit.",
        ),
    ],
)
def test_streamed_upload_without_content_length_enforces_request_cap(
    path: str,
    error_code: str,
    message: str,
) -> None:
    request_id = f"streamed-{error_code.casefold()}-limit"
    boundary = "lab-platform-stream-test"
    multipart_header = (
        f"--{boundary}\r\n"
        'Content-Disposition: form-data; name="file"; filename="large.bin"\r\n'
        "Content-Type: application/octet-stream\r\n\r\n"
    ).encode()
    multipart_footer = f"\r\n--{boundary}--\r\n".encode()

    class FakeAgent:
        started = True
        config = SimpleNamespace(
            agent=SimpleNamespace(name="stream-limit-agent", max_request_body_size_mb=1),
            artifacts=SimpleNamespace(max_upload_size_mb=1, max_firmware_size_mb=1),
        )

        async def start_background_workers(self) -> None:
            return None

        async def stop_background_workers(self) -> None:
            return None

    def streamed_body() -> Iterator[bytes]:
        yield multipart_header
        yield b"a" * (1024 * 1024)
        yield b"b" * (1024 * 1024)
        yield b"over-limit"
        yield multipart_footer

    app = create_app(cast(Any, FakeAgent()), manage_lifecycle=False)
    with (
        TestClient(app, raise_server_exceptions=False) as client,
        client.stream(
            "POST",
            path,
            headers={
                "Content-Type": f"multipart/form-data; boundary={boundary}",
                "X-Request-ID": request_id,
            },
            content=streamed_body(),
        ) as response,
    ):
        response.read()
        assert response.request.headers.get("Content-Length") is None
        assert response.status_code == 413
        assert response.headers["X-Request-ID"] == request_id
        assert response.json() == {
            "error": {
                "code": error_code,
                "message": message,
                "details": {"maximum_request_bytes": 2 * 1024 * 1024},
                "request_id": request_id,
            }
        }


def test_streamed_json_without_content_length_enforces_global_request_cap() -> None:
    request_id = "streamed-json-limit"

    class FakeAgent:
        started = True
        config = SimpleNamespace(
            agent=SimpleNamespace(name="stream-limit-agent", max_request_body_size_mb=1),
            artifacts=SimpleNamespace(max_upload_size_mb=10, max_firmware_size_mb=10),
        )

        async def start_background_workers(self) -> None:
            return None

        async def stop_background_workers(self) -> None:
            return None

    def streamed_body() -> Iterator[bytes]:
        yield b'{"name":"'
        yield b"a" * (1024 * 1024)
        yield b'","owner":"ci","scopes":["ci:sessions"]}'

    app = create_app(cast(Any, FakeAgent()), manage_lifecycle=False)
    with (
        TestClient(app, raise_server_exceptions=False) as client,
        client.stream(
            "POST",
            "/api/v1/tokens",
            headers={"Content-Type": "application/json", "X-Request-ID": request_id},
            content=streamed_body(),
        ) as response,
    ):
        response.read()
        assert response.request.headers.get("Content-Length") is None
        assert response.status_code == 413
        assert response.headers["X-Request-ID"] == request_id
        assert response.json() == {
            "error": {
                "code": "REQUEST_BODY_TOO_LARGE",
                "message": "Request body exceeds the configured size limit.",
                "details": {"maximum_request_bytes": 1024 * 1024},
                "request_id": request_id,
            }
        }


def test_cancelled_legacy_firmware_upload_removes_partial_file(tmp_path: Path) -> None:
    class CancelledUpload:
        filename = "firmware.bin"

        def __init__(self) -> None:
            self.reads = 0
            self.closed = False

        async def read(self, _size: int) -> bytes:
            self.reads += 1
            if self.reads == 1:
                return b"partial"
            raise asyncio.CancelledError

        async def close(self) -> None:
            self.closed = True

    async def scenario() -> None:
        upload = CancelledUpload()
        agent = SimpleNamespace(
            config=SimpleNamespace(artifacts=SimpleNamespace(max_firmware_size_mb=1)),
            artifacts_directory=tmp_path,
        )

        with pytest.raises(asyncio.CancelledError):
            await agent_api._store_firmware(agent, upload, None)

        assert upload.closed
        assert list((tmp_path / ".incoming").iterdir()) == []

    asyncio.run(scenario())
