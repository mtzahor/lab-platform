from __future__ import annotations

import importlib
import json
import subprocess
from email.message import Message
from io import BytesIO
from pathlib import Path
from typing import Any
from urllib.error import HTTPError
from urllib.request import Request

import lab_platform.cli.client as client_module
import pytest
from lab_platform.cli.client import AgentApiError, AgentClient
from lab_platform.cli.credentials import (
    CredentialStoreError,
    CredentialStoreUnavailableError,
    NativeCredentialStore,
    StoredCredential,
)

cli = importlib.import_module("lab_platform.cli.main")


class MemoryCredentialStore:
    def __init__(
        self,
        values: dict[str, StoredCredential | str] | None = None,
    ) -> None:
        self.values = {
            server: (
                value
                if isinstance(value, StoredCredential)
                else StoredCredential(access_token=value, refresh_token=value)
            )
            for server, value in (values or {}).items()
        }
        self.loads: list[str] = []
        self.saves: list[tuple[str, StoredCredential]] = []
        self.deletes: list[str] = []

    def load(self, server: str) -> StoredCredential | None:
        self.loads.append(server)
        return self.values.get(server)

    def save(self, server: str, credential: StoredCredential) -> None:
        self.saves.append((server, credential))
        self.values[server] = credential

    def delete(self, server: str) -> bool:
        self.deletes.append(server)
        return self.values.pop(server, None) is not None


class StubClient:
    instances: list[StubClient] = []
    login_response: object = {}
    me_response: object = {}
    logout_error: AgentApiError | None = None

    def __init__(
        self,
        base_url: str,
        timeout: float = 10.0,
        *,
        token: str | None = None,
        refresh_token: str | None = None,
        on_session_refresh: object | None = None,
    ) -> None:
        self.base_url = base_url
        self.timeout = timeout
        self.token = token
        self.refresh_token = refresh_token
        self.on_session_refresh = on_session_refresh
        self.calls: list[tuple[str, str, object]] = []
        self.instances.append(self)

    def get(
        self,
        path: str,
        query: dict[str, object] | None = None,
        **_kwargs: object,
    ) -> object:
        self.calls.append(("GET", path, query))
        if path == "/api/v1/auth/me":
            return self.me_response
        return {}

    def post(
        self,
        path: str,
        payload: dict[str, object],
        **_kwargs: object,
    ) -> object:
        self.calls.append(("POST", path, payload))
        if path == "/api/v1/auth/login":
            return self.login_response
        if path == "/api/v1/auth/logout" and self.logout_error is not None:
            raise self.logout_error
        return {}


@pytest.fixture(autouse=True)
def reset_stub_client(monkeypatch: pytest.MonkeyPatch) -> None:
    StubClient.instances = []
    StubClient.login_response = {}
    StubClient.me_response = {}
    StubClient.logout_error = None
    monkeypatch.setattr(cli, "AgentClient", StubClient)
    monkeypatch.delenv("LAB_PLATFORM_TOKEN", raising=False)
    monkeypatch.delenv("LAB_PLATFORM_SERVER", raising=False)


