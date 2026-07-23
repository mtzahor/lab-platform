from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

from lab_platform.config import (
    PlatformConfig,
    RealBackendSettings,
    SimLabBackendSettings,
    load_config,
)
from lab_platform.core import (
    VERSION,
    AgentCore,
    BenchOperationInProgressError,
    BenchService,
    CapabilityRegistry,
    ConfigurationError,
    EventBus,
    EventService,
    HealthMonitor,
    OperationRunner,
    OperationService,
)
from lab_platform.core.backend import LabBackend
from lab_platform.core.backend_registry import BackendFailure, BackendRegistry
from lab_platform.core.bench_catalog import (
    BenchCatalog,
    BenchMetadata,
    BenchRecord,
    CatalogRefreshResult,
)
from lab_platform.core.clock import Clock, UtcClock
from lab_platform.core.operation_locks import OperationLockService
from lab_platform.core.recovery import RecoveryService
from lab_platform.core.reservation_ports import (
    OperationLockRepository,
    ReservationEventRepository,
    TimedReservationRepository,
)
from lab_platform.core.reservations import ReservationService as TimedReservationService
from lab_platform.core.scheduling import SchedulingService
from lab_platform.core.workflows import WorkflowRunner, WorkflowService
from lab_platform.logging import get_logger
from lab_platform.models import (
    Bench,
    BenchOperationLock,
    BenchSnapshot,
    Event,
    EventRecord,
    HealthReport,
    HealthStatus,
    PluginMetadata,
    ReservationStatus,
    TargetHealth,
)
from lab_platform.persistence import (
    SQLiteArtifactRepository,
    SQLiteCatalogRepository,
    SQLiteDatabase,
    SQLiteEventRepository,
    SQLiteOperationArtifactRepository,
    SQLiteOperationLockRepository,
    SQLiteOperationRepository,
    SQLiteQueueRepository,
    SQLiteRecoveryRepository,
    SQLiteTimedReservationRepository,
    SQLiteTimelineRepository,
)
from lab_platform.persistence.workflows import SQLiteWorkflowRepository
from lab_platform.plugins import PluginManager
from lab_platform.real_backend import RealLabBackend
from lab_platform.simlab_adapter import SimLabBackend


class _WorkflowOperationLockAdapter:
    """Persist workflow locks without writing workflow IDs to operation event FKs."""

    def __init__(
        self,
        locks: OperationLockRepository,
        reservations: TimedReservationRepository,
        authorizer: TimedReservationService,
        events: ReservationEventRepository,
        clock: Clock,
    ) -> None:
        self._locks = locks
        self._reservations = reservations
        self._authorizer = authorizer
        self._events = events
        self._clock = clock

    async def acquire(self, bench_id: str, operation_id: UUID, owner: str) -> BenchOperationLock:
        now = self._clock.now()
        lock = await self._locks.acquire_for_active(
            BenchOperationLock(
                bench_id=bench_id,
                operation_id=operation_id,
                acquired_at=now,
            ),
            owner,
            now,
        )
        try:
            await self._events.create(
                EventRecord(
                    timestamp=now,
                    type="OPERATION_LOCK_ACQUIRED",
                    source="workflow",
                    bench_id=bench_id,
                    actor=owner,
                    payload={"workflow_run_id": str(operation_id)},
                    deduplication_key=f"workflow:{operation_id}:lock-acquired",
                )
            )
        except Exception:
            await self._locks.release(bench_id, operation_id)
            raise
        return lock

    async def release(self, bench_id: str, operation_id: UUID) -> bool:
        released = await self._locks.release(bench_id, operation_id)
        if not released:
            return False
        now = self._clock.now()
        await self._reservations.finalize_pending_expiry(bench_id, now)
        await self._events.create(
            EventRecord(
                timestamp=now,
                type="OPERATION_LOCK_RELEASED",
                source="workflow",
                bench_id=bench_id,
                payload={"workflow_run_id": str(operation_id)},
                deduplication_key=f"workflow:{operation_id}:lock-released",
            )
        )
        return True


