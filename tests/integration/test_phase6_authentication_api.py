from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

from fastapi.testclient import TestClient
from lab_platform.control_plane import ControlPlaneConfig, ControlPlaneRuntime, create_app
from lab_platform.core.identity import IssuedApiCredential
from lab_platform.models import (
    ApiTokenScope,
    AuditEvent,
    CiSession,
    Organisation,
    OrganisationMembership,
    OrganisationRole,
    PasswordCredential,
    PrincipalType,
    ResourceType,
    RoleAssignment,
    RoleName,
    RoleSubjectType,
    ServiceAccount,
    User,
)

PASSWORD = "correct horse battery staple"


async def _get_ci_session(runtime: ControlPlaneRuntime, session_id: UUID) -> CiSession:
    return await runtime.ci.get(session_id, synchronize=False)


async def _login_audit_events(
    runtime: ControlPlaneRuntime,
    organisation_id: UUID,
    action: str = "USER_LOGIN_SUCCEEDED",
) -> list[AuditEvent]:
    return await runtime.identity_repository.list_audit_events(
        organisation_id,
        action=action,
    )


def _runtime(
    tmp_path: Path,
    *,
    legacy_token_compatibility_enabled: bool = True,
) -> ControlPlaneRuntime:
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
                "authorisation": {
                    "legacy_token_compatibility_enabled": (legacy_token_compatibility_enabled)
                },
                "development": {
                    "enabled": True,
                    "allow_insecure_agent_transport": True,
                },
            }
        )
    )


async def _seed_user(
    runtime: ControlPlaneRuntime,
    username: str,
    display_name: str,
    role: OrganisationRole,
) -> User:
    organisation = await _default_organisation(runtime)
    now = datetime.now(UTC)
    user = User(
        organisation_id=organisation.id,
        username=username,
        display_name=display_name,
        created_at=now,
        updated_at=now,
    )
    password = PasswordCredential(
        user_id=user.id,
        password_hash=runtime.identity.hash_password(PASSWORD),
        created_at=now,
        updated_at=now,
    )
    membership = OrganisationMembership(
        organisation_id=organisation.id,
        user_id=user.id,
        role=role,
        created_at=now,
    )
    await runtime.identity_repository.create_user_with_password(user, password, membership)
    return user


async def _seed_service_credential(
    runtime: ControlPlaneRuntime,
    role: RoleName,
    permission_restrictions: set[str] | None = None,
) -> tuple[ServiceAccount, IssuedApiCredential]:
    organisation = await _default_organisation(runtime)
    now = datetime.now(UTC)
    account = ServiceAccount(
        organisation_id=organisation.id,
        name="github-ci",
        description="Phase 6 API integration test",
        created_at=now,
        updated_at=now,
    )
    await runtime.identity_repository.create_service_account(account)
    await runtime.identity_repository.create_role_assignment(
        RoleAssignment(
            organisation_id=organisation.id,
            subject_type=RoleSubjectType.SERVICE_ACCOUNT,
            subject_id=account.id,
            role=role,
            resource_type=ResourceType.ORGANISATION,
            resource_id=str(organisation.id),
            created_by=account.id,
            created_at=now,
        )
    )
    issued = await runtime.identity.issue_api_credential(
        service_account=account,
        name="test credential",
        permission_restrictions=permission_restrictions,
    )
    return account, issued


async def _default_organisation(runtime: ControlPlaneRuntime) -> Organisation:
    organisation = await runtime.identity_repository.get_organisation_by_slug(
        runtime.config.identity.default_organisation_slug
    )
    assert organisation is not None
    return organisation


