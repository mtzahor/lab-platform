# Architecture

Lab Platform is a layered distributed hardware-control system with a local compatibility mode.
Dependencies point inward; transports, CI providers, persistence, and backend implementations
remain replaceable adapters. The central control plane owns global coordination while every Agent
retains final authority over hardware execution and safety.

| Layer | Packages | Responsibility |
| --- | --- | --- |
| Transport/integration | `apps/control_plane`, `apps/agent`, `apps/cli`, `integrations/` | REST/WebSocket boundaries, process composition, HTTP clients, provider presentation |
| Distributed application | `packages/control_plane_core`, `packages/agent_runtime` | Enrollment, presence, inventory, leases, remote commands, reconciliation, transfer, connection/journal behavior |
| Local application | `packages/core` services | Backend selection/reservations, workflow execution, operations, CI cleanup and results |
| Domain | `packages/models`, `packages/agent_protocol`, core protocols/errors | Immutable contracts, typed versioned envelopes, transitions, ports, stable errors |
| Infrastructure | persistence, SimLab adapter, real backend | PostgreSQL/SQLite adapters, artifact files/cache, simulator mapping, ESP32 discovery/serial/esptool |

## Distributed controlling data flow

```text
GitHub Actions / GitLab CI / Jenkins / local shell
                        |
                     labctl
                        | HTTP + bearer token
                 Control Plane /api/v1
                        |
       +----------------+------------------+
       | global inventory/reservations/CI |
       | remote operations and artifacts  |
       +----------------+------------------+
                        | authenticated protocol 1.0 WebSocket
              +---------+---------+
              |                   |
           Agent A             Agent B
       local journal/locks  local journal/locks
          /         \          /         \
   SimLabBackend  RealLab  SimLabBackend  RealLab
                       |                       |
                  Esp32Target            future target
```

CI systems never call an Agent, SimLab, or hardware driver directly. The control plane selects the
owning Agent and sends one complete sequential workflow command; that Agent invokes the same local
workflow runner and backend ports used in standalone mode. GitHub job summaries and provider
environment detection stay in integration/CLI adapters, not the domain layer.

## Distributed consistency and safety

The control plane is authoritative for global identity, reservations, routing, CI/workflow
requests, and user-facing operation history. An Agent is authoritative for backend health,
physical locks, safety policy, execution, progress, and local artifacts. The control plane cannot
turn transmission into success; it waits for an Agent terminal event.

Remote delivery is at least once. Commands are persisted before dispatch and before Agent
acceptance, then deduplicated by command ID/idempotency key in a durable Agent journal. Global
reservations become usable only after the owning Agent confirms a versioned local lease. Event
batches remain in the durable Agent buffer until the control plane routes them and returns an
explicit acknowledgment, so reconnect replay carries the same event IDs.

Heartbeat loss marks benches offline and running operations/reservations unknown for bounded
reconciliation periods. A reconnect report compares the boot ID, command journal, local leases,
inventory, and buffered events. Same-boot work can be recovered or safely redelivered; a new boot
is treated as process restart and missing work is never assumed to continue. See
[Phase 5](docs/PHASE_5.md) for the full state and failure model.

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

Session, artifact upload, workflow launch, and finalization accept idempotency keys. Schema v10
scopes those retry keys by organisation and their documented owner/bench component. Phase 6 binds
sessions to a principal, applies owner-or-organisation-grant lifecycle rules, and authorises
artifacts through trusted workflow/bench/CI parents.

## Selection, ownership, and concurrency

The distributed selection service filters online, non-draining Agents and benches by capabilities,
Agent/bench labels, location, and simulated/physical policy. Available candidates rank ahead of
busy ones, followed by preference, load, least-recent use, and stable bench ID. Candidate choice
and central reservation creation are coordinated, and the assignment is not visible as active
until the Agent confirms its lease.

The production control plane uses PostgreSQL for global Agent/inventory/command/lease/
reconciliation, workflow/CI/artifact/token/timeline state. SQLite remains available for the
loopback developer control plane and for Agent-local journal/buffer/lease/cache metadata.
Transactional state transitions and unique constraints/indexes prevent double assignment and
duplicate idempotent resources on both supported central stores.

