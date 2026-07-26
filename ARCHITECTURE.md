# Architecture

Lab Platform is a local-first, layered hardware-control service. Dependencies point inward; CI
providers and backend implementations remain replaceable adapters.

| Layer | Packages | Responsibility |
| --- | --- | --- |
| Transport/integration | `apps/agent`, `apps/cli`, `integrations/` | FastAPI routes, HTTP client, provider metadata and presentation |
| Application | `packages/core` services | CI coordination, selection/reservations, workflows, operations, cleanup, results |
| Domain | `packages/models`, core protocols/errors | Immutable contracts, transitions, backend ports, stable errors |
| Infrastructure | persistence, SimLab adapter, real backend | SQLite, artifact files, simulator mapping, ESP32 discovery/serial/esptool |

## Controlling data flow

```text
GitHub Actions / GitLab CI / Jenkins / local shell
                        |
                     labctl
                        | HTTP + bearer token
                    /api/v1
                        |
                 CI Session Service
             /           |            \
       Artifact      Selection +       Workflow Runner
       Service       Reservation             |
                        |               BackendRegistry
                        |               /             \
                     SQLite      SimLabBackend   RealLabBackend
                                                    |
                                               PhysicalTarget
                                                    |
                                               Esp32Target
```

CI systems never call SimLab or hardware drivers directly. GitHub job summaries and provider
environment detection live in integration/CLI adapters, not the domain layer. A manually launched
workflow and a CI-launched workflow reach the same runner and backend ports.

## CI session aggregate

A CI session coordinates resources that retain their own identity and lifecycle:

```text
CI session
|- provider/external run metadata
|- bench request and selected bench
|- timed CI reservation and heartbeat
|- input artifact records
|- workflow run and step results
|- output artifacts
`- outcome and cleanup result
```

Status progresses through creation, bench wait, reservation, execution, a provisional terminal
result, cleanup, and `completed`. The separate outcome preserves `succeeded`, `failed`,
`cancelled`, `timed_out`, or `infrastructure_error` after completion. Cleanup status/result are
also separate so a passed test with a leaked reservation is never reported as success.

Session, artifact upload, workflow launch, and finalization accept idempotency keys. Token owner
checks keep one automation owner from operating another owner's sessions or artifacts.

## Selection, ownership, and concurrency

The selection service filters online benches by exact capabilities, labels, and simulated/physical
policy. Available candidates rank ahead of busy ones, followed by preferred-label score,
least-recent use, and bench ID. Candidate choice and timed reservation creation share a transaction,
so selection is deterministic and two sessions cannot acquire the same bench.

SQLite is authoritative for bench catalog metadata, timed reservations, FIFO queues, operation
locks, operations, workflow definitions/runs/steps, API token records, CI sessions, generic
artifacts, cleanup results, recovery records, and audit events. Partial unique indexes allow only
one active reservation and one operation lock per bench. Transactional state transitions prevent
double assignment and duplicate idempotent resources.

Operations and sequential workflows execute as in-process asyncio tasks. Status and progress are
persisted after each transition. A heartbeat reaper synchronizes live sessions and cleans up
abandoned clients.

## Authentication boundary

CI/artifact/result routes use bearer-token dependencies with explicit scopes. Tokens are generated
from cryptographic random bytes and stored as one-way hashes; plaintext is returned only at
creation. Expiry, revocation, last use, and audit events are persisted.

Legacy operational routes stay open only during first-token bootstrap. Once any token record
exists, those routes also require their mapped scopes and bind owner-bearing requests and resource
reads to the authenticated token owner. This preserves an upgrade path for a fresh local Agent
without leaving an owner-string bypass after machine authentication is enabled.

The first token creation is allowed without authentication only while the token store is empty,
and that token must grant every supported scope. Afterward, token creation, listing, and revocation
require an active all-scope bearer; the last active all-scope token cannot be revoked. There is no
distinct administrator identity in Phase 4, so an all-scope CI credential is admin-equivalent and
operators should retain a separate backup before expiry. Network restriction and TLS termination
belong outside the Agent. This boundary is limited machine authentication, not a hardened
multi-tenant or public-internet security system.

## Workflows and results

Phase 4 workflows declare `string`, `integer`, `boolean`, and `artifact` inputs. The only expression
form is `${{ inputs.<name> }}`; no expression or shell evaluator exists. Pydantic models reject
unknown inputs, malformed placeholders, undeclared step capabilities, and unsupported fields.

The workflow runner resolves an artifact UUID through the artifact service to a controlled storage
path, then invokes typed backend operations. Persisted step results produce JSON test records and
JUnit XML. Complete serial output remains an artifact instead of one database row per line.

## Artifact boundary

Uploads are streamed, size-limited, checksum-verified, and placed under generated paths outside
source/static directories. User filenames are display metadata, never filesystem paths or commands.
Temporary content is atomically committed after validation. CI session, workflow run, workflow
step, and operation are valid artifact owners.

The recent serial stream buffer is bounded; the complete capture is persisted as a size-limited
artifact. Invalid byte handling and optional redaction are configured at the Agent.

## Recovery and cleanup

On success, failure, cancellation, timeout, missed heartbeat, or restart, cleanup cancels active
work when safe, releases the reservation and operation lock, closes serial resources, finalizes
artifacts, and persists individual results. Finalization is idempotent.

On Agent startup, active operations/workflows are not blindly resumed. Previous work receives
stable interruption diagnostics, stale locks and reservations are reconciled, CI sessions are
synchronized, and backend inventories are refreshed. An unresolved cleanup error becomes an
infrastructure outcome rather than being hidden behind the workflow result.

## Backend boundaries

`SimLabBackend` converts frozen simulator snapshots/progress into neutral models. SimLab owns
mutable simulated state and deterministic timing; manual clock mode supports tests and accelerated
mode supports demos.

`RealLabBackend` translates neutral operations to a configured `PhysicalTarget`. `Esp32Target`
owns port discovery, esptool arguments/output, serial sessions, chip validation, and boot-marker
parsing. Probe failures become offline/degraded health snapshots rather than crashing startup.

The same `esp32-ci-test` definition targets either backend through capabilities and labels.

## Transport

The Agent is a FastAPI application served by Uvicorn. Resources live under `/api/v1`; Swagger,
ReDoc, and OpenAPI are exposed at `/docs`, `/redoc`, and `/openapi.json`. Middleware assigns a
request ID, emits structured logs, and returns it in `X-Request-ID`. Domain errors use one stable
error envelope. Polling is always supported for progress; provider adapters may render output and
summaries without changing domain records.
