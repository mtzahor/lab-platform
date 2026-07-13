from __future__ import annotations

from datetime import datetime
from typing import Protocol
from uuid import UUID

from lab_platform.models import (
    EventRecord,
    FirmwareArtifact,
    Operation,
    OperationStatus,
    OperationType,
    Reservation,
)


class ReservationRepository(Protocol):
    async def create(self, reservation: Reservation) -> Reservation: ...

    async def get_active(self, bench_id: str) -> Reservation | None: ...

    async def release(self, reservation: Reservation) -> None: ...


class OperationRepository(Protocol):
    async def create(self, operation: Operation) -> Operation: ...

    async def get(self, operation_id: UUID) -> Operation | None: ...

    async def update(self, operation: Operation) -> Operation: ...

    async def list(
        self,
        *,
        bench_id: str | None = None,
        status: OperationStatus | None = None,
        operation_type: OperationType | None = None,
        limit: int = 50,
    ) -> list[Operation]: ...

    async def recover_incomplete(self, now: datetime) -> int: ...


class EventRepository(Protocol):
    async def create(self, event: EventRecord) -> EventRecord: ...

    async def list(
        self,
        *,
        bench_id: str | None = None,
        event_type: str | None = None,
        after: datetime | None = None,
        before: datetime | None = None,
        limit: int = 50,
    ) -> list[EventRecord]: ...


class ArtifactRepository(Protocol):
    async def save(self, artifact: FirmwareArtifact) -> FirmwareArtifact: ...
