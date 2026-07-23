# Phase 3: Shared Labs, Scheduling, and Multi-Bench Operations

Phase 3 makes a single Lab Platform Agent safe for a team sharing multiple simulated and
physical benches. It adds timed reservations, persistent FIFO queues, deterministic scheduling,
mixed backend routing, operation locks, restart recovery, timelines, and sequential workflows.

## Supported deployment

One Agent owns one SQLite database and one or more backend instances. Bench identifiers are
globally unique inside that Agent. API routes and application services resolve the backend through
the registry and never branch on a backend type.

This phase intentionally does not include authentication, RBAC, a web dashboard, CI provider
integrations, distributed Agents, distributed locks, or arbitrary workflow code.

## Team safety invariants

- At most one active reservation exists for a bench.
- Scheduled reservations for a bench never overlap.
- Mutating operations require the caller's active reservation.
- At most one mutating operation lock exists for a bench.
- Queue promotion and reservation activation use transactional, idempotent transitions.
- Persisted timestamps are UTC; API timestamps use RFC 3339.
- An offline bench cannot activate a reservation or promote a queue entry.
- Startup recovery fails interrupted operations and clears their stale locks.

## Configuration

Phase 1 and Phase 2 single-backend configuration remains accepted. New deployments should use
the `backends` list plus the `reservations`, `scheduler`, `operations`, and `workflows` sections.
See [MULTI_BACKEND.md](MULTI_BACKEND.md) and [TEAM_DEMO.md](TEAM_DEMO.md).

## Main flow

```text
Discover benches -> reserve or queue -> run operations/workflow
                 -> record timeline -> release/expire -> promote queue
```

The REST API remains under `/api/v1`. Run `labctl --help`, `labctl reservation --help`, and
`labctl workflow --help` for the complete CLI surface.

## Verification

Normal verification needs no hardware:

```bash
ruff check .
ruff format --check .
mypy .
pytest -m "not hardware"
```

Physical ESP32 checks remain opt-in through the `hardware` pytest marker.
