from __future__ import annotations

from datetime import UTC, datetime, timedelta
from functools import partial
from pathlib import Path
from unittest.mock import AsyncMock, patch
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from lab_platform.agent_protocol import (
    ActorAttributedControlPayload,
    CommandCancelPayload,
    DrainAgentPayload,
    InventoryRefreshRequestPayload,
    MessageType,
    ReconciliationRequestPayload,
)
from lab_platform.control_plane import ControlPlaneConfig, ControlPlaneRuntime, create_app
from lab_platform.control_plane_core.commands import CommandDeliveryReceipt
from lab_platform.models import (
    AuthenticationContext,
    AuthenticationSource,
    DistributedOperationStatus,
    GlobalBenchKind,
    GlobalBenchRecord,
    GlobalBenchStatus,
    HealthStatus,
    OrganisationMembership,
    OrganisationRole,
    Principal,
    PrincipalType,
    RemoteCommandStatus,
    RemoteCommandType,
    ResourceType,
    User,
)

AUTO_LOGIN_USER = "control-operator"


def _config(tmp_path: Path) -> ControlPlaneConfig:
    return ControlPlaneConfig.model_validate(
        {
            "control_plane": {
                "host": "127.0.0.1",
                "port": 8443,
                "public_url": "http://127.0.0.1:8443",
            },
            "database": {"url": f"sqlite:///{tmp_path / 'control-plane.db'}"},
            "agent_gateway": {"monitor_interval_seconds": 3_600},
            "artifacts": {"directory": tmp_path / "artifacts"},
            "authorisation": {"hide_unauthorised_resources": True},
            "development": {
                "enabled": True,
                "allow_insecure_agent_transport": True,
                "auto_login_user": AUTO_LOGIN_USER,
            },
        }
    )


async def _seed_operator_and_route(
    runtime: ControlPlaneRuntime,
) -> tuple[User, GlobalBenchRecord]:
    organisation = await runtime.identity_repository.get_organisation_by_slug(
        runtime.config.identity.default_organisation_slug
    )
    assert organisation is not None
    now = datetime.now(UTC)
    user = User(
        organisation_id=organisation.id,
        username=AUTO_LOGIN_USER,
        display_name="Control Operator",
        authentication_source=AuthenticationSource.OIDC,
        created_at=now,
        updated_at=now,
    )
    await runtime.identity_repository.create_user_with_membership(
        user,
        OrganisationMembership(
            organisation_id=organisation.id,
            user_id=user.id,
            role=OrganisationRole.OWNER,
            created_at=now,
        ),
    )
    token = await runtime.enrollment.issue_token(
        name="actor-context-agent",
        expires_at=now + timedelta(minutes=5),
        organisation_id=organisation.id,
        allow_internal_authorisation=True,
    )
    enrolled = await runtime.enrollment.enroll(
        plaintext_token=token.plaintext.get_secret_value(),
        request_id=uuid4(),
        agent_version="0.7.0-alpha",
        protocol_version="1.0",
    )
    active = await runtime.presence.register_authenticated_connection(
        enrolled.agent,
        connection_id=uuid4(),
        boot_id=uuid4(),
        protocol_version="1.0",
        agent_version="0.7.0-alpha",
        observed_at=now,
        observed_monotonic=1.0,
    )
    bench = GlobalBenchRecord(
        id=f"{active.agent.slug}/control-bench",
        organisation_id=organisation.id,
        agent_id=active.agent.id,
        agent_slug=active.agent.slug,
        local_bench_id="control-bench",
        name="Control bench",
        backend_id="simlab",
        kind=GlobalBenchKind.SIMULATED,
        status=GlobalBenchStatus.ONLINE,
        health=HealthStatus.HEALTHY,
        capabilities=frozenset({"probe"}),
        last_seen_at=now,
        created_at=now,
        updated_at=now,
    )
    await runtime.inventory_repository.reconcile_agent_snapshot(
        active.agent,
        (bench,),
        observed_at=now,
    )
    return user, bench


async def _seed_dispatched_operation(
    runtime: ControlPlaneRuntime,
    bench: GlobalBenchRecord,
) -> UUID:
    command, operation = await runtime.commands.create(
        agent_id=bench.agent_id,
        bench_id=bench.id,
        command_type=RemoteCommandType.PROBE,
        payload={},
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
        idempotency_key=f"control-actor:{uuid4()}",
        operation_type="PROBE",
        dispatch=False,
        allow_internal_authorisation=True,
    )
    assert operation is not None
    dispatched_at = max(datetime.now(UTC), command.created_at)
    persisted_command = await runtime.command_repository.update_command(
        command.model_copy(
            update={
                "status": RemoteCommandStatus.DISPATCHED,
                "dispatched_at": dispatched_at,
            }
        ),
        expected_statuses={RemoteCommandStatus.CREATED},
    )
    persisted_operation = await runtime.command_repository.update_operation(
        operation.model_copy(
            update={
                "status": DistributedOperationStatus.DISPATCHED,
                "dispatched_at": dispatched_at,
            }
        ),
        expected_statuses={DistributedOperationStatus.CREATED},
    )
    assert persisted_command is not None
    assert persisted_operation is not None
    return operation.id


