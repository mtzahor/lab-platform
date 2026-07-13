# Phase 1

Phase 1 validates that an engineer can safely reserve and control a bench across a stable transport
boundary. The implemented release is `0.2.0-alpha`.

## Delivered

- `/api/v1` REST API with generated OpenAPI documentation
- HTTP-only `labctl` commands for benches, operations, and events
- Idempotent reservation/release with explicit owner checks
- Trackable power-on, power-off, power-cycle, and firmware-flash operations
- Best-effort operation cancellation and restart recovery
- Per-bench mutation locking
- Multipart firmware upload, local validation, size limit, and SHA-256 storage
- SQLite persistence for reservations, operations, events, and artifact metadata
- SimLab adapter implementing the neutral `LabBackend` protocol
- Manual and accelerated deterministic clocks and failure injection
- Unit, backend-contract-style, API integration, and real CLI end-to-end coverage

## Operational boundaries

The owner field is a workflow identifier, not authentication. The Agent is designed as one local
process and runs jobs in-process. Operation records survive restart, but active jobs are failed with
`AGENT_RESTARTED` rather than resumed. There are no physical drivers, cloud services, roles,
WebSockets, schedulers, or external job queues in this phase.

See the [README](README.md) for the demonstration and [API.md](API.md) for the transport contract.
