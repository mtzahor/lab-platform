from __future__ import annotations

from datetime import datetime
from uuid import UUID

from lab_platform.agent_protocol.messages import ProtocolModel, _as_utc
from lab_platform.models import RemoteCommand, ReservationLease
from pydantic import Field, SecretStr, field_serializer, field_validator, model_validator


class CommandRequestPayload(ProtocolModel):
    command: RemoteCommand
    reservation_lease: ReservationLease | None = None

    @model_validator(mode="after")
    def validate_lease_binding(self) -> CommandRequestPayload:
        if self.command.reservation_id is None:
            if self.reservation_lease is not None:
                raise ValueError("Unreserved command cannot carry a reservation lease")
            return self
        if self.reservation_lease is None:
            raise ValueError("Reserved command requires a reservation lease")
        if (
            self.reservation_lease.reservation_id != self.command.reservation_id
            or self.reservation_lease.agent_id != self.command.agent_id
            or self.reservation_lease.bench_id != self.command.bench_id
            or self.reservation_lease.lease_version != self.command.lease_version
        ):
            raise ValueError("Command and reservation lease identities differ")
        return self


class CommandCancelPayload(ProtocolModel):
    command_id: UUID
    reason: str | None = Field(default=None, max_length=1000)


class EventAckPayload(ProtocolModel):
    batch_message_id: UUID
    acknowledged_event_sequence: int = Field(ge=1, strict=True)


class InventoryRefreshRequestPayload(ProtocolModel):
    request_id: UUID


class ReconciliationRequestPayload(ProtocolModel):
    request_id: UUID
    expected_boot_id: UUID | None = None
    last_control_plane_sequence: int = Field(default=0, ge=0, strict=True)


class ArtifactUploadRequestPayload(ProtocolModel):
    transfer_id: UUID
    artifact_id: UUID
    local_artifact_id: UUID | None = None
    upload_url: str = Field(min_length=1, max_length=4000)
    transfer_token: SecretStr
    expires_at: datetime
    maximum_size_bytes: int = Field(ge=0, strict=True)
    expected_sha256: str = Field(min_length=64, max_length=64)

    @field_validator("expires_at")
    @classmethod
    def normalize_expires_at(cls, value: datetime) -> datetime:
        return _as_utc(value, field="Artifact upload expiry")

    @field_serializer("transfer_token", when_used="json")
    def serialize_transfer_token(self, value: SecretStr) -> str:
        return value.get_secret_value()


class ConfigRefreshRequestPayload(ProtocolModel):
    config_version: int = Field(ge=1, strict=True)


class DrainAgentPayload(ProtocolModel):
    drain: bool = True
    deadline: datetime | None = None

    @field_validator("deadline")
    @classmethod
    def normalize_deadline(cls, value: datetime | None) -> datetime | None:
        return _as_utc(value, field="Agent drain deadline") if value is not None else None


class ReservationActivatedPayload(ProtocolModel):
    lease: ReservationLease


class ReservationReleasedPayload(ProtocolModel):
    reservation_id: UUID
    bench_id: str = Field(min_length=3, max_length=600)
    lease_version: int = Field(ge=1, strict=True)
    released_at: datetime

    @field_validator("released_at")
    @classmethod
    def normalize_released_at(cls, value: datetime) -> datetime:
        return _as_utc(value, field="Reservation release timestamp")