def test_auth_login_prompts_and_stores_server_scoped_token_without_printing_secrets(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    store = MemoryCredentialStore()
    monkeypatch.setattr(cli, "_credential_store", lambda: store)
    monkeypatch.setattr("builtins.input", lambda prompt: "alice")
    monkeypatch.setattr(cli.getpass, "getpass", lambda prompt: "very-secret-password")
    StubClient.login_response = {
        "access_token": "lp_access_secret",
        "expires_at": "2026-08-02T12:15:00Z",
        "principal": {"username": "alice", "display_name": "Alice"},
        "organisation": {"slug": "simlab-demo", "name": "SimLab Demo"},
        "session": {"id": "session-1"},
    }

    assert (
        cli.main(
            [
                "auth",
                "login",
                "--server",
                "https://lab.example/",
                "--organisation",
                "simlab-demo",
            ]
        )
        == 0
    )

    assert store.saves == [
        (
            "https://lab.example/",
            StoredCredential(
                access_token="lp_access_secret",
                refresh_token="lp_access_secret",
                session_id="session-1",
                expires_at="2026-08-02T12:15:00Z",
            ),
        )
    ]
    client = StubClient.instances[0]
    assert client.token is None
    assert client.calls == [
        (
            "POST",
            "/api/v1/auth/login",
            {
                "username": "alice",
                "password": "very-secret-password",
                "organisation_slug": "simlab-demo",
            },
        )
    ]
    captured = capsys.readouterr()
    assert "Logged in as alice" in captured.out
    assert "simlab-demo" in captured.out
    assert "very-secret-password" not in captured.out + captured.err
    assert "lp_access_secret" not in captured.out + captured.err


def test_auth_login_accepts_username_but_never_a_password_argument(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    store = MemoryCredentialStore()
    monkeypatch.setattr(cli, "_credential_store", lambda: store)
    monkeypatch.setattr(
        "builtins.input",
        lambda _prompt: pytest.fail("username should not be prompted"),
    )
    monkeypatch.setattr(cli.getpass, "getpass", lambda _prompt: "password-from-prompt")
    StubClient.login_response = {
        "access_token": "stored-token",
        "principal": {"username": "alice"},
        "organisation": {"slug": "demo"},
        "session": {"id": "session-1"},
    }

    assert cli.main(["auth", "login", "--username", "alice", "--output", "json"]) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["principal"] == {"username": "alice"}
    assert "access_token" not in output

    with pytest.raises(SystemExit):
        cli._build_parser().parse_args(
            ["auth", "login", "--username", "alice", "--password", "do-not-accept"]
        )


def test_auth_status_uses_environment_token_before_the_native_store(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    class UnexpectedStore(MemoryCredentialStore):
        def load(self, server: str) -> StoredCredential | None:
            pytest.fail(f"credential store should not be read for {server}")

    monkeypatch.setattr(cli, "_credential_store", UnexpectedStore)
    monkeypatch.setenv("LAB_PLATFORM_TOKEN", "environment-token")
    StubClient.me_response = {
        "principal": {"username": "ci-user", "type": "SERVICE_ACCOUNT"},
        "organisation": {"slug": "simlab-demo"},
    }

    assert cli.main(["--server", "https://lab.example", "auth", "status"]) == 0

    client = StubClient.instances[0]
    assert client.token == "environment-token"
    assert client.refresh_token is None
    assert client.calls == [("GET", "/api/v1/auth/me", None)]
    output = capsys.readouterr().out
    assert "ci-user" in output
    assert "environment variable LAB_PLATFORM_TOKEN" in output


def test_auth_whoami_uses_stored_token_and_supports_json(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    server = "https://lab.example"
    store = MemoryCredentialStore({server: "stored-token"})
    monkeypatch.setattr(cli, "_credential_store", lambda: store)
    StubClient.me_response = {
        "principal": {"username": "alice", "display_name": "Alice"},
        "organisation": {"slug": "simlab-demo"},
    }

    assert cli.main(["--server", server, "auth", "whoami", "--output", "json"]) == 0

    assert store.loads == [server]
    assert StubClient.instances[0].token == "stored-token"
    assert json.loads(capsys.readouterr().out) == StubClient.me_response


def test_auth_status_without_a_stored_credential_is_not_authenticated(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    store = MemoryCredentialStore()
    monkeypatch.setattr(cli, "_credential_store", lambda: store)

    assert cli.main(["auth", "status", "--output", "json"]) == 1
    assert json.loads(capsys.readouterr().out) == {
        "authenticated": False,
        "server": "http://127.0.0.1:8080",
    }
    assert StubClient.instances == []


def test_auth_logout_revokes_then_deletes_stored_token_even_when_remote_fails(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    server = "https://lab.example"
    store = MemoryCredentialStore({server: "stored-token"})
    monkeypatch.setattr(cli, "_credential_store", lambda: store)
    StubClient.logout_error = AgentApiError(401, "SESSION_REVOKED", "already revoked")

    assert cli.main(["--server", server, "auth", "logout"]) == 1

    assert store.deletes == [server]
    assert server not in store.values
    assert "SESSION_REVOKED" in capsys.readouterr().err


def test_auth_logout_handles_stored_environment_and_absent_credentials(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    server = "https://lab.example"
    store = MemoryCredentialStore({server: "stored-token"})
    monkeypatch.setattr(cli, "_credential_store", lambda: store)

    assert cli.main(["--server", server, "auth", "logout", "--output", "json"]) == 0
    assert StubClient.instances[-1].calls == [("POST", "/api/v1/auth/logout", {})]
    assert store.deletes == [server]
    assert json.loads(capsys.readouterr().out)["remote_session_revoked"] is True

    monkeypatch.setenv("LAB_PLATFORM_TOKEN", "environment-token")
    assert cli.main(["--server", server, "auth", "logout"]) == 0
    assert StubClient.instances[-1].token == "environment-token"
    assert "remains set" in capsys.readouterr().err

    monkeypatch.delenv("LAB_PLATFORM_TOKEN")
    assert cli.main(["--server", server, "auth", "logout"]) == 0
    assert "No stored login" in capsys.readouterr().out


def test_login_storage_failure_revokes_fresh_session_without_exposing_token(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    class FailingStore(MemoryCredentialStore):
        def save(self, server: str, credential: StoredCredential) -> None:
            del server, credential
            raise CredentialStoreUnavailableError("configure Secret Service")

    monkeypatch.setattr(cli, "_credential_store", FailingStore)
    monkeypatch.setattr(cli.getpass, "getpass", lambda _prompt: "password-from-prompt")
    StubClient.login_response = {
        "access_token": "fresh-secret-token",
        "principal": {"username": "alice"},
        "organisation": {"slug": "demo"},
        "session": {"id": "session-1"},
    }

    assert cli.main(["auth", "login", "--username", "alice"]) == 1

    assert len(StubClient.instances) == 2
    assert StubClient.instances[1].token == "fresh-secret-token"
    assert StubClient.instances[1].calls == [("POST", "/api/v1/auth/logout", {})]
    captured = capsys.readouterr()
    assert "configure Secret Service" in captured.err
    assert "fresh-secret-token" not in captured.out + captured.err


def test_ordinary_client_uses_stored_token_but_tolerates_an_unavailable_store(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    server = "https://lab.example"
    store = MemoryCredentialStore({server: "stored-token"})
    monkeypatch.setattr(cli, "_credential_store", lambda: store)
    args = cli._build_parser().parse_args(["--server", server, "health"])

    client = cli._client(args)
    assert client.token == "stored-token"

    class UnavailableStore(MemoryCredentialStore):
        def load(self, server: str) -> StoredCredential | None:
            raise CredentialStoreUnavailableError("headless")

    monkeypatch.setattr(cli, "_credential_store", UnavailableStore)
    unavailable_args = cli._build_parser().parse_args(
        ["--config", str(tmp_path / "missing.yaml"), "health"]
    )
    assert cli._client(unavailable_args).token is None


def test_stored_credential_round_trip_and_legacy_access_token_migration() -> None:
    credential = StoredCredential(
        access_token="access-secret",
        refresh_token="refresh-secret",
        session_id="session-1",
        expires_at="2026-08-02T12:15:00+00:00",
        maximum_expires_at="2026-08-03T00:00:00+00:00",
    )

    encoded = credential.encode()
    assert StoredCredential.decode(encoded) == credential
    assert "access-secret" not in repr(credential)
    assert "refresh-secret" not in repr(credential)
    assert StoredCredential.decode("legacy-access-token") == StoredCredential(
        access_token="legacy-access-token",
        refresh_token="legacy-access-token",
    )
    distinct_refresh = StoredCredential.from_auth_response(
        {"access_token": "next-access"},
        previous=credential,
    )
    assert distinct_refresh.refresh_token == "refresh-secret"

    with pytest.raises(CredentialStoreError, match="unsupported"):
        StoredCredential.decode('{"format":"unknown","version":1}')
    with pytest.raises(CredentialStoreError, match="malformed"):
        StoredCredential.decode("{not-json")


def test_main_client_persists_rotated_session_as_one_credential_bundle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    server = "https://lab.example"
    store = MemoryCredentialStore(
        {
            server: StoredCredential(
                access_token="old-access",
                refresh_token="old-access",
                session_id="session-1",
                expires_at="old-expiry",
                maximum_expires_at="maximum-expiry",
            )
        }
    )
    monkeypatch.setattr(cli, "_credential_store", lambda: store)
    args = cli._build_parser().parse_args(["--server", server, "health"])

    client = cli._client(args)
    assert isinstance(client, StubClient)
    assert client.token == "old-access"
    assert client.refresh_token == "old-access"
    assert callable(client.on_session_refresh)
    client.on_session_refresh(
        {
            "access_token": "new-access",
            "expires_at": "new-expiry",
            "session": {
                "id": "session-1",
                "maximum_expires_at": "maximum-expiry",
            },
        }
    )

    assert store.saves == [
        (
            server,
            StoredCredential(
                access_token="new-access",
                refresh_token="new-access",
                session_id="session-1",
                expires_at="new-expiry",
                maximum_expires_at="maximum-expiry",
            ),
        )
    ]


def test_main_deletes_stale_native_entry_when_refresh_persistence_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    server = "https://lab.example"

    class FailingRefreshStore(MemoryCredentialStore):
        def save(self, server: str, credential: StoredCredential) -> None:
            del server, credential
            raise CredentialStoreError("keychain locked")

    store = FailingRefreshStore({server: "old-access"})
    monkeypatch.setattr(cli, "_credential_store", lambda: store)
    args = cli._build_parser().parse_args(["--server", server, "health"])
    client = cli._client(args)
    assert isinstance(client, StubClient)
    assert callable(client.on_session_refresh)

    with pytest.raises(CredentialStoreError, match="keychain locked"):
        client.on_session_refresh({"access_token": "new-access"})
    assert store.deletes == [server]
    assert server not in store.values


class JsonResponse(BytesIO):
    status = 200

    def __enter__(self) -> JsonResponse:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()


class ScriptedUrlOpen:
    def __init__(self, replies: list[JsonResponse | HTTPError]) -> None:
        self.replies = list(replies)
        self.requests: list[Request] = []

    def __call__(self, request: Request, *, timeout: float) -> JsonResponse:
        del timeout
        self.requests.append(request)
        reply = self.replies.pop(0)
        if isinstance(reply, HTTPError):
            raise reply
        return reply


def _json_response(payload: object) -> JsonResponse:
    return JsonResponse(json.dumps(payload).encode("utf-8"))


def _api_error(code: str) -> HTTPError:
    return HTTPError(
        "https://lab.example/api/v1/benches",
        401,
        "Unauthorized",
        Message(),
        BytesIO(json.dumps({"error": {"code": code, "message": "credential expired"}}).encode()),
    )


def test_agent_client_transparently_refreshes_and_retries_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    opened = ScriptedUrlOpen(
        [
            _api_error("SESSION_EXPIRED"),
            _json_response(
                {
                    "access_token": "new-access",
                    "expires_at": "new-expiry",
                    "session": {"id": "session-1"},
                }
            ),
            _json_response({"items": []}),
        ]
    )
    monkeypatch.setattr(client_module, "urlopen", opened)
    refreshed: list[dict[str, object]] = []
    client = AgentClient(
        "https://lab.example",
        token="old-access",
        refresh_token="old-access",
        on_session_refresh=refreshed.append,
    )

    assert client.get("/api/v1/benches") == {"items": []}
    assert [request.full_url for request in opened.requests] == [
        "https://lab.example/api/v1/benches",
        "https://lab.example/api/v1/auth/refresh",
        "https://lab.example/api/v1/benches",
    ]
    assert opened.requests[0].get_header("Authorization") == "Bearer old-access"
    assert opened.requests[1].get_header("Authorization") == "Bearer old-access"
    assert opened.requests[2].get_header("Authorization") == "Bearer new-access"
    assert refreshed[0]["access_token"] == "new-access"


def test_agent_client_does_not_refresh_or_retry_twice(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    opened = ScriptedUrlOpen(
        [
            _api_error("TOKEN_EXPIRED"),
            _json_response({"access_token": "new-access"}),
            _api_error("SESSION_EXPIRED"),
        ]
    )
    monkeypatch.setattr(client_module, "urlopen", opened)
    client = AgentClient(
        "https://lab.example",
        token="old-access",
        refresh_token="old-access",
    )

    with pytest.raises(AgentApiError, match="credential expired") as captured:
        client.get("/api/v1/benches")
    assert captured.value.code == "SESSION_EXPIRED"
    assert len(opened.requests) == 3


def test_agent_client_refreshes_before_retrying_a_binary_download(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    opened = ScriptedUrlOpen(
        [
            _api_error("SESSION_EXPIRED"),
            _json_response({"access_token": "new-access"}),
            JsonResponse(b"artifact-bytes"),
        ]
    )
    monkeypatch.setattr(client_module, "urlopen", opened)
    client = AgentClient(
        "https://lab.example",
        token="old-access",
        refresh_token="old-access",
    )

    assert client.download("/api/v1/artifacts/artifact-1/content") == b"artifact-bytes"
    assert opened.requests[-1].get_header("Authorization") == "Bearer new-access"


def test_agent_client_revokes_rotated_session_when_persistence_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    opened = ScriptedUrlOpen(
        [
            _api_error("SESSION_EXPIRED"),
            _json_response({"access_token": "new-access"}),
            _json_response(None),
        ]
    )
    monkeypatch.setattr(client_module, "urlopen", opened)

    def fail_persistence(_payload: dict[str, object]) -> None:
        raise CredentialStoreError("keychain locked")

    client = AgentClient(
        "https://lab.example",
        token="old-access",
        refresh_token="old-access",
        on_session_refresh=fail_persistence,
    )

    with pytest.raises(CredentialStoreError, match="keychain locked"):
        client.get("/api/v1/benches")
    assert [request.full_url for request in opened.requests] == [
        "https://lab.example/api/v1/benches",
        "https://lab.example/api/v1/auth/refresh",
        "https://lab.example/api/v1/auth/logout",
    ]
    assert opened.requests[-1].get_header("Authorization") == "Bearer new-access"


class RecordingRunner:
    def __init__(self, replies: list[subprocess.CompletedProcess[str]]) -> None:
        self.replies = list(replies)
        self.calls: list[tuple[list[str], str | None]] = []

    def __call__(self, command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        input_value = kwargs.get("input")
        assert input_value is None or isinstance(input_value, str)
        self.calls.append((command, input_value))
        return self.replies.pop(0)


def _completed(
    returncode: int = 0,
    *,
    stdout: str = "",
    stderr: str = "",
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess([], returncode, stdout, stderr)


def test_macos_keychain_commands_keep_secret_out_of_argv() -> None:
    runner = RecordingRunner([_completed(), _completed(stdout="stored-token\n"), _completed()])
    store = NativeCredentialStore(
        platform="darwin",
        executable_finder=lambda name: f"/usr/bin/{name}",
        runner=runner,
    )

    credential = StoredCredential(
        access_token="stored-token",
        refresh_token="refresh-token",
        session_id="session-1",
    )
    store.save("https://lab.example/", credential)
    assert store.load("https://lab.example/") == StoredCredential(
        access_token="stored-token",
        refresh_token="stored-token",
    )
    assert store.delete("https://lab.example/")

    save_command, save_input = runner.calls[0]
    assert save_command[-1] == "-w"
    assert "stored-token" not in save_command
    assert save_input == f"{credential.encode()}\n"
    assert "refresh-token" not in save_command
    assert "https://lab.example" in save_command
    assert runner.calls[1][1] is None
    assert runner.calls[2][1] is None


def test_linux_secret_service_commands_keep_secret_out_of_argv() -> None:
    runner = RecordingRunner([_completed(), _completed(stdout="stored-token\n"), _completed()])
    store = NativeCredentialStore(
        platform="linux",
        executable_finder=lambda _name: "/usr/bin/secret-tool",
        runner=runner,
    )

    credential = StoredCredential(access_token="stored-token", refresh_token="refresh-token")
    store.save("https://lab.example", credential)
    assert store.load("https://lab.example") == StoredCredential(
        access_token="stored-token",
        refresh_token="stored-token",
    )
    assert store.delete("https://lab.example")

    save_command, save_input = runner.calls[0]
    assert save_command[1] == "store"
    assert "stored-token" not in save_command
    assert save_input == f"{credential.encode()}\n"
    assert runner.calls[1][0][1] == "lookup"
    assert runner.calls[2][0][1] == "clear"


def test_native_store_reports_unavailable_missing_and_operational_failures() -> None:
    unavailable = NativeCredentialStore(
        platform="linux",
        executable_finder=lambda _name: None,
    )
    with pytest.raises(CredentialStoreUnavailableError, match="secret-tool"):
        unavailable.load("https://lab.example")

    missing_runner = RecordingRunner([_completed(44, stderr="could not be found")])
    missing = NativeCredentialStore(
        platform="darwin",
        executable_finder=lambda _name: "/usr/bin/security",
        runner=missing_runner,
    )
    assert missing.load("https://lab.example") is None

    failed_runner = RecordingRunner([_completed(1, stderr="stored-token: keychain is locked")])
    failed = NativeCredentialStore(
        platform="darwin",
        executable_finder=lambda _name: "/usr/bin/security",
        runner=failed_runner,
    )
    with pytest.raises(CredentialStoreError, match="keychain is locked") as captured:
        failed.save(
            "https://lab.example",
            StoredCredential(access_token="stored-token", refresh_token="refresh-token"),
        )
    assert "stored-token" not in str(captured.value)


def test_native_store_validates_inputs_and_handles_empty_missing_and_failed_operations() -> None:
    empty_runner = RecordingRunner([_completed(stdout="")])
    empty = NativeCredentialStore(
        platform="linux",
        executable_finder=lambda _name: "/usr/bin/secret-tool",
        runner=empty_runner,
    )
    assert empty.load("https://lab.example") is None

    missing_runner = RecordingRunner([_completed(1), _completed(1)])
    missing = NativeCredentialStore(
        platform="linux",
        executable_finder=lambda _name: "/usr/bin/secret-tool",
        runner=missing_runner,
    )
    assert missing.load("https://lab.example") is None
    assert not missing.delete("https://lab.example")

    failed_delete_runner = RecordingRunner([_completed(2, stderr="service unavailable")])
    failed_delete = NativeCredentialStore(
        platform="linux",
        executable_finder=lambda _name: "/usr/bin/secret-tool",
        runner=failed_delete_runner,
    )
    with pytest.raises(CredentialStoreError, match="service unavailable"):
        failed_delete.delete("https://lab.example")

    validator = NativeCredentialStore(
        platform="linux",
        executable_finder=lambda _name: "/usr/bin/secret-tool",
        runner=RecordingRunner([]),
    )
    for server in ("", "https://lab.example\nmalicious"):
        with pytest.raises(ValueError, match="Server URL"):
            validator.load(server)
    for token in ("", "token\nmalicious"):
        with pytest.raises(ValueError, match="Access token"):
            validator.save(
                "https://lab.example",
                StoredCredential(access_token=token),
            )


def test_native_store_translates_process_start_failures() -> None:
    def fail_to_start(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        del command, kwargs
        raise OSError("executable disappeared")

    store = NativeCredentialStore(
        platform="linux",
        executable_finder=lambda _name: "/usr/bin/secret-tool",
        runner=fail_to_start,
    )

    with pytest.raises(CredentialStoreUnavailableError, match="executable disappeared"):
        store.load("https://lab.example")