class _ProbeHealthRecorder:
    """Reconcile direct probe outcomes while the caller still owns its bench lock."""

    def __init__(
        self,
        catalog: BenchCatalog,
        repository: SQLiteCatalogRepository,
        events: SQLiteEventRepository,
        clock: Clock,
    ) -> None:
        self._catalog = catalog
        self._repository = repository
        self._events = events
        self._clock = clock

    async def record(self, health: TargetHealth) -> None:
        previous = self._catalog.get(health.bench_id)
        updated = self._catalog.record_probe_result(health.bench_id, health.status)
        await self._persist_change(previous, updated)

    async def failure(self, bench_id: str) -> None:
        previous = self._catalog.get(bench_id)
        self._catalog.mark_probe_required(bench_id)
        await self._persist_change(previous, self._catalog.get(bench_id))

    async def _persist_change(self, previous: BenchRecord, updated: BenchRecord) -> None:
        await self._repository.upsert_record(updated)
        if previous.online == updated.online and previous.health is updated.health:
            return
        await self._events.create(
            EventRecord(
                timestamp=self._clock.now(),
                type="BENCH_HEALTH_CHANGED",
                source="health-probe",
                bench_id=updated.id,
                payload={
                    "backend_id": updated.backend_id,
                    "online": updated.online,
                    "health": updated.health.value,
                },
                deduplication_key=(f"bench:{updated.id}:health:{updated.updated_at.isoformat()}"),
            )
        )


