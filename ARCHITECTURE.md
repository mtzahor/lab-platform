# Architecture

Lab Platform uses four layers with dependencies pointing inward.

| Layer | Packages | Responsibility |
| --- | --- | --- |
| Transport | `apps/agent`, `apps/cli` | FastAPI routes, HTTP serialization, CLI presentation |
| Application | `packages/core/services.py` | Reservation policy, ownership, orchestration, operation lifecycle |
| Domain | `packages/models`, core protocols/errors | Immutable contracts, state rules, ports, stable errors |
| Infrastructure | persistence, SimLab adapter, real backend | SQLite, simulator mapping, ESP32 discovery/serial/esptool |

The controlling data flow is:

```text
labctl -> /api/v1 -> FastAPI route -> application service -> LabBackend protocol
                                                        -> repository protocols
                                      SimLabBackend -----^       ^
                                      RealLabBackend ----^       ^
                                      SQLite repositories -------+
```

Application services and API routes import neither SimLab nor ESP32 code. Backend selection occurs
only in `create_lab_backend()` in the Agent composition root. `RealLabBackend` delegates neutral
backend operations to a `PhysicalTarget`; Phase 2's first implementation is `Esp32Target`.

## Ownership and concurrency

SQLite is authoritative for reservations, operations, uploaded-firmware and operation-artifact
metadata, and audit events. A partial unique index allows only one active reservation per bench,
and another permits
only one pending/running/cancel-requested operation per bench. Those constraints make reservation
and operation creation atomic even when HTTP requests arrive concurrently.

Operations execute as in-process asyncio tasks. Their status and progress are persisted after each
transition. On startup, previously active records are marked failed with `AGENT_RESTARTED`; jobs
themselves are not resumed.

## Simulator boundary

SimLab owns mutable device state and deterministic timing. `SimLabBackend` converts frozen
simulator snapshots and progress records into neutral models. No internal mutable simulator object
crosses the adapter boundary. Manual clock mode is available to deterministic tests; accelerated
mode scales simulated delays for local demos.

## Physical-target boundary

`RealLabBackend` owns configured targets and translates unsupported power actions generically.
`Esp32Target` owns port discovery, esptool arguments/output, serial sessions, chip validation, and
boot-marker parsing. The generic operation runner persists serial captures without understanding
ESP32 output. Probe failures become offline/degraded health snapshots instead of crashing startup.

## Transport

The Agent is a FastAPI application served by Uvicorn. All resources live under `/api/v1`.
Middleware assigns a request ID, emits structured request logs, and returns it in the response.
Domain errors are translated into one stable error envelope. Firmware is streamed to a temporary
file, size-checked, SHA-256-addressed, and then passed to the backend as metadata.