def test_authenticated_agent_controls_carry_current_actor_and_snapshot(
    tmp_path: Path,
) -> None:
    runtime = ControlPlaneRuntime(_config(tmp_path))
    sent: list[tuple[MessageType, ActorAttributedControlPayload]] = []
    cancelled: list[CommandCancelPayload] = []

    async def send(
        _agent_id: UUID,
        message_type: MessageType,
        payload: object,
        **_kwargs: object,
    ) -> object:
        assert isinstance(payload, ActorAttributedControlPayload)
        sent.append((message_type, payload))
        return object()

    async def send_cancel(
        _agent_id: UUID,
        payload: CommandCancelPayload,
        *,
        correlation_id: UUID,
    ) -> CommandDeliveryReceipt:
        assert correlation_id == payload.command_id
        cancelled.append(payload)
        return CommandDeliveryReceipt(
            connection_id=UUID(int=71_001),
            sequence_number=1,
            dispatched_at=datetime.now(UTC),
        )

    with TestClient(create_app(runtime)) as client:
        assert client.portal is not None
        user, bench = client.portal.call(_seed_operator_and_route, runtime)
        operation_id = client.portal.call(_seed_dispatched_operation, runtime, bench)
        with (
            patch.object(runtime.hub, "send", new=AsyncMock(side_effect=send)),
            patch.object(runtime.hub, "send_cancel", new=AsyncMock(side_effect=send_cancel)),
            patch.object(runtime.hub, "is_connected", new=AsyncMock(return_value=True)),
        ):
            refresh = client.post(f"/api/v1/agents/{bench.agent_id}/actions/refresh-inventory")
            reconcile = client.post(f"/api/v1/operations/{operation_id}/reconcile")
            drain = client.post(
                f"/api/v1/agents/{bench.agent_id}/drain",
                json={"cancel_queued_work": False},
            )
            undrain = client.post(f"/api/v1/agents/{bench.agent_id}/undrain")
            cancel = client.post(
                f"/api/v1/operations/{operation_id}/cancel",
                json={"reason": "operator request"},
            )

            # Infrastructure maintenance and Phase 5 compatibility callers deliberately
            # retain the same messages without manufacturing a human/service principal.
            client.portal.call(
                partial(
                    runtime.refresh_inventory,
                    bench.agent_id,
                    allow_internal_authorisation=True,
                )
            )
            client.portal.call(
                partial(
                    runtime.request_reconciliation,
                    bench.agent_id,
                    allow_internal_authorisation=True,
                )
            )
            client.portal.call(
                partial(
                    runtime.drain_agent,
                    bench.agent_id,
                    allow_internal_authorisation=True,
                )
            )
            client.portal.call(
                partial(
                    runtime.undrain_agent,
                    bench.agent_id,
                    allow_internal_authorisation=True,
                )
            )
            operation = client.portal.call(
                partial(
                    runtime.operation_records.get,
                    operation_id,
                    organisation_id=user.organisation_id,
                )
            )
            assert operation is not None
            client.portal.call(
                partial(
                    runtime.commands.request_cancel,
                    operation.remote_command_id,
                    reason="automatic maintenance",
                    allow_internal_authorisation=True,
                )
            )

        assert refresh.status_code == 202, refresh.text
        assert reconcile.status_code == 202, reconcile.text
        assert drain.status_code == 200, drain.text
        assert undrain.status_code == 200, undrain.text
        assert cancel.status_code == 202, cancel.text

        assert [message_type for message_type, _payload in sent] == [
            MessageType.INVENTORY_REFRESH_REQUEST,
            MessageType.RECONCILIATION_REQUEST,
            MessageType.DRAIN_AGENT,
            MessageType.DRAIN_AGENT,
        ] * 2
        assert isinstance(sent[0][1], InventoryRefreshRequestPayload)
        assert isinstance(sent[1][1], ReconciliationRequestPayload)
        assert isinstance(sent[2][1], DrainAgentPayload) and sent[2][1].drain
        assert isinstance(sent[3][1], DrainAgentPayload) and not sent[3][1].drain
        authenticated_payloads = [payload for _message_type, payload in sent[:4]] + cancelled[:1]
        assert len(cancelled) == 2
        for payload in authenticated_payloads:
            actor = payload.actor_context
            assert actor is not None
            assert actor.principal_id == user.id
            assert actor.organisation_id == user.organisation_id
            assert payload.authorisation_snapshot_id is not None
            assert actor.authorisation_snapshot_id == payload.authorisation_snapshot_id
            snapshot = client.portal.call(
                runtime.identity_repository.get_authorisation_snapshot,
                user.organisation_id,
                payload.authorisation_snapshot_id,
            )
            assert snapshot is not None
            assert snapshot.principal_id == user.id
        for payload in [payload for _message_type, payload in sent[4:]] + cancelled[1:]:
            assert payload.actor_context is None
            assert payload.authorisation_snapshot_id is None

        entries = client.portal.call(partial(runtime.timeline.list, bench.agent_id, limit=100))
        attributable = {
            entry.event_type: entry
            for entry in entries
            if entry.event_type
            in {
                "INVENTORY_REFRESH_REQUESTED",
                "RECONCILIATION_REQUESTED",
                "AGENT_DRAIN_REQUESTED",
                "AGENT_UNDRAINED",
                "REMOTE_COMMAND_CANCELLATION_REQUESTED",
            }
            and "actor_context" in entry.metadata
        }
        assert len(attributable) == 5
        assert all(
            entry.metadata["actor_context"]["principal_id"] == str(user.id)
            for entry in attributable.values()
        )


