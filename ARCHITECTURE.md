# Architecture

Lab Platform uses four layers with dependencies pointing inward.

| Layer | Packages | Responsibility |
| --- | --- | --- |
| Transport | `apps/agent`, `apps/cli` | FastAPI routes, HTTP serialization, CLI presentation |
| Application | `packages/core` services | Catalog, reservation/scheduling policy, workflows, operation lifecycle |
| Domain | `packages/models`, core protocols/errors | Immutable contracts, state rules, ports, stable errors |
| Infrastructure | persistence, SimLab adapter, real backend | SQLite, simulator mapping, ESP32 discovery/serial/esptool |

The controlling data flow is:

```text
labctl -> /api/v1 -> FastAPI route -> application services -> BackendRegistry
                                 |             |                 |-- SimLabBackend(s)
                                 |             |                 `-- RealLabBackend(s)
                                 |             `-> scheduling/workflow services
                                 `----------------> SQLite repositories
```

Application services and API routes import neither SimLab nor ESP32 code. Backend construction
occurs only in the Agent composition root; routing uses globally unique bench IDs registered at
startup. `RealLabBackend` delegates neutral backend operations to a `PhysicalTarget`; Phase 2's
first implementation remains `Esp32Target`.

## Ownership and concurrency

SQLite is authoritative for the bench catalog, timed reservations, queues, operation locks,
operations, workflow runs, uploaded-firmware and operation-artifact metadata, and audit events.
Partial unique indexes allow only one active reservation and one operation lock per bench. Time
range checks and transactional transitions make activation, expiry, and FIFO promotion safe under
concurrent requests.

Operations and sequential workflows execute as in-process asyncio tasks. Their status and progress
are persisted after each transition. The scheduler uses an injected UTC clock. On startup,
previously active work is failed with `AGENT_RESTARTED`, stale locks are cleared, reservations are
reconciled, and backend inventories are refreshed; jobs themselves are not resumed.

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
