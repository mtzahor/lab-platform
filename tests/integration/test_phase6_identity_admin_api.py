from __future__ import annotations

from pathlib import Path
from uuid import UUID

from fastapi.testclient import TestClient
from lab_platform.control_plane import ControlPlaneConfig, ControlPlaneRuntime, create_app
from lab_platform.models import ApiTokenScope

OWNER_PASSWORD = "correct horse battery staple"
MEMBER_PASSWORD = "member password is long enough"
REPLACEMENT_PASSWORD = "replacement password is long enough"


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


async def _bootstrap_owner(runtime: ControlPlaneRuntime) -> None:
    await runtime.identity_administration.bootstrap_admin(
        organisation_slug=runtime.config.identity.default_organisation_slug,
        organisation_name=runtime.config.identity.default_organisation_name,
        username="owner",
        display_name="Lab Owner",
        password=OWNER_PASSWORD,
    )


async def _issue_legacy_token(runtime: ControlPlaneRuntime) -> str:
    issued = await runtime.token_service.issue(
        name="legacy administrator",
        owner="legacy",
        scopes=set(ApiTokenScope),
    )
    return issued.plaintext


def _login(client: TestClient, username: str, password: str) -> dict[str, object]:
    response = client.post(
        "/api/v1/auth/login",
        json={"username": username, "password": password},
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert isinstance(payload, dict)
    return payload


def _bearer(token: object) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _owner_client(
    tmp_path: Path,
) -> tuple[ControlPlaneRuntime, TestClient, dict[str, str], dict[str, object]]:
    runtime = _runtime(tmp_path)
    client = TestClient(create_app(runtime))
    client.__enter__()
    assert client.portal is not None
    client.portal.call(_bootstrap_owner, runtime)
    login = _login(client, "owner", OWNER_PASSWORD)
    return runtime, client, _bearer(login["access_token"]), login


def test_owner_user_team_and_role_crud_with_team_inheritance(tmp_path: Path) -> None:
    _runtime_value, client, owner_headers, owner_login = _owner_client(tmp_path)
    try:
        organisation = owner_login["organisation"]
        assert isinstance(organisation, dict)
        organisation_id = organisation["id"]

        shown_organisation = client.get("/api/v1/organisation", headers=owner_headers)
        assert shown_organisation.status_code == 200
        assert shown_organisation.json() == organisation
        updated_organisation = client.patch(
            "/api/v1/organisation",
            headers=owner_headers,
            json={"name": "Updated Lab Organisation"},
        )
        assert updated_organisation.status_code == 200
        assert updated_organisation.json()["name"] == "Updated Lab Organisation"

        roles = client.get("/api/v1/roles", headers=owner_headers)
        assert roles.status_code == 200
        assert "OPERATOR" in roles.json()["items"]
        operator_permissions = client.get(
            "/api/v1/roles/OPERATOR/permissions",
            headers=owner_headers,
        )
        assert operator_permissions.status_code == 200
        assert operator_permissions.json()["role"] == "OPERATOR"
        assert "benches:flash" in operator_permissions.json()["permissions"]

        created_user = client.post(
            "/api/v1/users",
            headers=owner_headers,
            json={
                "username": "member",
                "display_name": "Team Member",
                "email": "member@example.test",
                "password": MEMBER_PASSWORD,
                "organisation_role": "MEMBER",
            },
        )
        assert created_user.status_code == 201, created_user.text
        user = created_user.json()
        assert user["username"] == "member"
        assert user["status"] == "ACTIVE"
        assert "password" not in str(user).casefold()
        user_id = user["id"]

        duplicate = client.post(
            "/api/v1/users",
            headers=owner_headers,
            json={
                "username": "MEMBER",
                "display_name": "Duplicate",
                "password": MEMBER_PASSWORD,
            },
        )
        assert duplicate.status_code == 409
        assert duplicate.json()["error"]["code"] == "USERNAME_ALREADY_EXISTS"

        users = client.get("/api/v1/users", headers=owner_headers)
        assert users.status_code == 200
        assert {item["username"] for item in users.json()["items"]} == {
            "owner",
            "member",
        }
        fetched_user = client.get(f"/api/v1/users/{user_id}", headers=owner_headers)
        assert fetched_user.status_code == 200
        assert fetched_user.json() == user
        updated_user = client.patch(
            f"/api/v1/users/{user_id}",
            headers=owner_headers,
            json={"display_name": "Updated Team Member", "email": None},
        )
        assert updated_user.status_code == 200, updated_user.text
        assert updated_user.json()["display_name"] == "Updated Team Member"
        assert updated_user.json()["email"] is None

        disabled = client.post(
            f"/api/v1/users/{user_id}/disable",
            headers=owner_headers,
        )
        assert disabled.status_code == 200
        assert disabled.json()["status"] == "DISABLED"
        denied_login = client.post(
            "/api/v1/auth/login",
            json={"username": "member", "password": MEMBER_PASSWORD},
        )
        assert denied_login.status_code == 403
        assert denied_login.json()["error"]["code"] == "USER_DISABLED"
        enabled = client.post(
            f"/api/v1/users/{user_id}/enable",
            headers=owner_headers,
        )
        assert enabled.status_code == 200
        assert enabled.json()["status"] == "ACTIVE"
        reset = client.post(
            f"/api/v1/users/{user_id}/reset-password",
            headers=owner_headers,
            json={"password": REPLACEMENT_PASSWORD},
        )
        assert reset.status_code == 204
        stale_password = client.post(
            "/api/v1/auth/login",
            json={"username": "member", "password": MEMBER_PASSWORD},
        )
        assert stale_password.status_code == 401

        created_team = client.post(
            "/api/v1/teams",
            headers=owner_headers,
            json={
                "slug": "validation",
                "name": "Validation",
                "description": "Hardware validation team",
            },
        )
        assert created_team.status_code == 201, created_team.text
        team = created_team.json()
        team_id = team["id"]
        duplicate_team = client.post(
            "/api/v1/teams",
            headers=owner_headers,
            json={"slug": "validation", "name": "Duplicate Validation"},
        )
        assert duplicate_team.status_code == 409
        assert duplicate_team.json()["error"]["code"] == "TEAM_ALREADY_EXISTS"
        assert client.get(f"/api/v1/teams/{team_id}", headers=owner_headers).json() == team
        updated_team = client.patch(
            f"/api/v1/teams/{team_id}",
            headers=owner_headers,
            json={"slug": "validation-team", "description": None},
        )
        assert updated_team.status_code == 200, updated_team.text
        assert updated_team.json()["slug"] == "validation-team"
        assert updated_team.json()["description"] is None
        teams = client.get("/api/v1/teams", headers=owner_headers)
        assert teams.status_code == 200
        assert [item["id"] for item in teams.json()["items"]] == [team_id]

        membership = client.post(
            f"/api/v1/teams/{team_id}/members",
            headers=owner_headers,
            json={"user_id": user_id, "role": "MEMBER"},
        )
        assert membership.status_code == 201, membership.text
        assert membership.json()["team_id"] == team_id
        assert membership.json()["user_id"] == user_id
        user_teams = client.get(
            f"/api/v1/users/{user_id}/teams",
            headers=owner_headers,
        )
        assert user_teams.status_code == 200, user_teams.text
        assert user_teams.json()["items"] == [
            {"team": updated_team.json(), "membership": membership.json()}
        ]
        team_members = client.get(
            f"/api/v1/teams/{team_id}/members",
            headers=owner_headers,
        )
        assert team_members.status_code == 200, team_members.text
        assert team_members.json()["items"][0]["membership"] == membership.json()
        assert team_members.json()["items"][0]["user"]["id"] == user_id

        assigned = client.post(
            "/api/v1/role-assignments",
            headers=owner_headers,
            json={
                "subject_type": "TEAM",
                "subject_id": team_id,
                "role": "ORGANISATION_ADMIN",
                "resource_type": "ORGANISATION",
                "resource_id": organisation_id,
            },
        )
        assert assigned.status_code == 201, assigned.text
        assignment = assigned.json()
        assert assignment["subject_id"] == team_id
        duplicate_role = client.post(
            "/api/v1/role-assignments",
            headers=owner_headers,
            json={
                "subject_type": "TEAM",
                "subject_id": team_id,
                "role": "ORGANISATION_ADMIN",
                "resource_type": "ORGANISATION",
                "resource_id": organisation_id,
            },
        )
        assert duplicate_role.status_code == 409
        assert duplicate_role.json()["error"]["code"] == "ROLE_ASSIGNMENT_CONFLICT"

        role_listing = client.get("/api/v1/role-assignments", headers=owner_headers)
        assert role_listing.status_code == 200
        assert [item["id"] for item in role_listing.json()["items"]] == [assignment["id"]]
        filtered_roles = client.get(
            "/api/v1/role-assignments",
            headers=owner_headers,
            params={
                "subject_type": "TEAM",
                "subject_id": team_id,
                "resource_type": "ORGANISATION",
                "resource_id": organisation_id,
            },
        )
        assert filtered_roles.status_code == 200, filtered_roles.text
        assert filtered_roles.json()["items"] == [assignment]

        member_login = _login(client, "member", REPLACEMENT_PASSWORD)
        member_headers = _bearer(member_login["access_token"])
        sessions = client.get(
            f"/api/v1/users/{user_id}/sessions",
            headers=owner_headers,
        )
        assert sessions.status_code == 200, sessions.text
        assert sessions.json()["items"][0]["active"] is True
        assert "secret_hash" not in sessions.text
        effective = client.get(
            "/api/v1/permissions/effective",
            headers=member_headers,
            params={
                "resource_type": "BENCH",
                "resource_id": "home-lab/esp32-01",
                "permission": "benches:flash",
            },
        )
        assert effective.status_code == 200, effective.text
        decision = effective.json()
        assert decision["allowed"] is True
        assert "ORGANISATION_ADMIN" in decision["roles"]
        assert "benches:flash" in decision["permissions"]
        assert assignment["id"] in decision["granting_assignment_ids"]

        inspected = client.get(
            "/api/v1/permissions/effective",
            headers=owner_headers,
            params={
                "resource_type": "BENCH",
                "resource_id": "home-lab/esp32-01",
                "permission": "benches:flash",
                "subject_type": "USER",
                "subject_id": user_id,
            },
        )
        assert inspected.status_code == 200, inspected.text
        assert inspected.json() == decision

        revoked_role = client.delete(
            f"/api/v1/role-assignments/{assignment['id']}",
            headers=owner_headers,
        )
        assert revoked_role.status_code == 204
        removed_member = client.delete(
            f"/api/v1/teams/{team_id}/members/{user_id}",
            headers=owner_headers,
        )
        assert removed_member.status_code == 204
        removed_team = client.delete(f"/api/v1/teams/{team_id}", headers=owner_headers)
        assert removed_team.status_code == 204
        missing_team = client.get(f"/api/v1/teams/{team_id}", headers=owner_headers)
        assert missing_team.status_code == 404
        assert missing_team.json()["error"]["code"] == "TEAM_NOT_FOUND"
    finally:
        client.__exit__(None, None, None)


def test_oidc_user_provisioning_requires_no_local_password(tmp_path: Path) -> None:
    runtime, client, owner_headers, _owner_login = _owner_client(tmp_path)
    try:
        missing_local_password = client.post(
            "/api/v1/users",
            headers=owner_headers,
            json={"username": "local", "display_name": "Local User"},
        )
        assert missing_local_password.status_code == 422
        assert missing_local_password.json()["error"]["code"] == "VALIDATION_ERROR"

        oidc_with_password = client.post(
            "/api/v1/users",
            headers=owner_headers,
            json={
                "username": "oidc-invalid",
                "display_name": "Invalid OIDC User",
                "authentication_source": "OIDC",
                "password": MEMBER_PASSWORD,
            },
        )
        assert oidc_with_password.status_code == 422
        assert oidc_with_password.json()["error"]["code"] == "VALIDATION_ERROR"

        created = client.post(
            "/api/v1/users",
            headers=owner_headers,
            json={
                "username": "oidc-user",
                "display_name": "OIDC User",
                "email": "oidc-user@example.test",
                "authentication_source": "OIDC",
                "organisation_role": "VIEWER",
            },
        )
        assert created.status_code == 201, created.text
        user = created.json()
        assert user["authentication_source"] == "OIDC"
        assert "password" not in user
        assert client.portal is not None
        assert (
            client.portal.call(
                runtime.identity_repository.get_password_credential,
                UUID(user["id"]),
            )
            is None
        )
        membership = client.portal.call(
            runtime.identity_repository.get_organisation_membership,
            UUID(user["organisation_id"]),
            UUID(user["id"]),
        )
        assert membership is not None
        assert membership.role.value == "VIEWER"

        local_login = client.post(
            "/api/v1/auth/login",
            json={"username": "oidc-user", "password": MEMBER_PASSWORD},
        )
        assert local_login.status_code == 401
        assert local_login.json()["error"]["code"] == "INVALID_CREDENTIALS"

        reset = client.post(
            f"/api/v1/users/{user['id']}/reset-password",
            headers=owner_headers,
            json={"password": REPLACEMENT_PASSWORD},
        )
        assert reset.status_code == 403
        assert reset.json()["error"]["code"] == "ROLE_NOT_ALLOWED"
    finally:
        client.__exit__(None, None, None)


def test_service_account_credential_secret_is_one_time_and_revocable(
    tmp_path: Path,
) -> None:
    _runtime_value, client, owner_headers, owner_login = _owner_client(tmp_path)
    try:
        organisation = owner_login["organisation"]
        assert isinstance(organisation, dict)
        account_response = client.post(
            "/api/v1/service-accounts",
            headers=owner_headers,
            json={"name": "github-ci", "description": "Hardware CI"},
        )
        assert account_response.status_code == 201, account_response.text
        account = account_response.json()
        account_id = account["id"]
        assert account["status"] == "ACTIVE"

        accounts = client.get("/api/v1/service-accounts", headers=owner_headers)
        assert accounts.status_code == 200
        assert [item["id"] for item in accounts.json()["items"]] == [account_id]
        fetched = client.get(
            f"/api/v1/service-accounts/{account_id}",
            headers=owner_headers,
        )
        assert fetched.status_code == 200
        assert fetched.json() == account

        patched = client.patch(
            f"/api/v1/service-accounts/{account_id}",
            headers=owner_headers,
            json={"status": "DISABLED", "description": "Paused hardware CI"},
        )
        assert patched.status_code == 200, patched.text
        assert patched.json()["status"] == "DISABLED"
        assert patched.json()["description"] == "Paused hardware CI"
        reenabled = client.patch(
            f"/api/v1/service-accounts/{account_id}",
            headers=owner_headers,
            json={"status": "ACTIVE"},
        )
        assert reenabled.status_code == 200
        assert reenabled.json()["status"] == "ACTIVE"

        role = client.post(
            "/api/v1/role-assignments",
            headers=owner_headers,
            json={
                "subject_type": "SERVICE_ACCOUNT",
                "subject_id": account_id,
                "role": "VIEWER",
                "resource_type": "ORGANISATION",
                "resource_id": organisation["id"],
            },
        )
        assert role.status_code == 201, role.text

        issued = client.post(
            f"/api/v1/service-accounts/{account_id}/credentials",
            headers=owner_headers,
            json={
                "name": "main",
                "permission_restrictions": ["benches:read"],
            },
        )
        assert issued.status_code == 201, issued.text
        issued_payload = issued.json()
        assert set(issued_payload) == {"credential", "token"}
        credential = issued_payload["credential"]
        token = issued_payload["token"]
        assert isinstance(credential, dict)
        assert isinstance(token, str) and token.startswith("lp_")
        assert "secret_hash" not in str(issued_payload)

        credentials = client.get(
            f"/api/v1/service-accounts/{account_id}/credentials",
            headers=owner_headers,
        )
        assert credentials.status_code == 200
        listed_payload = credentials.json()
        assert listed_payload["items"] == [credential]
        assert token not in str(listed_payload)
        assert "token" not in str(listed_payload).casefold()
        assert "secret_hash" not in str(listed_payload)

        credential_headers = _bearer(token)
        me = client.get("/api/v1/auth/me", headers=credential_headers)
        assert me.status_code == 200
        assert me.json()["principal"]["id"] == account_id

        revoked = client.delete(
            f"/api/v1/credentials/{credential['id']}",
            headers=owner_headers,
        )
        assert revoked.status_code == 204
        rejected = client.get("/api/v1/auth/me", headers=credential_headers)
        assert rejected.status_code == 401
        assert rejected.json()["error"]["code"] == "TOKEN_REVOKED"

        replacement = client.post(
            f"/api/v1/service-accounts/{account_id}/credentials",
            headers=owner_headers,
            json={"name": "replacement", "permission_restrictions": ["benches:read"]},
        )
        assert replacement.status_code == 201, replacement.text
        replacement_payload = replacement.json()
        replacement_token = replacement_payload["token"]
        replacement_headers = _bearer(replacement_token)
        assert client.get("/api/v1/auth/me", headers=replacement_headers).status_code == 200

        deleted = client.delete(
            f"/api/v1/service-accounts/{account_id}",
            headers=owner_headers,
        )
        assert deleted.status_code == 204
        revoked_account = client.get(
            f"/api/v1/service-accounts/{account_id}",
            headers=owner_headers,
        )
        assert revoked_account.status_code == 200
        assert revoked_account.json()["status"] == "REVOKED"
        rejected_replacement = client.get("/api/v1/auth/me", headers=replacement_headers)
        assert rejected_replacement.status_code == 401
        assert rejected_replacement.json()["error"]["code"] == "TOKEN_REVOKED"

        resurrection = client.patch(
            f"/api/v1/service-accounts/{account_id}",
            headers=owner_headers,
            json={"status": "ACTIVE"},
        )
        assert resurrection.status_code == 403
        assert resurrection.json()["error"]["code"] == "ROLE_NOT_ALLOWED"
        description_change = client.patch(
            f"/api/v1/service-accounts/{account_id}",
            headers=owner_headers,
            json={"description": "should remain deleted"},
        )
        assert description_change.status_code == 403
        assert description_change.json()["error"]["code"] == "ROLE_NOT_ALLOWED"

        after_delete_credentials = client.get(
            f"/api/v1/service-accounts/{account_id}/credentials",
            headers=owner_headers,
        )
        assert after_delete_credentials.status_code == 200
        replacement_record = next(
            item
            for item in after_delete_credentials.json()["items"]
            if item["id"] == replacement_payload["credential"]["id"]
        )
        assert replacement_record["revoked_at"] is not None
    finally:
        client.__exit__(None, None, None)


def test_viewer_administration_denial_and_audit_api(tmp_path: Path) -> None:
    runtime, client, owner_headers, _owner_login = _owner_client(tmp_path)
    try:
        assert client.portal is not None
        legacy_token = client.portal.call(_issue_legacy_token, runtime)
        rejected_legacy = client.get(
            "/api/v1/users",
            headers=_bearer(legacy_token),
        )
        assert rejected_legacy.status_code == 401
        assert rejected_legacy.json()["error"]["code"] == "AUTHENTICATION_FAILED"

        viewer_response = client.post(
            "/api/v1/users",
            headers=owner_headers,
            json={
                "username": "viewer",
                "display_name": "Read Only",
                "password": MEMBER_PASSWORD,
                "organisation_role": "VIEWER",
            },
        )
        assert viewer_response.status_code == 201
        viewer = viewer_response.json()
        viewer_login = _login(client, "viewer", MEMBER_PASSWORD)
        viewer_headers = _bearer(viewer_login["access_token"])

        denied = client.post(
            "/api/v1/teams",
            headers=viewer_headers,
            json={"slug": "forbidden", "name": "Forbidden"},
        )
        assert denied.status_code == 403
        error = denied.json()["error"]
        assert error["code"] == "PERMISSION_DENIED"
        assert error["details"]["required_permission"] == "teams:manage"

        viewer_audit = client.get("/api/v1/audit-events", headers=viewer_headers)
        assert viewer_audit.status_code == 403
        assert viewer_audit.json()["error"]["details"]["required_permission"] == "audit:read"

        audit = client.get(
            "/api/v1/audit-events",
            headers=owner_headers,
            params={"action": "PERMISSION_DENIED", "outcome": "DENIED", "limit": 100},
        )
        assert audit.status_code == 200, audit.text
        events = audit.json()["items"]
        team_denial = next(
            event
            for event in events
            if event["actor_id"] == viewer["id"]
            and event["metadata"] == {"required_permission": "teams:manage"}
        )
        assert team_denial["action"] == "PERMISSION_DENIED"
        assert team_denial["outcome"] == "DENIED"
        assert team_denial["resource_type"] == "ORGANISATION"

        correlated = client.get(
            "/api/v1/audit-events",
            headers=owner_headers,
            params={
                "actor": "read only",
                "actor_id": viewer["id"],
                "resource_type": "ORGANISATION",
                "resource_id": team_denial["resource_id"],
            },
        )
        assert correlated.status_code == 200, correlated.text
        assert any(item["id"] == team_denial["id"] for item in correlated.json()["items"])

        first_page = client.get(
            "/api/v1/audit-events",
            headers=owner_headers,
            params={"limit": 1},
        )
        assert first_page.status_code == 200, first_page.text
        assert first_page.json()["has_more"] is True
        assert first_page.json()["next_cursor"] == first_page.json()["items"][0]["id"]
        second_page = client.get(
            "/api/v1/audit-events",
            headers=owner_headers,
            params={"limit": 1, "cursor": first_page.json()["next_cursor"]},
        )
        assert second_page.status_code == 200, second_page.text
        assert second_page.json()["items"][0]["id"] != first_page.json()["items"][0]["id"]

        fetched = client.get(
            f"/api/v1/audit-events/{team_denial['id']}",
            headers=owner_headers,
        )
        assert fetched.status_code == 200
        assert fetched.json() == team_denial
        assert "password" not in str(events).casefold()
        assert "authorization" not in str(events).casefold()
    finally:
        client.__exit__(None, None, None)