Operations and sequential workflows execute as in-process asyncio tasks. Status and progress are
persisted after each transition. A heartbeat reaper synchronizes live sessions and cleans up
abandoned clients.

## Authentication boundary

The Agent gateway requires a unique Agent credential over WSS outside the explicit loopback demo.
One-time enrollment tokens and Agent credentials are stored only as hashes by the control plane;
credential rotation/revocation is per Agent. The Agent stores only its non-secret ID in YAML and
reads credential plaintext from a named environment variable. Protocol message IDs/sequences,
command expiry, strict typed payloads, and bounded queues constrain replay and input handling.

The control plane resolves a Phase 6 user session or identity-bound service-account credential to
an organisation principal before optional legacy-token fallback. Local/OIDC sessions and service
credentials are generated from cryptographic random bytes and stored as one-way hashes; plaintext
is returned only at login/creation. Expiry, rotation/revocation, last use, credential narrowing,
and audit events are persisted. Fixed roles, team/direct assignments, and bench/workflow access
policies evaluate trusted resources. Protected command, reservation, workflow, CI,
drain/enrollment/runtime-lifecycle, and artifact services repeat critical checks before side
effects.

Identity-backed manual, workflow, and CI-launched remote commands persist the initiating actor and
exact authorisation snapshot. Principal-initiated cancellation, reconciliation, inventory-refresh,
and drain/undrain control messages carry the same actor/snapshot identity, and the protocol rejects
mismatched evidence. Exact idempotent retries remain tied to the stable principal, tenant, request
content, and still-allowed required permissions and return the already accepted command with its
original snapshot rather than treating a fresh session/snapshot ID as new work. Workflow replay
also fingerprints the effective credential restrictions. New work still requires current
authorisation. Automatic/background paths with no originating Phase 6 decision and legacy paths do
not manufacture an initiating actor; a CI-cancellation maintenance retry can reuse evidence already
persisted by the principal request.

Agent-control paths persist an attributed intent before the in-memory WebSocket enqueue, while the
protocol journal records a successful wire send. These records distinguish an authorised request
from delivery, but they are not a transactional replay outbox and do not by themselves redeliver an
arbitrary control message after restart.

The standalone Agent's legacy operational routes stay open only during first-token bootstrap. Once
any token record exists, those routes require mapped scopes and bind owner-bearing requests and
resource reads to the authenticated token owner. The control plane may accept those legacy tokens
only while its explicit compatibility switch is enabled; they have no organisation principal and
remain outside Phase 6 tenant-isolation guarantees.

The first token creation is allowed without authentication only while the relevant token store is
empty, and that token must grant every scope supported by that endpoint. The standalone Agent then
requires all seven local scopes for token administration; the control plane requires
`agents:admin`. Last-administrator revocation guards apply in both cases, but cannot prevent expiry,
so operators should retain a separate backup. The distributed control-plane server can terminate
TLS directly when both certificate/key paths are configured. A reverse proxy is accepted only in
explicit TLS-termination mode while the server process remains loopback-bound. This boundary is
not yet a hardened public-internet service: lower-priority internal storage, remaining global
Agent/bench identifiers, automatic legacy-token conversion, shared OIDC transaction state,
issuer/subject binding, broad rate limiting, trusted-proxy address policy, mTLS, HA, and
secret-vault integration remain unfinished or out of scope.

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

Both applications use FastAPI/Uvicorn for REST. Resources live under `/api/v1`; Swagger, ReDoc,
and OpenAPI are exposed at `/docs`, `/redoc`, and `/openapi.json`. The control plane additionally
hosts one authenticated protocol WebSocket per Agent and keeps binary artifacts on scoped HTTP
transfer routes rather than the control channel. Request middleware returns `X-Request-ID`, and
domain errors use a stable envelope. Polling remains the portable client progress mechanism.

Checked-in contracts are [`docs/control-plane-openapi.json`](docs/control-plane-openapi.json) and
[`docs/openapi.json`](docs/openapi.json) for the standalone Agent compatibility API.
