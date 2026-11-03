from __future__ import annotations

import importlib
import json
from typing import Any

import pytest

cli = importlib.import_module("lab_platform.cli.main")

USER_ID = "11111111-1111-1111-1111-111111111111"
TEAM_ID = "22222222-2222-2222-2222-222222222222"
ACCOUNT_ID = "33333333-3333-3333-3333-333333333333"
CREDENTIAL_ID = "44444444-4444-4444-4444-444444444444"
ASSIGNMENT_ID = "55555555-5555-5555-5555-555555555555"
AUDIT_EVENT_ID = "66666666-6666-6666-6666-666666666666"


class StubClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, object]] = []
        self.responses: dict[tuple[str, str], object] = {}

    def get(self, path: str, query: dict[str, object] | None = None) -> object:
        self.calls.append(("GET", path, query))
        return self._response("GET", path)

    def post(self, path: str, payload: dict[str, object], **_kwargs: object) -> object:
        self.calls.append(("POST", path, payload))
        return self._response("POST", path)

    def put(self, path: str, payload: dict[str, object], **_kwargs: object) -> object:
        self.calls.append(("PUT", path, payload))
        return self._response("PUT", path)

    def patch(self, path: str, payload: dict[str, object], **_kwargs: object) -> object:
        self.calls.append(("PATCH", path, payload))
        return self._response("PATCH", path)

    def delete(self, path: str, payload: dict[str, object], **_kwargs: object) -> object:
        self.calls.append(("DELETE", path, payload))
        return self._response("DELETE", path)

    def _response(self, method: str, path: str) -> object:
        return self.responses.get((method, path), {})


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> StubClient:
    result = StubClient()
    monkeypatch.setattr(cli, "_client", lambda _args: result)
    return result


def _users() -> dict[str, Any]:
    return {
        "items": [
            {
                "id": USER_ID,
                "username": "alice",
                "display_name": "Alice Example",
                "email": "alice@example.test",
                "status": "ACTIVE",
                "authentication_source": "LOCAL",
                "created_at": "2026-08-02T00:00:00Z",
            }
        ]
    }


def _teams() -> dict[str, Any]:
    return {
        "items": [
            {
                "id": TEAM_ID,
                "slug": "hardware",
                "name": "Hardware",
                "description": "Lab operators",
                "created_at": "2026-08-02T00:00:00Z",
            }
        ]
    }


def _accounts() -> dict[str, Any]:
    return {
        "items": [
            {
                "id": ACCOUNT_ID,
                "name": "github-ci",
                "description": "GitHub Actions",
                "status": "ACTIVE",
                "created_at": "2026-08-02T00:00:00Z",
            }
        ]
    }