def _login(client: TestClient, username: str) -> dict[str, object]:
    response = client.post(
        "/api/v1/auth/login",
        json={"username": username, "password": PASSWORD},
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert isinstance(payload, dict)
    return payload


def _bearer(token: object) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def test_local_login_me_and_owner_authorisation(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    with TestClient(create_app(runtime)) as client:
        assert client.portal is not None
        user = client.portal.call(
            _seed_user,
            runtime,
            "alice",
            "Alice Example",
            OrganisationRole.OWNER,
        )

        invalid = client.post(
            "/api/v1/auth/login",
            json={"username": "alice", "password": "wrong password"},
        )
        assert invalid.status_code == 401
        assert invalid.json()["error"]["code"] == "INVALID_CREDENTIALS"

        payload = _login(client, "ALICE")
        assert set(payload) == {
            "access_token",
            "token_type",
            "expires_at",
            "principal",
            "organisation",
            "session",
        }
        assert payload["token_type"] == "bearer"
        assert str(payload["access_token"]).startswith("lps_")
        assert "secret_hash" not in str(payload)
        principal = payload["principal"]
        assert isinstance(principal, dict)
        assert principal["id"] == str(user.id)

        headers = _bearer(payload["access_token"])
        me = client.get("/api/v1/auth/me", headers=headers)
        assert me.status_code == 200
        assert me.json()["principal"]["display_name"] == "Alice Example"
        sessions = client.get("/api/v1/auth/sessions", headers=headers)
        assert sessions.status_code == 200
        assert len(sessions.json()["items"]) == 1
        assert "secret_hash" not in str(sessions.json())

        agents = client.get("/api/v1/agents", headers=headers)
        assert agents.status_code == 200, agents.text
        assert agents.json() == {"items": []}

        anonymous_legacy_bootstrap = client.post(
            "/api/v1/tokens",
            json={
                "name": "unsafe anonymous bootstrap",
                "owner": "anonymous",
                "scopes": [scope.value for scope in ApiTokenScope],
            },
        )
        assert anonymous_legacy_bootstrap.status_code == 401


def test_login_audit_redacts_secret_shaped_user_agent(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    with TestClient(create_app(runtime)) as client:
        assert client.portal is not None
        user = client.portal.call(
            _seed_user,
            runtime,
            "alice",
            "Alice Example",
            OrganisationRole.OWNER,
        )
        injected_token = f"lps_{uuid4().hex}_{'x' * 43}"
        injected_client_secret = "opaque-client-secret-from-header"
        injected_api_key = "opaque-api-key-from-header"
        injected_passphrase = "passphrase-from-header"
        injected_password = "swordfish-from-header"
        injected_private_key = "-----BEGIN PGP PRIVATE KEY BLOCK----- opaque-private-key-payload"
        response = client.post(
            "/api/v1/auth/login",
            headers={
                "User-Agent": (
                    f"security-test Bearer {injected_token}; "
                    f'"client_secret": "{injected_client_secret}"; '
                    f"x-api-key={injected_api_key}; "
                    f"passphrase: {injected_passphrase}; "
                    f"password: {injected_password}; {injected_private_key}"
                )
            },
            json={"username": "alice", "password": PASSWORD},
        )
        assert response.status_code == 200, response.text

        events = client.portal.call(
            _login_audit_events,
            runtime,
            user.organisation_id,
        )
        assert len(events) == 1
        serialized = events[0].model_dump_json()
        assert injected_token not in serialized
        assert injected_client_secret not in serialized
        assert injected_api_key not in serialized
        assert injected_passphrase not in serialized
        assert injected_password not in serialized
        assert "opaque-private-key-payload" not in serialized
        assert events[0].user_agent == "[REDACTED]"


def test_login_audit_redacts_and_bounds_oversized_user_agent(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    with TestClient(create_app(runtime)) as client:
        assert client.portal is not None
        user = client.portal.call(
            _seed_user,
            runtime,
            "alice",
            "Alice Example",
            OrganisationRole.OWNER,
        )
        oidc_token = f"eyJ{'a' * 16}.{'b' * 16}.{'c' * 16}"
        oversized_user_agent = f"{'u' * 994} {oidc_token}{'z' * 100}"

        invalid = client.post(
            "/api/v1/auth/login",
            headers={"User-Agent": oversized_user_agent},
            json={"username": "alice", "password": "wrong password"},
        )
        assert invalid.status_code == 401, invalid.text

        valid = client.post(
            "/api/v1/auth/login",
            headers={"User-Agent": oversized_user_agent},
            json={"username": "alice", "password": PASSWORD},
        )
        assert valid.status_code == 200, valid.text

        events = [
            *client.portal.call(
                _login_audit_events,
                runtime,
                user.organisation_id,
                "USER_LOGIN_FAILED",
            ),
            *client.portal.call(
                _login_audit_events,
                runtime,
                user.organisation_id,
                "USER_LOGIN_SUCCEEDED",
            ),
        ]
        assert len(events) == 2
        for event in events:
            assert event.user_agent is not None
            assert len(event.user_agent) == 1000
            assert event.user_agent.endswith("…[REDACTED]")
            serialized = event.model_dump_json()
            assert oidc_token not in serialized
            assert "eyJ" not in serialized


def test_session_refresh_revoke_and_logout(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    with TestClient(create_app(runtime)) as client:
        assert client.portal is not None
        client.portal.call(
            _seed_user,
            runtime,
            "alice",
            "Alice Example",
            OrganisationRole.MEMBER,
        )
        first = _login(client, "alice")
        second = _login(client, "alice")
        first_headers = _bearer(first["access_token"])

        listing = client.get("/api/v1/auth/sessions", headers=first_headers)
        assert listing.status_code == 200
        assert len(listing.json()["items"]) == 2

        second_session = second["session"]
        assert isinstance(second_session, dict)
        revoked = client.delete(
            f"/api/v1/auth/sessions/{second_session['id']}",
            headers=first_headers,
        )
        assert revoked.status_code == 204
        rejected = client.get("/api/v1/auth/me", headers=_bearer(second["access_token"]))
        assert rejected.status_code == 401
        assert rejected.json()["error"]["code"] == "SESSION_REVOKED"

        refreshed = client.post("/api/v1/auth/refresh", headers=first_headers)
        assert refreshed.status_code == 200, refreshed.text
        refreshed_token = refreshed.json()["access_token"]
        assert refreshed_token != first["access_token"]
        stale = client.get("/api/v1/auth/me", headers=first_headers)
        assert stale.status_code == 401
        fresh_headers = _bearer(refreshed_token)
        assert client.get("/api/v1/auth/me", headers=fresh_headers).status_code == 200

        logged_out = client.post("/api/v1/auth/logout", headers=fresh_headers)
        assert logged_out.status_code == 204
        after_logout = client.get("/api/v1/auth/me", headers=fresh_headers)
        assert after_logout.status_code == 401
        assert after_logout.json()["error"]["code"] == "SESSION_REVOKED"


def test_member_denial_is_audited_and_credential_narrowing_is_enforced(
    tmp_path: Path,
) -> None:
    runtime = _runtime(tmp_path)
    with TestClient(create_app(runtime)) as client:
        assert client.portal is not None
        member = client.portal.call(
            _seed_user,
            runtime,
            "bob",
            "Bob Member",
            OrganisationRole.MEMBER,
        )
        member_login = _login(client, "bob")
        denied = client.get("/api/v1/agents", headers=_bearer(member_login["access_token"]))
        assert denied.status_code == 403
        assert denied.json()["error"]["details"]["required_permission"] == "agents:read"
        metrics_denied = client.get(
            "/metrics",
            headers=_bearer(member_login["access_token"]),
        )
        assert metrics_denied.status_code == 403
        assert metrics_denied.json()["error"]["details"]["required_permission"] == "agents:read"

        organisation = client.portal.call(_default_organisation, runtime)
        audit_events = client.portal.call(
            runtime.identity_repository.list_audit_events,
            organisation.id,
        )
        assert any(
            event.action == "PERMISSION_DENIED"
            and event.actor_id == member.id
            and event.metadata == {"required_permission": "agents:read"}
            for event in audit_events
        )

        _account, issued = client.portal.call(
            _seed_service_credential,
            runtime,
            RoleName.AUDITOR,
            {"benches:read"},
        )
        credential_headers = _bearer(issued.token)
        me = client.get("/api/v1/auth/me", headers=credential_headers)
        assert me.status_code == 200
        assert me.json()["principal"]["type"] == "SERVICE_ACCOUNT"
        narrowed = client.get("/api/v1/agents", headers=credential_headers)
        assert narrowed.status_code == 403
        sessions = client.get("/api/v1/auth/sessions", headers=credential_headers)
        assert sessions.status_code == 403
        assert sessions.json()["error"]["details"] == {
            "required_permission": "users:read",
            "resource_type": "SESSION",
            "resource_id": None,
        }
        target_session_id = uuid4()
        revoke_session = client.delete(
            f"/api/v1/auth/sessions/{target_session_id}",
            headers=credential_headers,
        )
        assert revoke_session.status_code == 403
        assert revoke_session.json()["error"]["details"] == {
            "required_permission": "users:read",
            "resource_type": "SESSION",
            "resource_id": str(target_session_id),
        }

        service_denials = [
            event
            for event in client.portal.call(
                runtime.identity_repository.list_audit_events,
                organisation.id,
            )
            if event.action == "PERMISSION_DENIED"
            and event.actor_id == issued.credential.principal_id
            and event.resource_type == "SESSION"
        ]
        assert {event.resource_id for event in service_denials} == {None, str(target_session_id)}
        assert all(
            event.resource_type == "SESSION"
            and event.metadata == {"required_permission": "users:read"}
            for event in service_denials
        )


def test_service_account_runs_ci_with_identity_and_legacy_flag_is_honoured(
    tmp_path: Path,
) -> None:
    runtime = _runtime(tmp_path)
    with TestClient(create_app(runtime)) as client:
        assert client.portal is not None
        account, issued = client.portal.call(
            _seed_service_credential,
            runtime,
            RoleName.WORKFLOW_RUNNER,
        )
        headers = _bearer(issued.token)
        created = client.post(
            "/api/v1/ci/sessions",
            headers=headers,
            json={
                "provider": "github_actions",
                "external_run_id": "phase6-identity",
                "requested_by": "spoofed-request-owner",
                "bench_request": {},
            },
        )
        assert created.status_code == 201, created.text
        assert created.json()["requested_by"] == account.name
        session = client.portal.call(_get_ci_session, runtime, UUID(created.json()["id"]))
        assert session.organisation_id == account.organisation_id
        assert session.requested_by_principal_id == account.id
        assert session.requested_by_principal_type is PrincipalType.SERVICE_ACCOUNT
        assert client.get("/api/v1/agents", headers=headers).status_code == 403
        # Resource routes hide both missing and inaccessible Agent identities.
        assert (
            client.post(
                f"/api/v1/agents/{uuid4()}/drain",
                headers=headers,
                json={},
            ).status_code
            == 404
        )

    disabled_runtime = _runtime(
        tmp_path / "legacy-disabled",
        legacy_token_compatibility_enabled=False,
    )
    with TestClient(create_app(disabled_runtime)) as client:
        assert client.portal is not None
        client.portal.call(
            _seed_user,
            disabled_runtime,
            "owner",
            "Owner",
            OrganisationRole.OWNER,
        )
        owner = _login(client, "owner")
        response = client.post(
            "/api/v1/tokens",
            headers=_bearer(owner["access_token"]),
            json={
                "name": "disabled",
                "owner": "owner",
                "scopes": [scope.value for scope in ApiTokenScope],
            },
        )
        assert response.status_code == 403
        assert response.json()["error"]["code"] == "PERMISSION_DENIED"
