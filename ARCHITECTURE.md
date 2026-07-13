# Architecture

Lab Platform uses four layers with dependencies pointing inward.

| Layer | Packages | Responsibility |
| --- | --- | --- |
| Transport | `apps/agent`, `apps/cli` | FastAPI routes, HTTP serialization, CLI presentation |
| Application | `packages/core/services.py` | Reservation policy, ownership, orchestration, operation lifecycle |
| Domain | `packages/models`, core protocols/errors | Immutable contracts, state rules, ports, stable errors |
| Infrastructure | persistence and SimLab adapter packages | SQLite, simulator mapping, backend implementation |

The controlling data flow is:

```text
labctl -> /api/v1 -> FastAPI route -> application service -> LabBackend protocol
                                                        -> repository protocols
                                      SimLabBackend -----^       ^
                                      SQLite repositories -------+
```

Application services and API routes never import SimLab. Backend selection occurs only in
`create_lab_backend()` in the Agent composition root, so a future real backend can replace SimLab
without changing routes, services, or CLI commands.

## Ownership and concurrency

SQLite is authoritative for reservations, operations, uploaded-firmware metadata, and audit
events. A partial unique index allows only one active reservation per bench, and another permits
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

## Transport

The Agent is a FastAPI application served by Uvicorn. All Phase 1 resources live under `/api/v1`.
Middleware assigns a request ID, emits structured request logs, and returns it in the response.
Domain errors are translated into one stable error envelope. Firmware is streamed to a temporary
file, size-checked, SHA-256-addressed, and then passed to the backend as metadata.
