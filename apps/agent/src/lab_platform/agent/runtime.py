from __future__ import annotations

import logging
from pathlib import Path

from lab_platform.config import PlatformConfig, load_config
from lab_platform.core import (
    VERSION,
    AgentCore,
    BenchService,
    CapabilityRegistry,
    ConfigurationError,
    EventBus,
    EventService,
    HealthMonitor,
    OperationRunner,
    OperationService,
    ReservationService,
)
from lab_platform.core.backend import LabBackend
from lab_platform.logging import get_logger
from lab_platform.models import (
    Bench,
    BenchSnapshot,
    Event,
    HealthReport,
    HealthStatus,
    PluginMetadata,
)
from lab_platform.persistence import (
    SQLiteArtifactRepository,
    SQLiteDatabase,
    SQLiteEventRepository,
    SQLiteOperationArtifactRepository,
    SQLiteOperationRepository,
    SQLiteReservationRepository,
)
from lab_platform.plugins import PluginManager
from lab_platform.real_backend import RealLabBackend
from lab_platform.simlab_adapter import SimLabBackend


class LabAgent:
    def __init__(
        self,
        *,
        config: PlatformConfig,
        logger: logging.Logger,
        event_bus: EventBus,
        health_monitor: HealthMonitor,
        plugin_manager: PluginManager,
        backend: LabBackend,
        database: SQLiteDatabase,
        core: AgentCore,
        bench_service: BenchService,
        reservation_service: ReservationService,
        operation_service: OperationService,
        event_service: EventService,
        operation_runner: OperationRunner,
        artifacts_directory: Path,
    ) -> None:
        self.config = config
        self.bench_service = bench_service
        self.reservation_service = reservation_service
        self.operation_service = operation_service
        self.event_service = event_service
        self.operation_runner = operation_runner
        self.artifacts_directory = artifacts_directory
        self._logger = logger
        self._event_bus = event_bus
        self._health_monitor = health_monitor
        self._plugin_manager = plugin_manager
        self._backend = backend
        self._database = database
        self._core = core
        self._started = False
        self._bench_cache: list[BenchSnapshot] = []
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
            await self._publish_initial_health()
            await self._set_health("event_bus", HealthStatus.HEALTHY, "Event bus started")
            await self._core.start()

            plugins = await self._plugin_manager.load(self.config.plugins)
            for plugin in plugins:
                await self._core.register_plugin(plugin.metadata, plugin.capabilities())
            await self._set_health(
                "plugins", HealthStatus.HEALTHY, f"{len(plugins)} plugins loaded"
            )

            await self._backend.start()
            self._bench_cache = await self._backend.list_benches()
            for snapshot in self._bench_cache:
                await self._core.register_bench(
                    Bench(
                        name=snapshot.id,
                        status=snapshot.status,
                        capabilities=snapshot.capabilities,
                    )
                )
            await self._set_health(
                "backend",
                HealthStatus.HEALTHY,
                f"{len(self._bench_cache)} benches available through {self.config.backend.type}",
            )
            recovered = await self.operation_service.recover_interrupted()
            await self._set_health(
                "operations",
                HealthStatus.HEALTHY,
                f"Recovered {recovered} interrupted operations",
            )
            await self._set_health("agent", HealthStatus.HEALTHY, "Agent ready")
            self._started = True
            self._logger.info(
                "Agent ready with %d benches and %d plugins",
                len(self._bench_cache),
                len(plugins),
            )
        except Exception:
            await self._plugin_manager.shutdown()
            await self._backend.stop()
            await self._core.shutdown()
            self._database.close()
            await self._set_health("agent", HealthStatus.UNHEALTHY, "Agent failed to start")
            self._logger.exception("Agent startup failed")
            raise

    async def shutdown(self) -> None:
        if not self._started:
            return
        await self.operation_runner.shutdown()
        await self._plugin_manager.shutdown()
        await self._backend.stop()
        await self._core.shutdown()
        self._database.close()
        self._bench_cache.clear()
        await self._set_health("agent", HealthStatus.WARNING, "Agent stopped")
        self._logger.info("Agent stopped")
        self._started = False

    def benches(self) -> list[Bench]:
        return self._core.benches()

    def plugins(self) -> list[PluginMetadata]:
        return self._core.plugins()

    def health_reports(self) -> list[HealthReport]:
        return self._health_monitor.component_statuses()

    def health_payload(self) -> dict[str, object]:
        online = sum(1 for bench in self._bench_cache if bench.online)
        return {
            "status": self._core.health().value,
            "version": VERSION,
            "backend": self.config.backend.type,
            "database": "healthy" if self._started else "stopped",
            "benches": {"total": len(self._bench_cache), "online": online},
        }

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


def create_lab_backend(config: PlatformConfig) -> LabBackend:
    if config.backend.type == "simlab":
        return SimLabBackend(
            enabled=config.simlab.enabled,
            bench_count=config.simlab.benches,
            clock_mode=config.simlab.clock_mode,
            speed_multiplier=config.simlab.speed_multiplier,
            flash_duration_seconds=config.simlab.flash_duration_seconds,
        )
    if config.backend.type == "real":
        return RealLabBackend.from_config(config.hardware)
    raise ConfigurationError(f"Unsupported backend type: {config.backend.type}")


def create_agent(config_dir: str | Path = "config") -> LabAgent:
    config_root = Path(config_dir)
    config = load_config(config_root)
    storage_root = (
        config_root.parent if config_root.is_file() or config_root.name == "config" else config_root
    )
    database = SQLiteDatabase(_database_path(config.database.url, storage_root))
    reservations = SQLiteReservationRepository(database)
    operations = SQLiteOperationRepository(database)
    events = SQLiteEventRepository(database)
    artifacts = SQLiteArtifactRepository(database)
    operation_artifacts = SQLiteOperationArtifactRepository(database)
    backend = create_lab_backend(config)
    artifact_path = config.artifacts.directory
    if not artifact_path.is_absolute():
        artifact_path = storage_root / artifact_path
    runner = OperationRunner(
        backend,
        operations,
        events,
        operation_artifacts,
        artifact_path,
    )
    reservation_service = ReservationService(backend, reservations, events)
    bench_service = BenchService(
        backend,
        reservation_service,
        reservations,
        operations,
        events,
        artifacts,
        runner,
    )
    operation_service = OperationService(operations, events, runner, operation_artifacts)
    event_service = EventService(events)
    event_bus = EventBus()
    health_monitor = HealthMonitor()
    logger = get_logger(config.agent.name, config.agent.log_level)
    get_logger("lab-platform.operations", config.agent.log_level)
    get_logger("lab-platform.backend.simlab", config.agent.log_level)
    get_logger("lab-platform.backend.real", config.agent.log_level)
    return LabAgent(
        config=config,
        logger=logger,
        event_bus=event_bus,
        health_monitor=health_monitor,
        plugin_manager=PluginManager(),
        backend=backend,
        database=database,
        core=AgentCore(event_bus, health_monitor, CapabilityRegistry()),
        bench_service=bench_service,
        reservation_service=reservation_service,
        operation_service=operation_service,
        event_service=event_service,
        operation_runner=runner,
        artifacts_directory=artifact_path,
    )


def _database_path(url: str, storage_root: Path) -> Path:
    prefix = "sqlite:///"
    if not url.startswith(prefix):
        raise ConfigurationError("Lab Platform supports only sqlite:/// database URLs", url=url)
    path = Path(url.removeprefix(prefix))
    return path if path.is_absolute() else storage_root / path