def test_service_account_commands_and_name_resolution(
    client: StubClient,
    capsys: pytest.CaptureFixture[str],
) -> None:
    account = _accounts()["items"][0]
    assert isinstance(account, dict)
    client.responses.update(
        {
            ("POST", "/api/v1/service-accounts"): account,
            ("GET", "/api/v1/service-accounts"): _accounts(),
            ("GET", f"/api/v1/service-accounts/{ACCOUNT_ID}"): account,
            ("PATCH", f"/api/v1/service-accounts/{ACCOUNT_ID}"): {
                **account,
                "status": "DISABLED",
            },
            ("DELETE", f"/api/v1/service-accounts/{ACCOUNT_ID}"): None,
        }
    )

    assert (
        cli.main(
            [
                "service-account",
                "create",
                "--name",
                "github-ci",
                "--description",
                "GitHub Actions",
                "--output",
                "json",
            ]
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out)["id"] == ACCOUNT_ID
    assert cli.main(["service-account", "list"]) == 0
    assert "github-ci" in capsys.readouterr().out
    assert cli.main(["service-account", "show", ACCOUNT_ID]) == 0
    assert "GitHub Actions" in capsys.readouterr().out
    assert cli.main(["service-account", "disable", "github-ci"]) == 0
    assert "disabled" in capsys.readouterr().out
    assert cli.main(["service-account", "delete", ACCOUNT_ID, "--output", "json"]) == 0
    assert json.loads(capsys.readouterr().out) == {
        "deleted": True,
        "service_account_id": ACCOUNT_ID,
    }

    assert (
        "PATCH",
        f"/api/v1/service-accounts/{ACCOUNT_ID}",
        {"status": "DISABLED"},
    ) in client.calls
    assert client.calls[-1] == ("DELETE", f"/api/v1/service-accounts/{ACCOUNT_ID}", {})


def test_service_account_credentials_show_secret_once_and_support_restrictions(
    client: StubClient,
    capsys: pytest.CaptureFixture[str],
) -> None:
    client.responses[("GET", "/api/v1/service-accounts")] = _accounts()
    credential = {
        "id": CREDENTIAL_ID,
        "name": "github-main",
        "expires_at": "2027-01-01T00:00:00Z",
        "last_used_at": None,
        "revoked_at": None,
    }
    path = f"/api/v1/service-accounts/{ACCOUNT_ID}/credentials"
    client.responses[("POST", path)] = {
        "credential": credential,
        "token": "lp_one_time_secret",
    }
    client.responses[("GET", path)] = {"items": [credential]}
    client.responses[("DELETE", f"/api/v1/credentials/{CREDENTIAL_ID}")] = None

    assert (
        cli.main(
            [
                "service-account",
                "credential",
                "create",
                "--service-account",
                "github-ci",
                "--name",
                "github-main",
                "--expires-at",
                "2027-01-01T00:00:00Z",
                "--allowed-ip",
                "10.0.0.0/8",
                "--permission",
                "ci:run",
            ]
        )
        == 0
    )
    output = capsys.readouterr().out
    assert output.count("lp_one_time_secret") == 1
    assert "will not be shown again" in output
    assert client.calls[-1] == (
        "POST",
        path,
        {
            "name": "github-main",
            "expires_at": "2027-01-01T00:00:00Z",
            "allowed_ip_ranges": ["10.0.0.0/8"],
            "permission_restrictions": ["ci:run"],
        },
    )

    assert (
        cli.main(
            [
                "service-account",
                "credential",
                "list",
                "--service-account",
                ACCOUNT_ID,
            ]
        )
        == 0
    )
    assert "github-main" in capsys.readouterr().out
    assert (
        cli.main(
            [
                "service-account",
                "credential",
                "revoke",
                CREDENTIAL_ID,
                "--output",
                "json",
            ]
        )
        == 0
    )
    revoked = json.loads(capsys.readouterr().out)
    assert revoked == {"credential_id": CREDENTIAL_ID, "revoked": True}


def test_organisation_show_and_update(
    client: StubClient,
    capsys: pytest.CaptureFixture[str],
) -> None:
    organisation = {
        "id": "00000000-0000-0000-0000-000000000001",
        "slug": "default",
        "name": "Default Organisation",
        "status": "ACTIVE",
        "created_at": "2026-08-02T00:00:00Z",
    }
    client.responses[("GET", "/api/v1/organisation")] = organisation
    client.responses[("PATCH", "/api/v1/organisation")] = {
        **organisation,
        "name": "Robotics Lab",
    }

    assert cli.main(["organisation", "show"]) == 0
    assert "Default Organisation" in capsys.readouterr().out
    assert cli.main(["organisation", "update", "--name", "Robotics Lab"]) == 0
    assert "Robotics Lab" in capsys.readouterr().out
    assert client.calls[-1] == ("PATCH", "/api/v1/organisation", {"name": "Robotics Lab"})


def test_user_commands_use_secure_password_sources_and_name_resolution(
    client: StubClient,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    user = _users()["items"][0]
    assert isinstance(user, dict)
    client.responses.update(
        {
            ("GET", "/api/v1/users"): _users(),
            ("POST", "/api/v1/users"): user,
            ("GET", f"/api/v1/users/{USER_ID}"): user,
            ("POST", f"/api/v1/users/{USER_ID}/disable"): {**user, "status": "DISABLED"},
            ("POST", f"/api/v1/users/{USER_ID}/enable"): user,
            ("POST", f"/api/v1/users/{USER_ID}/reset-password"): None,
        }
    )
    monkeypatch.setenv("NEW_USER_PASSWORD", "correct horse battery staple")

    assert (
        cli.main(
            [
                "user",
                "create",
                "--username",
                "alice",
                "--display-name",
                "Alice Example",
                "--email",
                "alice@example.test",
                "--organisation-role",
                "admin",
                "--password-env",
                "NEW_USER_PASSWORD",
                "--output",
                "json",
            ]
        )
        == 0
    )
    captured = capsys.readouterr()
    assert json.loads(captured.out)["username"] == "alice"
    assert "correct horse" not in captured.out + captured.err
    assert client.calls[-1][2] == {
        "username": "alice",
        "display_name": "Alice Example",
        "password": "correct horse battery staple",
        "email": "alice@example.test",
        "organisation_role": "ADMIN",
        "authentication_source": "LOCAL",
    }

    assert cli.main(["user", "list"]) == 0
    assert "Alice Example" in capsys.readouterr().out
    assert cli.main(["user", "show", USER_ID]) == 0
    assert "alice@example.test" in capsys.readouterr().out
    assert cli.main(["user", "disable", "alice"]) == 0
    assert "disabled" in capsys.readouterr().out
    assert cli.main(["user", "enable", USER_ID, "--output", "json"]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "ACTIVE"

    answers = iter(("replacement password value", "replacement password value"))
    monkeypatch.setattr(cli.getpass, "getpass", lambda _prompt: next(answers))
    assert cli.main(["user", "reset-password", "alice"]) == 0
    output = capsys.readouterr()
    assert "Password reset" in output.out
    assert "replacement password" not in output.out + output.err
    assert client.calls[-1] == (
        "POST",
        f"/api/v1/users/{USER_ID}/reset-password",
        {"password": "replacement password value"},
    )


def test_oidc_user_create_omits_password_and_rejects_password_source(
    client: StubClient,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    oidc_user = {
        **_users()["items"][0],
        "username": "oidc-user",
        "authentication_source": "OIDC",
    }
    client.responses[("POST", "/api/v1/users")] = oidc_user
    monkeypatch.setattr(
        cli.getpass,
        "getpass",
        lambda _prompt: (_ for _ in ()).throw(AssertionError("password prompt was used")),
    )

    assert (
        cli.main(
            [
                "user",
                "create",
                "--username",
                "oidc-user",
                "--display-name",
                "OIDC User",
                "--authentication-source",
                "oidc",
                "--output",
                "json",
            ]
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out)["authentication_source"] == "OIDC"
    assert client.calls[-1] == (
        "POST",
        "/api/v1/users",
        {
            "username": "oidc-user",
            "display_name": "OIDC User",
            "email": None,
            "organisation_role": "MEMBER",
            "authentication_source": "OIDC",
        },
    )

    monkeypatch.setenv("UNUSED_OIDC_PASSWORD", "must not be sent")
    call_count = len(client.calls)
    assert (
        cli.main(
            [
                "user",
                "create",
                "--username",
                "invalid-oidc",
                "--display-name",
                "Invalid OIDC",
                "--authentication-source",
                "oidc",
                "--password-env",
                "UNUSED_OIDC_PASSWORD",
            ]
        )
        == 1
    )
    captured = capsys.readouterr()
    assert "--password-env cannot be used for an OIDC user" in captured.err
    assert "must not be sent" not in captured.out + captured.err
    assert len(client.calls) == call_count


def test_password_arguments_are_rejected_and_prompt_errors_are_safe(
    client: StubClient,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    del client
    with pytest.raises(SystemExit):
        cli._build_parser().parse_args(
            [
                "user",
                "create",
                "--username",
                "alice",
                "--display-name",
                "Alice",
                "--password",
                "unsafe",
            ]
        )
    assert "unrecognized arguments: --password unsafe" in capsys.readouterr().err

    answers = iter(("first secret", "different secret"))
    monkeypatch.setattr(cli.getpass, "getpass", lambda _prompt: next(answers))
    assert cli.main(["user", "create", "--username", "alice", "--display-name", "Alice"]) == 1
    captured = capsys.readouterr()
    assert "passwords do not match" in captured.err
    assert "first secret" not in captured.out + captured.err


def test_team_commands_resolve_team_and_user_names(
    client: StubClient,
    capsys: pytest.CaptureFixture[str],
) -> None:
    team = _teams()["items"][0]
    assert isinstance(team, dict)
    membership = {"id": ASSIGNMENT_ID, "team_id": TEAM_ID, "user_id": USER_ID, "role": "MANAGER"}
    client.responses.update(
        {
            ("GET", "/api/v1/teams"): _teams(),
            ("GET", "/api/v1/users"): _users(),
            ("POST", "/api/v1/teams"): team,
            ("GET", f"/api/v1/teams/{TEAM_ID}"): team,
            ("POST", f"/api/v1/teams/{TEAM_ID}/members"): membership,
            ("DELETE", f"/api/v1/teams/{TEAM_ID}/members/{USER_ID}"): None,
            ("DELETE", f"/api/v1/teams/{TEAM_ID}"): None,
        }
    )

    assert (
        cli.main(
            [
                "team",
                "create",
                "--slug",
                "hardware",
                "--name",
                "Hardware",
                "--description",
                "Lab operators",
                "--output",
                "json",
            ]
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out)["slug"] == "hardware"
    assert cli.main(["team", "list"]) == 0
    assert "Lab operators" in capsys.readouterr().out
    assert cli.main(["team", "show", TEAM_ID]) == 0
    assert "Hardware" in capsys.readouterr().out
    assert cli.main(["team", "add-member", "hardware", "--user", "alice", "--role", "manager"]) == 0
    assert "Team member added" in capsys.readouterr().out
    assert client.calls[-1] == (
        "POST",
        f"/api/v1/teams/{TEAM_ID}/members",
        {"user_id": USER_ID, "role": "MANAGER"},
    )
    assert cli.main(["team", "remove-member", TEAM_ID, "--user", USER_ID]) == 0
    assert "Removed user" in capsys.readouterr().out
    assert cli.main(["team", "delete", TEAM_ID, "--output", "json"]) == 0
    assert json.loads(capsys.readouterr().out)["team_id"] == TEAM_ID


def test_role_commands_resolve_subjects_and_send_structured_resources(
    client: StubClient,
    capsys: pytest.CaptureFixture[str],
) -> None:
    client.responses.update(
        {
            ("GET", "/api/v1/roles"): {"items": ["OPERATOR", "VIEWER"]},
            ("GET", "/api/v1/roles/OPERATOR/permissions"): {
                "role": "OPERATOR",
                "permissions": ["benches:read", "benches:operate"],
            },
            ("GET", "/api/v1/users"): _users(),
            ("GET", "/api/v1/service-accounts"): _accounts(),
            ("POST", "/api/v1/role-assignments"): {
                "id": ASSIGNMENT_ID,
                "subject_type": "USER",
                "subject_id": USER_ID,
                "role": "OPERATOR",
                "resource_type": "BENCH",
                "resource_id": "home-lab/esp32",
            },
            ("DELETE", f"/api/v1/role-assignments/{ASSIGNMENT_ID}"): None,
            ("GET", "/api/v1/permissions/effective"): {
                "allowed": True,
                "roles": ["WORKFLOW_RUNNER"],
                "permissions": ["workflows:run"],
                "granting_assignment_ids": [ASSIGNMENT_ID],
            },
        }
    )

    assert cli.main(["role", "list"]) == 0
    assert "OPERATOR" in capsys.readouterr().out
    assert cli.main(["role", "permissions", "operator"]) == 0
    assert "benches:operate" in capsys.readouterr().out
    assert (
        cli.main(
            [
                "role",
                "assign",
                "--subject",
                "user:alice",
                "--role",
                "operator",
                "--resource",
                "bench:home-lab/esp32",
                "--expires-at",
                "2027-01-01T00:00:00Z",
                "--output",
                "json",
            ]
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out)["id"] == ASSIGNMENT_ID
    assert client.calls[-1] == (
        "POST",
        "/api/v1/role-assignments",
        {
            "subject_type": "USER",
            "subject_id": USER_ID,
            "role": "OPERATOR",
            "resource_type": "BENCH",
            "resource_id": "home-lab/esp32",
            "expires_at": "2027-01-01T00:00:00Z",
        },
    )
    assert cli.main(["role", "revoke", ASSIGNMENT_ID]) == 0
    assert "revoked" in capsys.readouterr().out
    assert (
        cli.main(
            [
                "role",
                "effective",
                "--subject",
                "service-account:github-ci",
                "--resource",
                "workflow:smoke-test",
                "--permission",
                "workflows:run",
            ]
        )
        == 0
    )
    assert "WORKFLOW_RUNNER" in capsys.readouterr().out
    assert client.calls[-1] == (
        "GET",
        "/api/v1/permissions/effective",
        {
            "subject_type": "SERVICE_ACCOUNT",
            "subject_id": ACCOUNT_ID,
            "resource_type": "WORKFLOW",
            "resource_id": "smoke-test",
            "permission": "workflows:run",
            "parent_agent_id": None,
        },
    )


def test_access_policy_commands_support_defaults_upserts_and_team_resolution(
    client: StubClient,
    capsys: pytest.CaptureFixture[str],
) -> None:
    bench_path = "/api/v1/access-policies/benches/home-lab/esp32"
    workflow_path = "/api/v1/access-policies/workflows/smoke-test"
    client.responses.update(
        {
            ("GET", bench_path): {
                "resource_type": "BENCH",
                "resource_id": "home-lab/esp32",
                "configured": False,
                "source": "DEFAULT",
                "policy": {
                    "bench_id": "home-lab/esp32",
                    "visibility": "ORGANISATION",
                    "reservation_role": None,
                    "operation_role": None,
                    "allowed_team_ids": [],
                },
            },
            ("GET", "/api/v1/teams"): _teams(),
            ("PUT", bench_path): {
                "resource_type": "BENCH",
                "resource_id": "home-lab/esp32",
                "configured": True,
                "source": "CONFIGURED",
                "policy": {
                    "bench_id": "home-lab/esp32",
                    "visibility": "RESTRICTED",
                    "reservation_role": "RESERVER",
                    "operation_role": "OPERATOR",
                    "allowed_team_ids": [TEAM_ID],
                },
            },
            ("PUT", workflow_path): {
                "resource_type": "WORKFLOW",
                "resource_id": "smoke-test",
                "configured": True,
                "source": "CONFIGURED",
                "policy": {
                    "workflow_id": "smoke-test",
                    "visibility": "ADMIN_ONLY",
                },
            },
        }
    )

    assert cli.main(["access-policy", "bench", "get", "home-lab/esp32"]) == 0
    default_output = capsys.readouterr().out
    assert "DEFAULT" in default_output
    assert "No" in default_output

    assert (
        cli.main(
            [
                "access-policy",
                "bench",
                "set",
                "home-lab/esp32",
                "--visibility",
                "restricted",
                "--reservation-role",
                "reserver",
                "--operation-role",
                "operator",
                "--allowed-team",
                "hardware",
                "--output",
                "json",
            ]
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out)["source"] == "CONFIGURED"
    assert client.calls[-1] == (
        "PUT",
        bench_path,
        {
            "visibility": "RESTRICTED",
            "reservation_role": "RESERVER",
            "operation_role": "OPERATOR",
            "allowed_team_ids": [TEAM_ID],
        },
    )

    assert (
        cli.main(
            [
                "access-policy",
                "workflow",
                "set",
                "smoke-test",
                "--visibility",
                "admin-only",
            ]
        )
        == 0
    )
    assert "ADMIN_ONLY" in capsys.readouterr().out
    assert client.calls[-1] == (
        "PUT",
        workflow_path,
        {"visibility": "ADMIN_ONLY"},
    )


def test_audit_list_and_show_commands(
    client: StubClient,
    capsys: pytest.CaptureFixture[str],
) -> None:
    event = {
        "id": AUDIT_EVENT_ID,
        "timestamp": "2026-08-02T14:02:11Z",
        "actor_display_name": "Alice Example",
        "action": "BENCH_FLASH_REQUESTED",
        "resource_type": "BENCH",
        "resource_id": "home-lab/esp32",
        "outcome": "SUCCEEDED",
        "metadata": {},
    }
    client.responses[("GET", "/api/v1/audit-events")] = {"items": [event]}
    client.responses[("GET", f"/api/v1/audit-events/{AUDIT_EVENT_ID}")] = event

    assert (
        cli.main(
            [
                "audit",
                "list",
                "--action",
                "BENCH_FLASH_REQUESTED",
                "--outcome",
                "succeeded",
                "--after",
                "2026-08-01T00:00:00Z",
                "--limit",
                "25",
            ]
        )
        == 0
    )
    output = capsys.readouterr().out
    assert "Alice Example" in output
    assert "home-lab/esp32" in output
    assert client.calls[-1] == (
        "GET",
        "/api/v1/audit-events",
        {
            "action": "BENCH_FLASH_REQUESTED",
            "outcome": "SUCCEEDED",
            "after": "2026-08-01T00:00:00Z",
            "before": None,
            "limit": 25,
        },
    )

    assert cli.main(["audit", "show", AUDIT_EVENT_ID, "--output", "json"]) == 0
    assert json.loads(capsys.readouterr().out)["id"] == AUDIT_EVENT_ID


@pytest.mark.parametrize(
    ("reference", "message"),
    [
        ("invalid", "must use type:name syntax"),
        ("robot:alice", "Unsupported subject type"),
    ],
)
def test_role_subject_validation(
    client: StubClient,
    capsys: pytest.CaptureFixture[str],
    reference: str,
    message: str,
) -> None:
    assert (
        cli.main(
            [
                "role",
                "assign",
                "--subject",
                reference,
                "--role",
                "viewer",
                "--resource",
                "bench:lab/one",
            ]
        )
        == 1
    )
    assert message in capsys.readouterr().err


def test_name_resolution_reports_missing_ambiguous_and_invalid_server_data(
    client: StubClient,
) -> None:
    client.responses[("GET", "/api/v1/users")] = {"items": []}
    with pytest.raises(ValueError, match="Unknown user"):
        cli._resolve_entity_id(client, "/api/v1/users", "nobody", ("username",), "user")

    client.responses[("GET", "/api/v1/users")] = {
        "items": [
            {"id": USER_ID, "username": "alice"},
            {"id": TEAM_ID, "username": "ALICE"},
        ]
    }
    with pytest.raises(ValueError, match="Ambiguous user"):
        cli._resolve_entity_id(client, "/api/v1/users", "alice", ("username",), "user")

    client.responses[("GET", "/api/v1/users")] = {"items": [{"username": "alice"}]}
    with pytest.raises(ValueError, match="without an ID"):
        cli._resolve_entity_id(client, "/api/v1/users", "alice", ("username",), "user")


def test_admin_parser_exposes_every_phase6_command() -> None:
    parser = cli._build_parser()
    commands: list[list[str]] = [
        ["service-account", "create", "--name", "ci"],
        ["service-account", "list"],
        ["service-account", "show", ACCOUNT_ID],
        ["service-account", "disable", ACCOUNT_ID],
        ["service-account", "delete", ACCOUNT_ID],
        ["service-account", "credential", "create", "--service-account", "ci", "--name", "key"],
        ["service-account", "credential", "list", "--service-account", "ci"],
        ["service-account", "credential", "revoke", CREDENTIAL_ID],
        ["organisation", "show"],
        ["organisation", "update", "--name", "Lab"],
        ["user", "create", "--username", "a", "--display-name", "A"],
        ["user", "list"],
        ["user", "show", USER_ID],
        ["user", "disable", USER_ID],
        ["user", "enable", USER_ID],
        ["user", "reset-password", USER_ID],
        ["team", "create", "--slug", "lab", "--name", "Lab"],
        ["team", "list"],
        ["team", "show", TEAM_ID],
        ["team", "add-member", TEAM_ID, "--user", USER_ID],
        ["team", "remove-member", TEAM_ID, "--user", USER_ID],
        ["team", "delete", TEAM_ID],
        ["role", "list"],
        ["role", "permissions", "viewer"],
        ["role", "assign", "--subject", "user:a", "--role", "viewer", "--resource", "bench:b"],
        ["role", "revoke", ASSIGNMENT_ID],
        ["role", "effective", "--subject", "user:a", "--resource", "bench:b"],
        ["access-policy", "bench", "get", "lab/bench"],
        [
            "access-policy",
            "bench",
            "set",
            "lab/bench",
            "--visibility",
            "private",
        ],
        ["access-policy", "workflow", "get", "smoke-test"],
        [
            "access-policy",
            "workflow",
            "set",
            "smoke-test",
            "--visibility",
            "restricted",
        ],
    ]
    assert [parser.parse_args(arguments).command for arguments in commands] == [
        arguments[0] for arguments in commands
    ]


def test_agent_client_patch_uses_patch_method(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    def request(self: object, request: object, *, timeout: float | None = None) -> object:
        captured["request"] = request
        captured["timeout"] = timeout
        return {"ok": True}

    monkeypatch.setattr("lab_platform.cli.client.AgentClient._request", request)
    client = cli.AgentClient("https://lab.example", token="identity-token")

    assert client.patch("/api/v1/organisation", {"name": "Lab"}) == {"ok": True}
    request_value = captured["request"]
    assert request_value.get_method() == "PATCH"
    assert request_value.get_header("Authorization") == "Bearer identity-token"


def test_agent_client_put_uses_put_method(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    def request(self: object, request: object, *, timeout: float | None = None) -> object:
        captured["request"] = request
        captured["timeout"] = timeout
        return {"ok": True}

    monkeypatch.setattr("lab_platform.cli.client.AgentClient._request", request)
    client = cli.AgentClient("https://lab.example", token="identity-token")

    assert client.put("/api/v1/access-policies/workflows/test", {"visibility": "PRIVATE"}) == {
        "ok": True
    }
    request_value = captured["request"]
    assert request_value.get_method() == "PUT"
    assert request_value.get_header("Authorization") == "Bearer identity-token"