def test_inventory_refresh_intent_survives_enqueue_failure_and_restart(tmp_path: Path) -> None:
    config = _config(tmp_path)
    runtime = ControlPlaneRuntime(config)
    with TestClient(create_app(runtime), raise_server_exceptions=False) as client:
        assert client.portal is not None
        user, bench = client.portal.call(_seed_operator_and_route, runtime)
        with (
            patch.object(
                runtime.hub,
                "send",
                new=AsyncMock(side_effect=RuntimeError("simulated enqueue failure")),
            ),
            patch.object(runtime.hub, "is_connected", new=AsyncMock(return_value=True)),
        ):
            response = client.post(f"/api/v1/agents/{bench.agent_id}/actions/refresh-inventory")
        assert response.status_code == 500

    restarted = ControlPlaneRuntime(config)
    with TestClient(create_app(restarted)) as client:
        assert client.portal is not None
        entries = client.portal.call(
            partial(
                restarted.timeline.list,
                bench.agent_id,
                event_type="INVENTORY_REFRESH_REQUESTED",
                limit=10,
            )
        )
        assert len(entries) == 1
        entry = entries[0]
        assert entry.correlation_id is not None
        assert entry.deduplication_key == f"control-intent:inventory-refresh:{entry.correlation_id}"
        assert entry.metadata["actor_context"]["principal_id"] == str(user.id)
        snapshot_id = UUID(entry.metadata["authorisation_snapshot_id"])
        snapshot = client.portal.call(
            restarted.identity_repository.get_authorisation_snapshot,
            user.organisation_id,
            snapshot_id,
        )
        assert snapshot is not None
        assert snapshot.principal_id == user.id


def test_direct_authenticated_runtime_controls_mint_and_validate_exact_snapshots(
    tmp_path: Path,
) -> None:
    runtime = ControlPlaneRuntime(_config(tmp_path))
    sent: list[ActorAttributedControlPayload] = []

    async def send(
        _agent_id: UUID,
        _message_type: MessageType,
        payload: object,
        **_kwargs: object,
    ) -> object:
        assert isinstance(payload, ActorAttributedControlPayload)
        sent.append(payload)
        return object()

    with TestClient(create_app(runtime)) as client:
        assert client.portal is not None
        user, bench = client.portal.call(_seed_operator_and_route, runtime)
        context = AuthenticationContext(
            principal=Principal(
                id=user.id,
                type=PrincipalType.USER,
                organisation_id=user.organisation_id,
                display_name=user.display_name,
            )
        )
        with (
            patch.object(runtime.hub, "send", new=AsyncMock(side_effect=send)),
            patch.object(runtime.hub, "is_connected", new=AsyncMock(return_value=True)),
        ):
            client.portal.call(
                partial(
                    runtime.refresh_inventory,
                    bench.agent_id,
                    authentication_context=context,
                )
            )
            client.portal.call(
                partial(
                    runtime.request_reconciliation,
                    bench.agent_id,
                    authentication_context=context,
                )
            )
            client.portal.call(
                partial(
                    runtime.drain_agent,
                    bench.agent_id,
                    authentication_context=context,
                )
            )
            client.portal.call(
                partial(
                    runtime.undrain_agent,
                    bench.agent_id,
                    authentication_context=context,
                )
            )

            assert len(sent) == 4
            for payload, permission in zip(
                sent,
                ("agents:manage", "agents:manage", "agents:drain", "agents:drain"),
                strict=True,
            ):
                assert payload.actor_context is not None
                assert payload.authorisation_snapshot_id is not None
                snapshot = client.portal.call(
                    runtime.identity_repository.get_authorisation_snapshot,
                    user.organisation_id,
                    payload.authorisation_snapshot_id,
                )
                assert snapshot is not None
                assert snapshot.principal_id == user.id
                assert snapshot.permission == permission
                assert snapshot.resource_type is ResourceType.AGENT
                assert snapshot.resource_id == str(bench.agent_id)

            mismatched_context = context.model_copy(
                update={"authorisation_snapshot_id": sent[0].authorisation_snapshot_id}
            )
            with pytest.raises(ValueError, match="snapshot evidence is unresolved or mismatched"):
                client.portal.call(
                    partial(
                        runtime.drain_agent,
                        bench.agent_id,
                        authentication_context=mismatched_context,
                    )
                )
            assert len(sent) == 4