class LabAgent:
    def __init__(
        self,
        *,
        config: PlatformConfig,
        logger: logging.Logger,
        event_bus: EventBus,
        health_monitor: HealthMonitor,
        plugin_manager: PluginManager,
        backend_registry: BackendRegistry,
        catalog: BenchCatalog,
        catalog_repository: SQLiteCatalogRepository,
        clock: Clock,
        database: SQLiteDatabase,
        core: AgentCore,
        bench_service: BenchService,
        reservation_service: TimedReservationService,
        operation_service: OperationService,
        event_service: EventService,
        operation_runner: OperationRunner,
        operation_locks: OperationLockRepository,
        queue_repository: SQLiteQueueRepository,
        timeline_repository: SQLiteTimelineRepository,
        scheduling_service: SchedulingService,
        recovery_service: RecoveryService,
        workflow_service: WorkflowService,
        workflow_runner: WorkflowRunner,
        event_repository: SQLiteEventRepository,
        probe_health_recorder: _ProbeHealthRecorder,
        workflow_directory: Path,
        workflow_paths: tuple[Path, ...],
        artifacts_directory: Path,
    ) -> None:
        self.config = config
        self.backend_registry = backend_registry
        self.catalog = catalog
        self.bench_service = bench_service
        self.reservation_service = reservation_service
        self.operation_service = operation_service
        self.event_service = event_service
        self.operation_runner = operation_runner
        self.queue_repository = queue_repository
        self.timeline_repository = timeline_repository
        self.scheduling_service = scheduling_service
        self.recovery_service = recovery_service
        self.workflow_service = workflow_service
        self.artifacts_directory = artifacts_directory
        self._logger = logger
        self._catalog_repository = catalog_repository
        self._clock = clock
        self._event_bus = event_bus
        self._health_monitor = health_monitor
        self._plugin_manager = plugin_manager
        self._backend: LabBackend = backend_registry
        self._database = database
        self._core = core
        self._workflow_runner = workflow_runner
        self._event_repository = event_repository
        self._probe_health_recorder = probe_health_recorder
        self._operation_locks = operation_locks
        self._workflow_directory = workflow_directory
        self._workflow_paths = workflow_paths
        self._started = False
        self._bench_cache: list[BenchSnapshot] = []
        self._background_tasks: set[asyncio.Task[None]] = set()
        self._release_tasks: dict[UUID, asyncio.Task[None]] = {}
        self._last_health_probe_at: dict[str, datetime] = {}
        self._health_probe_poll_seconds = 5.0
        self._health_monitor.report("configuration", HealthStatus.HEALTHY, "Configuration loaded")
        self._health_monitor.report("logging", HealthStatus.HEALTHY, "Logging initialized")

    @property
    def started(self) -> bool:
        return self._started

    async def start(self) -> None:
        if self._started:
            return
        try:
            self._database.initialize()
            await self._set_health("database", HealthStatus.HEALTHY, "SQLite ready")
            await self._restore_persistent_catalog()
            await self._publish_initial_health()
            await self._set_health("event_bus", HealthStatus.HEALTHY, "Event bus started")
            await self._core.start()

            plugins = await self._plugin_manager.load(self.config.plugins)
            for plugin in plugins:
                await self._core.register_plugin(plugin.metadata, plugin.capabilities())
            await self._set_health(
                "plugins", HealthStatus.HEALTHY, f"{len(plugins)} plugins loaded"
            )

            await self.backend_registry.start()
            catalog_refresh = await self.catalog.refresh()
            await self._persist_catalog_refresh(catalog_refresh)
            self._bench_cache = [
                item.snapshot for item in self.backend_registry.last_refresh.benches
            ]
            observed_at = self._clock.now()
            self._last_health_probe_at = {record.id: observed_at for record in self.catalog.list()}
            for snapshot in self._bench_cache:
                await self._core.register_bench(
                    Bench(
                        name=snapshot.id,
                        status=snapshot.status,
                        capabilities=snapshot.capabilities,
                    )
                )
            backend_failures = self._backend_failures(catalog_refresh.backend_failures)
            backend_status = HealthStatus.WARNING if backend_failures else HealthStatus.HEALTHY
            backend_message = (
                f"{len(self._bench_cache)} benches available through backend IDs "
                f"{', '.join(self.backend_registry.backend_ids)}"
            )
            if backend_failures:
                backend_message += f"; unavailable: {', '.join(backend_failures)}"
            await self._set_health("backend", backend_status, backend_message)

            loaded_workflows = await self._load_workflow_definitions()
            recovered_operations = await self.operation_service.recover_interrupted()
            recovery = await self.recovery_service.recover(
                interrupted_operations=recovered_operations
            )
            recovered_workflows = await self.workflow_service.recover_interrupted()
            await self._set_health(
                "operations",
                HealthStatus.HEALTHY,
                f"Recovered {recovered_operations} interrupted operations",
            )
            await self._set_health(
                "recovery",
                HealthStatus.HEALTHY,
                "Recovery completed: "
                f"{recovery.stale_locks_removed} stale locks, "
                f"{recovery.reservations_expired} expired reservations",
            )
            await self._set_health(
                "workflows",
                HealthStatus.HEALTHY,
                f"Loaded {loaded_workflows} definitions; recovered {recovered_workflows} runs",
            )
            await self._set_health("agent", HealthStatus.HEALTHY, "Agent ready")
            self._started = True
            await self.start_background_workers()
            self._logger.info(
                "Agent ready with %d benches, %d backends, and %d plugins",
                len(self._bench_cache),
                len(self.backend_registry.backend_ids),
                len(plugins),
            )
        except Exception:
            await self.stop_background_workers()
            await self._workflow_runner.shutdown()
            await self.operation_runner.shutdown()
            await self._plugin_manager.shutdown()
            await self.backend_registry.stop()
            await self._core.shutdown()
            self._database.close()
            await self._set_health("agent", HealthStatus.UNHEALTHY, "Agent failed to start")
            self._logger.exception("Agent startup failed")
            raise

    async def shutdown(self) -> None:
        if not self._started:
            return
        await self.stop_background_workers()
        await self._workflow_runner.shutdown()
        await self._drain_release_tasks()
        await self.operation_runner.shutdown()
        await self._plugin_manager.shutdown()
        await self.backend_registry.stop()
        await self._core.shutdown()
        self._database.close()
        self._bench_cache.clear()
        await self._set_health("agent", HealthStatus.WARNING, "Agent stopped")
        self._logger.info("Agent stopped")
        self._started = False

    async def start_background_workers(self) -> None:
        if any(not task.done() for task in self._background_tasks):
            return
        loop = asyncio.get_running_loop()
        for worker, name in (
            (self._scheduler_loop(), "lab-platform-scheduler"),
            (self._health_probe_loop(), "lab-platform-health-probes"),
        ):
            task = loop.create_task(worker, name=name)
            self._background_tasks.add(task)
            task.add_done_callback(self._background_tasks.discard)

    async def stop_background_workers(self) -> None:
        tasks = list(self._background_tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._background_tasks.clear()

    def release_reservation_after_workflow(
        self,
        run_id: UUID,
        reservation_id: UUID,
        owner: str,
    ) -> None:
        existing = self._release_tasks.get(run_id)
        if existing is not None and not existing.done():
            return
        task = asyncio.get_running_loop().create_task(
            self._release_when_workflow_finishes(run_id, reservation_id, owner),
            name=f"release-reservation-{reservation_id}",
        )
        self._release_tasks[run_id] = task
        task.add_done_callback(lambda completed: self._discard_release_task(run_id, completed))

    def benches(self) -> list[Bench]:
        return self._core.benches()

    def plugins(self) -> list[PluginMetadata]:
        return self._core.plugins()

    def health_reports(self) -> list[HealthReport]:
        return self._health_monitor.component_statuses()

    def health_payload(self) -> dict[str, object]:
        catalog = self.catalog.list()
        online = sum(1 for bench in catalog if bench.online)
        definitions = self.config.effective_backends
        backend_kind = definitions[0].type if len(definitions) == 1 else "mixed"
        payload: dict[str, object] = {
            "status": self._core.health().value,
            "version": VERSION,
            "backend": backend_kind,
            "database": "healthy" if self._started else "stopped",
            "benches": {"total": len(catalog), "online": online},
        }
        failures = self._backend_failures(self.backend_registry.last_refresh.failures)
        if len(definitions) > 1 or failures:
            payload["backends"] = {
                "ids": list(self.backend_registry.backend_ids),
                "unavailable": failures,
            }
        return payload

    async def _scheduler_loop(self) -> None:
        interval = self.config.scheduler.poll_interval_seconds
        while True:
            await asyncio.sleep(interval)
            try:
                await self.scheduling_service.expire_reservations()
                await self._handle_expiry_overruns()
                refresh = await self.catalog.refresh()
                await self._persist_catalog_refresh(refresh)
                self._bench_cache = [
                    item.snapshot for item in self.backend_registry.last_refresh.benches
                ]
                await self.scheduling_service.process_due_reservations()
                if self.config.scheduler.automatic_assignment:
                    await self.scheduling_service.promote_queues()
                if refresh.backend_failures:
                    self._logger.warning(
                        "Some backends were unavailable during scheduled refresh",
                        extra={
                            "backend_ids": sorted(
                                failure.backend_id for failure in refresh.backend_failures
                            )
                        },
                    )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._logger.exception("Scheduled Phase 3 processing failed")
                try:
                    await self._event_repository.create(
                        EventRecord(
                            type="SCHEDULER_FAILURE",
                            source="scheduler",
                            payload={"error": str(exc)},
                        )
                    )
                except Exception:
                    self._logger.exception("Could not persist scheduler failure event")

    async def _health_probe_loop(self) -> None:
        while True:
            await asyncio.sleep(self._health_probe_poll_seconds)
            try:
                await self._probe_idle_benches()
            except asyncio.CancelledError:
                raise
            except Exception:
                self._logger.exception("Periodic bench health probing failed")

    async def _probe_idle_benches(self) -> None:
        """Refresh target health without colliding with reservations or operations."""

        interval = timedelta(seconds=30)
        maintenance_horizon = timedelta(seconds=60)
        for record in self.catalog.list():
            if "probe" not in {capability.casefold() for capability in record.capabilities}:
                continue
            now = self._clock.now()
            last_probe = self._last_health_probe_at.get(record.id)
            if last_probe is not None and now - last_probe < interval:
                continue
            if await self.reservation_service.get_active(record.id) is not None:
                continue
            probe_started_at = self._clock.now()
            probe_id = uuid4()
            try:
                await self._operation_locks.acquire_for_maintenance(
                    BenchOperationLock(
                        bench_id=record.id,
                        operation_id=probe_id,
                        acquired_at=probe_started_at,
                        expires_at=probe_started_at + maintenance_horizon,
                    ),
                    probe_started_at,
                )
            except BenchOperationInProgressError:
                continue
            try:
                if await self.reservation_service.get_active(record.id) is not None:
                    continue
                backend = await self.backend_registry.get_backend_for_bench(record.id)
                health = await backend.probe(record.id)
                await self._probe_health_recorder.record(health)
            except Exception:
                self._logger.exception(
                    "Periodic bench health probe failed",
                    extra={"bench_id": record.id, "backend_id": record.backend_id},
                )
                try:
                    await self._probe_health_recorder.failure(record.id)
                except Exception:
                    self._logger.exception(
                        "Could not persist failed bench probe state",
                        extra={"bench_id": record.id, "backend_id": record.backend_id},
                    )
            finally:
                self._last_health_probe_at[record.id] = self._clock.now()
                await self._operation_locks.release(record.id, probe_id)

    async def _handle_expiry_overruns(self) -> None:
        now = self._clock.now()
        grace = timedelta(seconds=self.config.reservations.expiry_grace_seconds)
        pending = await self.reservation_service.list(
            status=ReservationStatus.EXPIRED_PENDING_OPERATION,
            limit=500,
        )
        for reservation in pending:
            if reservation.ends_at is None or reservation.ends_at + grace > now:
                continue
            lock = await self._operation_locks.get(reservation.bench_id)
            if lock is None:
                continue
            cancelled = await self.operation_service.cancel_for_expiry(lock.operation_id)
            if not cancelled:
                cancelled = await self.workflow_service.cancel_for_expiry(lock.operation_id)
            self.catalog.mark_probe_required(reservation.bench_id)
            await self._event_repository.create(
                EventRecord(
                    timestamp=now,
                    type="OPERATION_GRACE_EXCEEDED",
                    source="scheduler",
                    bench_id=reservation.bench_id,
                    reservation_id=reservation.id,
                    payload={
                        "operation_id": str(lock.operation_id),
                        "cancellation_requested": cancelled,
                        "grace_seconds": self.config.reservations.expiry_grace_seconds,
                    },
                    deduplication_key=f"operation:{lock.operation_id}:grace-exceeded",
                )
            )

    async def refresh_catalog(self) -> CatalogRefreshResult:
        refresh = await self.catalog.refresh()
        await self._persist_catalog_refresh(refresh)
        self._bench_cache = [item.snapshot for item in self.backend_registry.last_refresh.benches]
        return refresh

    async def _load_workflow_definitions(self) -> int:
        paths = self._workflow_definition_files()
        for path in paths:
            source = await asyncio.to_thread(path.read_text, encoding="utf-8")
            await self.workflow_service.register_yaml(
                source,
                source_name=str(path),
                base_directory=path.parent,
            )
        return len(paths)

    async def _restore_persistent_catalog(self) -> None:
        now = self._clock.now()
        known_backend_ids = {
            registration.id for registration in await self._catalog_repository.list_backends()
        }
        configured_backend_ids = {definition.id for definition in self.config.effective_backends}
        for definition in self.config.effective_backends:
            await self._catalog_repository.upsert_backend(
                definition.id,
                definition.type,
                definition.config.model_dump(mode="json"),
                now=now,
            )
            if definition.id not in known_backend_ids:
                await self._event_repository.create(
                    EventRecord(
                        timestamp=now,
                        type="BACKEND_REGISTERED",
                        source="catalog",
                        payload={"backend_id": definition.id, "backend_type": definition.type},
                        deduplication_key=f"backend:{definition.id}:registered",
                    )
                )
        removed_backend_ids = known_backend_ids - configured_backend_ids
        if removed_backend_ids:
            await self._catalog_repository.reconcile(
                (),
                reconciled_backend_ids=removed_backend_ids,
                observed_at=now,
            )
        self.catalog.restore(
            await self._catalog_repository.load_records(),
            metadata=await self._catalog_repository.load_metadata(),
        )

    async def _persist_catalog_refresh(self, refresh: CatalogRefreshResult) -> None:
        previous = {record.id: record for record in await self._catalog_repository.load_records()}
        current_refresh = CatalogRefreshResult(
            records=tuple(self.catalog.list()),
            discovered_bench_ids=refresh.discovered_bench_ids,
            seen_bench_ids=refresh.seen_bench_ids,
            offline_bench_ids=refresh.offline_bench_ids,
            backend_failures=refresh.backend_failures,
        )
        reconciled = (
            self.backend_registry.last_refresh.refreshed_backend_ids
            | self.backend_registry.last_refresh.failed_backend_ids
        )
        await self._catalog_repository.reconcile(
            current_refresh.records,
            reconciled_backend_ids=reconciled,
            observed_at=self._clock.now(),
        )
        await self._emit_catalog_events(current_refresh, previous)

    async def _emit_catalog_events(
        self,
        refresh: CatalogRefreshResult,
        previous: dict[str, BenchRecord],
    ) -> None:
        now = self._clock.now()
        records = {record.id: record for record in refresh.records}
        for backend_failure in refresh.backend_failures:
            await self._event_repository.create(
                EventRecord(
                    timestamp=now,
                    type="BACKEND_UNAVAILABLE",
                    source="catalog",
                    payload={
                        "backend_id": backend_failure.backend_id,
                        "stage": backend_failure.stage,
                        "error": backend_failure.message,
                    },
                    deduplication_key=f"backend:{backend_failure.backend_id}:unavailable",
                )
            )
        for bench_id in sorted(refresh.discovered_bench_ids):
            record = records[bench_id]
            await self._event_repository.create(
                EventRecord(
                    timestamp=now,
                    type="BENCH_DISCOVERED",
                    source="catalog",
                    bench_id=bench_id,
                    payload={"backend_id": record.backend_id},
                    deduplication_key=f"bench:{bench_id}:discovered",
                )
            )
        for bench_id, record in records.items():
            old = previous.get(bench_id)
            if old is None:
                continue
            if old.online == record.online and old.health is record.health:
                continue
            await self._event_repository.create(
                EventRecord(
                    timestamp=now,
                    type="BENCH_HEALTH_CHANGED",
                    source="catalog",
                    bench_id=bench_id,
                    payload={
                        "backend_id": record.backend_id,
                        "online": record.online,
                        "health": record.health.value,
                    },
                    deduplication_key=f"bench:{bench_id}:health:{record.updated_at.isoformat()}",
                )
            )
            if (
                not record.online
                and old.online
                and bench_id in refresh.offline_bench_ids
                and record.backend_id in self.backend_registry.last_refresh.refreshed_backend_ids
            ):
                await self._event_repository.create(
                    EventRecord(
                        timestamp=now,
                        type="BENCH_REMOVED",
                        source="catalog",
                        bench_id=bench_id,
                        payload={"backend_id": record.backend_id},
                        deduplication_key=(
                            f"bench:{bench_id}:removed:{record.updated_at.isoformat()}"
                        ),
                    )
                )

    def _workflow_definition_files(self) -> tuple[Path, ...]:
        discovered: set[Path] = set()
        if self._workflow_directory.exists():
            if not self._workflow_directory.is_dir():
                raise ConfigurationError(
                    "workflows.definitions_directory must be a directory",
                    path=str(self._workflow_directory),
                )
            discovered.update(self._yaml_files(self._workflow_directory))
        for configured in self._workflow_paths:
            if not configured.exists():
                raise ConfigurationError(
                    "Configured workflow definition path does not exist",
                    path=str(configured),
                )
            if configured.is_dir():
                discovered.update(self._yaml_files(configured))
            elif configured.suffix.casefold() in {".yaml", ".yml"}:
                discovered.add(configured)
            else:
                raise ConfigurationError(
                    "Workflow definitions must be YAML files or directories",
                    path=str(configured),
                )
        return tuple(sorted(discovered, key=str))

    async def _release_when_workflow_finishes(
        self,
        run_id: UUID,
        reservation_id: UUID,
        owner: str,
    ) -> None:
        try:
            await self.workflow_service.wait(run_id)
            await self.reservation_service.release(reservation_id, owner)
        except asyncio.CancelledError:
            raise
        except Exception:
            self._logger.exception(
                "Could not release reservation after workflow",
                extra={
                    "workflow_run_id": str(run_id),
                    "reservation_id": str(reservation_id),
                },
            )

    async def _drain_release_tasks(self) -> None:
        tasks = list(self._release_tasks.values())
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._release_tasks.clear()

    def _discard_release_task(self, run_id: UUID, completed: asyncio.Task[None]) -> None:
        if self._release_tasks.get(run_id) is completed:
            self._release_tasks.pop(run_id, None)

    def _backend_failures(self, refresh_failures: tuple[BackendFailure, ...]) -> list[str]:
        failures = {
            failure.backend_id
            for failure in self.backend_registry.lifecycle_failures + refresh_failures
            if failure.stage != "stop"
        }
        return sorted(failures)

    async def _publish_initial_health(self) -> None:
        for report in self._health_monitor.component_statuses():
            await self._event_bus.publish(
                Event(
                    type="HealthChanged",
                    payload={
                        "component": report.component,
                        "status": report.status.value,
                        "message": report.message,
                    },
                )
            )

    async def _set_health(self, component: str, status: HealthStatus, message: str) -> None:
        self._health_monitor.report(component, status, message)
        await self._event_bus.publish(
            Event(
                type="HealthChanged",
                payload={"component": component, "status": status.value, "message": message},
            )
        )

    @staticmethod
    def _yaml_files(directory: Path) -> set[Path]:
        return {*directory.glob("*.yaml"), *directory.glob("*.yml")}


def create_backend_registry(config: PlatformConfig) -> BackendRegistry:
    backends: dict[str, LabBackend] = {}
    for definition in config.effective_backends:
        if isinstance(definition, SimLabBackendSettings):
            backend_config = definition.config
            backends[definition.id] = SimLabBackend(
                enabled=backend_config.enabled and backend_config.auto_start,
                bench_count=backend_config.bench_count,
                bench_prefix=backend_config.bench_prefix,
                clock_mode=backend_config.clock_mode,
                speed_multiplier=backend_config.speed_multiplier,
                flash_duration_seconds=backend_config.flash_duration_seconds,
            )
        elif isinstance(definition, RealBackendSettings):
            backends[definition.id] = RealLabBackend.from_config(definition.config)
        else:  # pragma: no cover - discriminated configuration union is exhaustive
            raise ConfigurationError(f"Unsupported backend type: {definition.type}")
    return BackendRegistry(backends)


def create_lab_backend(config: PlatformConfig) -> LabBackend:
    """Phase 2 factory name retained; the returned backend is now the registry."""

    return create_backend_registry(config)


def create_agent(config_dir: str | Path = "config") -> LabAgent:
    config_root = Path(config_dir)
    config = load_config(config_root)
    storage_root = (
        config_root.parent if config_root.is_file() or config_root.name == "config" else config_root
    )
    database = SQLiteDatabase(_database_path(config.database.url, storage_root))
    timed_reservations = SQLiteTimedReservationRepository(database)
    queues = SQLiteQueueRepository(database)
    operation_locks = SQLiteOperationLockRepository(database)
    recovery_records = SQLiteRecoveryRepository(database)
    timeline = SQLiteTimelineRepository(database)
    catalog_repository = SQLiteCatalogRepository(database)
    operations = SQLiteOperationRepository(database)
    events = SQLiteEventRepository(database)
    artifacts = SQLiteArtifactRepository(database)
    operation_artifacts = SQLiteOperationArtifactRepository(database)
    workflow_repository = SQLiteWorkflowRepository(database, initialize_schema=False)
    registry = create_backend_registry(config)
    clock = UtcClock()
    catalog = BenchCatalog(registry, metadata=_catalog_metadata(config), clock=clock)
    probe_health_recorder = _ProbeHealthRecorder(catalog, catalog_repository, events, clock)

    artifact_path = _relative_to_storage(config.artifacts.directory, storage_root)
    reservation_service = TimedReservationService(
        timed_reservations,
        queues,
        events,
        clock=clock,
        availability=catalog,
        operation_locks=operation_locks,
        default_duration_seconds=config.reservations.default_duration_minutes * 60,
        maximum_duration_seconds=config.reservations.maximum_duration_minutes * 60,
        queue_enabled=config.reservations.queue_enabled,
    )
    operation_lock_service = OperationLockService(
        operation_locks,
        timed_reservations,
        reservation_service,
        events,
        clock=clock,
    )
    operation_runner = OperationRunner(
        registry,
        operations,
        events,
        operation_artifacts,
        artifact_path,
        clock.now,
        operation_locks=operation_lock_service,
    )
    bench_service = BenchService(
        registry,
        reservation_service,
        timed_reservations,
        operations,
        events,
        artifacts,
        operation_runner,
        clock.now,
        operation_locks=operation_lock_service,
        availability=catalog,
        probe_result_handler=probe_health_recorder.record,
        probe_failure_handler=probe_health_recorder.failure,
    )
    scheduling_service = SchedulingService(
        timed_reservations,
        queues,
        operation_locks,
        events,
        catalog,
        clock=clock,
        scheduled_protection_window_seconds=(
            config.reservations.scheduled_protection_window_minutes * 60
        ),
        expiry_grace_seconds=config.reservations.expiry_grace_seconds,
    )
    recovery_service = RecoveryService(
        operations,
        operation_locks,
        scheduling_service,
        events,
        records=recovery_records,
        clock=clock,
        automatic_assignment=config.scheduler.automatic_assignment,
    )
    workflow_locks = _WorkflowOperationLockAdapter(
        operation_locks,
        timed_reservations,
        reservation_service,
        events,
        clock,
    )
    workflow_runner = WorkflowRunner(
        workflow_repository,
        registry,
        reservation_service,
        events,
        workflow_locks,
        clock=clock.now,
        probe_result_handler=probe_health_recorder.record,
        probe_failure_handler=probe_health_recorder.failure,
    )
    workflow_service = WorkflowService(
        workflow_repository,
        registry,
        reservation_service,
        workflow_runner,
        events,
        workflow_locks,
        clock=clock.now,
    )
    operation_service = OperationService(
        operations,
        events,
        operation_runner,
        operation_artifacts,
        clock.now,
        operation_locks=operation_lock_service,
    )
    event_service = EventService(events)
    event_bus = EventBus()
    health_monitor = HealthMonitor()
    logger = get_logger(config.agent.name, config.agent.log_level)
    get_logger("lab-platform.operations", config.agent.log_level)
    get_logger("lab-platform.backend.registry", config.agent.log_level)
    get_logger("lab-platform.backend.simlab", config.agent.log_level)
    get_logger("lab-platform.backend.real", config.agent.log_level)
    return LabAgent(
        config=config,
        logger=logger,
        event_bus=event_bus,
        health_monitor=health_monitor,
        plugin_manager=PluginManager(),
        backend_registry=registry,
        catalog=catalog,
        catalog_repository=catalog_repository,
        clock=clock,
        database=database,
        core=AgentCore(event_bus, health_monitor, CapabilityRegistry()),
        bench_service=bench_service,
        reservation_service=reservation_service,
        operation_service=operation_service,
        event_service=event_service,
        operation_runner=operation_runner,
        operation_locks=operation_locks,
        queue_repository=queues,
        timeline_repository=timeline,
        scheduling_service=scheduling_service,
        recovery_service=recovery_service,
        workflow_service=workflow_service,
        workflow_runner=workflow_runner,
        event_repository=events,
        probe_health_recorder=probe_health_recorder,
        workflow_directory=_relative_to_storage(
            config.workflows.definitions_directory, storage_root
        ),
        workflow_paths=tuple(
            _relative_to_storage(path, storage_root) for path in config.workflows.definition_paths
        ),
        artifacts_directory=artifact_path,
    )


def _catalog_metadata(config: PlatformConfig) -> dict[str, BenchMetadata]:
    metadata: dict[str, BenchMetadata] = {}
    for definition in config.effective_backends:
        if isinstance(definition, SimLabBackendSettings):
            width = max(2, len(str(definition.config.bench_count)))
            for index in range(definition.config.bench_count):
                bench_id = f"{definition.config.bench_prefix}-{index + 1:0{width}d}"
                metadata[bench_id] = BenchMetadata(
                    target_type="simlab",
                    labels={
                        "board": "virtual",
                        "location": "simulation",
                        "purpose": "testing",
                    },
                )
        elif isinstance(definition, RealBackendSettings):
            for bench in definition.config.benches:
                metadata[bench.id] = BenchMetadata(
                    target_type=bench.target_type,
                    labels=bench.labels,
                )
    return metadata


def _relative_to_storage(path: Path, storage_root: Path) -> Path:
    return path if path.is_absolute() else storage_root / path


def _database_path(url: str, storage_root: Path) -> Path:
    prefix = "sqlite:///"
    if not url.startswith(prefix):
        raise ConfigurationError("Lab Platform supports only sqlite:/// database URLs", url=url)
    path = Path(url.removeprefix(prefix))
    return path if path.is_absolute() else storage_root / path
