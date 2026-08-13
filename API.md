# REST API

The distributed platform has two HTTP applications. Clients use the central control plane; the
owning Agent is selected by global bench identity and never appears in a client URL. Each Agent
also retains its local compatibility/diagnostic API.

Both expose Swagger at `/docs`, ReDoc at `/redoc`, and OpenAPI at `/openapi.json`; public resources
use `/api/v1`. Checked-in contracts are
[`docs/control-plane-openapi.json`](docs/control-plane-openapi.json) and
[`docs/openapi.json`](docs/openapi.json) for the standalone Agent. REST responses include
`X-Request-ID`.

## Control-plane authentication

The control plane recognizes a Phase 6 user session or identity-bound service-account credential
first. It derives the organisation and principal from that bearer, resolves trusted resources,
applies roles/access policies/credential narrowing, and passes the authenticated context into
protected application services. Identity-only administration routes never accept a legacy token.

When `authorisation.legacy_token_compatibility_enabled` is true, protected operational routes may
fall back to a hashed Phase 4/5 scope-bearing token. If that token store is empty, the first
`POST /api/v1/tokens` request may bootstrap it without a bearer and must grant all nine supported
scopes. Only this token-creation route participates in empty-store bootstrap. Legacy tokens have no
organisation principal; their inventory and operational compatibility queries remain
deployment-global and are excluded from Phase 6 tenant-isolation guarantees.

The legacy scope map is:

| Scope | Protected control-plane resources |
| --- | --- |
| `agents:read` | Agent inventory/timelines and metrics |
| `agents:admin` | Client-token/enrollment administration, Agent revoke/drain/undrain/refresh, operation reconciliation |
| `benches:read` | Unified bench inventory |
| `reservations:write` | Central reservations and confirmed Agent leases |
| `workflows:run` | Workflow definitions/runs, direct bench actions, and operation cancellation |
| `operations:read` | Remote-operation/workflow status, results, and synchronized serial output |
| `artifacts:read` | Artifact metadata/content reads |
| `artifacts:write` | Client uploads, direct firmware staging, and Agent transfer issuance |
| `ci:sessions` | Distributed CI session lifecycle |

Agent enrollment is authenticated by a one-time enrollment token in its strict request body. The
Agent WebSocket at `/api/v1/agent-gateway/{agent_id}` uses that Agent's own bearer credential and
protocol `1.0`; it is not a client endpoint and is intentionally absent from ordinary `labctl`
routing. Artifact-transfer upload/download routes use their own short-lived Agent/artifact-scoped
capability instead of a client API token.

Credential rotation is a separate self-service boundary:
`POST /api/v1/agents/{agent_id}/credentials/rotate` authenticates the current Agent credential in
the strict JSON body and returns the replacement plaintext once. It is currently a REST operation,
not a `labctl agent` subcommand; store the replacement in the configured Agent credential secret
before reconnecting.

### Control-plane endpoint groups

| Group | Representative paths |
| --- | --- |
| Service | `GET /api/v1/version`, `GET /api/v1/health`, `GET /metrics` |
| Client token | `POST/GET /api/v1/tokens`, `POST /api/v1/tokens/{id}/revoke` |
| Agent identity | `/api/v1/agents`, `/api/v1/agents/enrollment-tokens`, `/api/v1/agents/enroll` |
| Agent administration | `/api/v1/agents/{id}/revoke`, `/credentials/rotate`, `/drain`, `/undrain`, `/actions/refresh-inventory`, `/timeline` |
| Unified inventory | `GET /api/v1/benches`, `GET /api/v1/benches/{global_id}` |
| Direct remote actions | `/api/v1/benches/{global_id}/actions/probe`, `/reset`, `/read-serial`, `/flash` |
| Central reservations | `/api/v1/reservations`, `/{id}/renew`, `/{id}/release` |
| Remote workflows | `/api/v1/workflows`, `/api/v1/workflows/{name}/runs` |
| Remote operations | `/api/v1/operations`, `/{id}`, `/{id}/cancel`, `/{id}/reconcile`, `/{id}/artifacts/serial` |
| Distributed CI | `/api/v1/ci/sessions`, `/{id}/run`, `/{id}/heartbeat`, `/{id}/cancel`, `/{id}/finalize`, `/{id}/artifacts` |
| Artifacts | `GET/POST /api/v1/artifacts`, `GET/DELETE /api/v1/artifacts/{id}`, `/{id}/content`, `/{id}/transfers` |
| Transfer capabilities | `/api/v1/artifact-transfers/{id}/content` |
| User authentication | `/api/v1/auth/login`, `/refresh`, `/logout`, `/me`, `/sessions` |
| OIDC | `/api/v1/auth/oidc/login`, `/callback` |
| Identity administration | `/api/v1/organisation`, `/users`, `/teams`, `/service-accounts`, `/role-assignments`, `/permissions/effective` |
| Access policies and audit | `/api/v1/access-policies/benches`, `/access-policies/workflows`, `/audit-events` |

Global bench IDs contain a slash, for example `home-lab/esp32-01`. Action and detail routes use a
path-capturing parameter, so preserve that separator in the request path (the CLI does this for
you) and percent-encode unsafe characters within each component. Reservation creation does not
return an active assignment until the Agent confirms the versioned lease. Remote operations can
become `UNKNOWN` during a disconnection and are finalized only by reconciliation or its configured
timeout. See [Phase 5](docs/PHASE_5.md) for these state machines,
[Phase 6](docs/PHASE_6.md) for the principal boundary, and the generated control-plane OpenAPI
contract for exact request/response schemas.

For a Phase 6 principal, identity-backed direct/workflow/CI remote commands persist the initiating
actor and exact decision snapshot. Principal-initiated operation cancellation/reconciliation and
Agent inventory-refresh/drain/undrain requests put matching actor/snapshot evidence on their Agent
control payloads; inconsistent evidence is rejected before dispatch. Principal-requested CI cancel
atomically persists an exact `CI_SESSION` snapshot and actor with `CANCEL_REQUESTED`, validates the
trusted session-to-command/operation/Agent/bench/reservation route, and reuses the actor/evidence
for restart-time delivery retry. A timeout-originated cancellation remains unattributed and cannot
later adopt a caller. An exact idempotent retry is bound to the stable principal, tenant, request
content, and still-allowed required permissions and returns the original accepted work and
snapshot; workflow replay also fingerprints the effective credential
restrictions. A fresh session or route-decision snapshot does not create duplicate work, while
changed content, a different principal, or credential restrictions that remove a required
permission are rejected. System timeouts/automatic maintenance and legacy calls without identity
evidence omit Phase 6 actor/snapshot fields.

Phase 6 artifact list/read/content/upload/transfer/platform-delete operations inherit permissions
from trusted operation, workflow-run, CI-session, command, Agent, and bench relationships. Linked
CI artifacts require session access plus `artifacts:*` on both the workflow and actual bench.
Collections filter inaccessible records; named denials follow the configured hidden-`404` policy.
`WORKFLOW_STEP` parents currently fail closed for a Phase 6 principal, and remote-artifact deletion
is not supported. The transfer-capability content routes remain a separate short-lived bearer
boundary. See [Artifacts](docs/ARTIFACTS.md#phase-6-parent-inherited-access).

### Direct remote actions

These client-facing routes hide Agent routing and always return `202` with a global
`operation_id`, durable `command_id`, and current status:

| Method | Path | Legacy scope / Phase 6 permission and reservation rule |
| --- | --- | --- |
| POST | `/api/v1/benches/{global_id}/actions/probe` | `workflows:run` / `benches:operate`; capability `probe`; no reservation |
| POST | `/api/v1/benches/{global_id}/actions/read-serial` | `workflows:run` / `benches:serial`; capability `serial`; no reservation |
| POST | `/api/v1/benches/{global_id}/actions/reset` | `workflows:run` / `benches:reset`; active confirmed owned lease |
| POST | `/api/v1/benches/{global_id}/actions/flash` | `workflows:run` + `artifacts:write` / `benches:flash` + inherited `artifacts:write`; active confirmed owned lease |

Probe/reset accept JSON `{"owner": "..."}`. Serial read additionally accepts
`timeout_seconds`, `until_pattern`, and `max_lines`. Flash is multipart with `firmware`, `owner`,
and optional `version`. All accept an optional `Idempotency-Key` header. Firmware content is staged
in control-plane artifact storage and delivered to the owning Agent by a fresh short-lived
capability; it is not embedded in durable command payloads or sent over the WebSocket.
For a Phase 6 principal, the server derives reservation/operation ownership from the principal and
does not trust the supplied owner string.

Poll `GET /api/v1/operations/{operation_id}` for the Agent-confirmed terminal result. A successful
serial command exposes its synchronized text through
`GET /api/v1/operations/{operation_id}/artifacts/serial`. Cancellation uses
`POST /api/v1/operations/{operation_id}/cancel` with `owner` and an optional `reason`. The
control plane does not expose the standalone Agent's legacy `power-on`, `power-off`,
`power-cycle`, immediate bench-reservation, queue, bench-timeline, or event-list routes.

### Transport and persistence deployment modes

The checked-in configuration is plaintext HTTP/WS only because both the bind address and public
URL are loopback and `development.allow_insecure_agent_transport` is explicit. For direct TLS,
an HTTPS public URL requires both `control_plane.tls_certificate_path` and
`control_plane.tls_private_key_path`. Same-host TLS termination is an explicit alternative only
when the process binds to loopback and `development.allow_tls_termination_proxy: true`; it is
rejected on non-loopback binds. An HTTPS URL with neither direct certificates nor that constrained
proxy mode is rejected rather than serving misleading plaintext.

The production control-plane store is PostgreSQL. Schema v10 uses composite tenant workflow keys
and organisation-scoped CI/artifact/reservation/queue retry keys. Both `postgresql://` and
`postgresql+psycopg://` configuration spellings are accepted; the latter is normalized to a
Psycopg/libpq `postgresql://` DSN. SQLite remains available for the loopback developer demo, but
is not the recommended central production store. Apply migrations before starting a deployment
with `lab-control-plane migrate --config /path/to/control-plane.yaml`.

## Standalone Agent API and Phase 4 authentication

Machine endpoints use:

```http
Authorization: Bearer <token>
```

Tokens are stored hashed and support scopes, expiry, and revocation. With an empty token store,
the first `POST /api/v1/tokens` request may be unauthenticated but must create a token containing
all seven scopes. Once any token record exists, token creation, listing, and revocation require an
active all-scope bearer. The Agent refuses to revoke the last active all-scope token; maintain a
backup before the remaining administrator expires. This admin-equivalent mechanism is not a full
administrator identity system, so restrict the Agent to a private network. See
[API tokens](docs/API_TOKENS.md).

For a fresh local installation, the pre-Phase-4 operational routes retain their historical
unauthenticated behavior only while the token store is empty. Creating the first token closes that
bootstrap mode: legacy bench, reservation, workflow, operation, event, queue, and timeline routes
then require the corresponding bearer scope, enforce token ownership, and filter collections to
the token owner. Health, version, and OpenAPI remain unauthenticated; token administration does
not, apart from the one-time empty-store creation exception described above.

| Scope | Protected resources |
| --- | --- |
| `benches:read` + `reservations:write` + `ci:sessions` | CI session creation/selection |
| `ci:sessions` | Get, heartbeat, cancel, finalize owned sessions |
| `workflows:run` + `ci:sessions` | Launch an owned session workflow |
| `operations:read` | JSON and JUnit workflow results |
| `artifacts:write` | Upload to an owned session |
| `artifacts:read` | List/download owned session artifacts |

The same individual scopes protect their matching legacy resources after token bootstrap:
`benches:read` for bench metadata, `reservations:write` for reservations and queues,
`workflows:run` for target actions and workflow mutation, and `operations:read` for operation,
workflow-result, event, and timeline reads.

The Agent derives CI `requested_by` from the token owner; the request body cannot impersonate
another owner.

## Standalone Agent endpoint summary

### Agent, benches, operations, and events

| Method | Path | Success |
| --- | --- | --- |
| GET | `/api/v1/health` | Agent/backend/database health |
| GET | `/api/v1/version` | API version |
| GET | `/api/v1/benches` | Filtered `items` collection |
| GET | `/api/v1/benches/{id}` | Bench snapshot |
| POST | `/api/v1/benches/{id}/reservation` | `201` legacy immediate reservation |
| GET | `/api/v1/benches/{id}/reservation` | Active reservation |
| DELETE | `/api/v1/benches/{id}/reservation` | `204` release |
| POST | `/api/v1/benches/{id}/actions/power-on` | `202` operation ID |
| POST | `/api/v1/benches/{id}/actions/power-off` | `202` operation ID |
| POST | `/api/v1/benches/{id}/actions/power-cycle` | `202` operation ID |
| POST | `/api/v1/benches/{id}/actions/flash` | `202` operation ID; multipart |
| POST | `/api/v1/benches/{id}/actions/probe` | Synchronous target health under a lock |
| POST | `/api/v1/benches/{id}/actions/reset` | `202` operation ID |
| POST | `/api/v1/benches/{id}/actions/read-serial` | `202` operation ID |
| GET | `/api/v1/operations` | Filtered operation history |
| GET | `/api/v1/operations/{id}` | Operation and progress |
| GET | `/api/v1/operations/{id}/artifacts` | Operation artifact metadata |
| GET | `/api/v1/operations/{id}/artifacts/serial` | Captured serial text |
| POST | `/api/v1/operations/{id}/cancel` | Current cancellation state |
| GET | `/api/v1/events` | Filtered audit/history events |

### Reservations, queues, and timelines

| Method | Path | Success |
| --- | --- | --- |
| GET | `/api/v1/reservations` | Timed reservation history and filters |
| POST | `/api/v1/reservations` | `201` reservation or queue entry |
| GET | `/api/v1/reservations/{id}` | Reservation record |
| POST | `/api/v1/reservations/{id}/release` | Released reservation |
| POST | `/api/v1/reservations/{id}/extend` | Extended active reservation |
| POST | `/api/v1/reservations/{id}/cancel` | Cancelled scheduled/active reservation |
| GET | `/api/v1/benches/{id}/queue` | FIFO queue entries |
| POST | `/api/v1/benches/{id}/queue` | `201` queue entry |
| DELETE | `/api/v1/queue/{id}` | `204` cancellation |
| GET | `/api/v1/benches/{id}/timeline` | Aggregated bench timeline |

### Workflows and results

| Method | Path | Required machine scope / success |
| --- | --- | --- |
| GET | `/api/v1/workflows` | Stored definitions |
| GET | `/api/v1/workflows/{name}` | Latest/specified definition |
| POST | `/api/v1/workflows/{name}/runs` | `201` workflow run |
| GET | `/api/v1/workflow-runs/{id}` | Run and persisted step results |
| POST | `/api/v1/workflow-runs/{id}/cancel` | Cancellation state |
| GET | `/api/v1/workflow-runs/{id}/results` | `operations:read`; JSON tests |
| GET | `/api/v1/workflow-runs/{id}/results/junit` | `operations:read`; JUnit XML |

### Tokens

| Method | Path | Required authorization / success |
| --- | --- | --- |
| POST | `/api/v1/tokens` | All-scope bearer (except all-scope first-token bootstrap); `201`, plaintext once |
| GET | `/api/v1/tokens` | All-scope bearer; public metadata, never hashes/plaintext |
| POST | `/api/v1/tokens/{token_id}/revoke` | All-scope bearer; revoked metadata, last active admin protected |

### CI sessions and artifacts

| Method | Path | Required scope / success |
| --- | --- | --- |
| POST | `/api/v1/ci/sessions` | `ci:sessions`, `benches:read`, `reservations:write`; `201` |
| GET | `/api/v1/ci/sessions/{id}` | `ci:sessions`; owned details |
| POST | `/api/v1/ci/sessions/{id}/heartbeat` | `ci:sessions`; refreshed heartbeat |
| POST | `/api/v1/ci/sessions/{id}/run` | `ci:sessions`, `workflows:run`; `202` |
| POST | `/api/v1/ci/sessions/{id}/cancel` | `ci:sessions`; cancellation/cleanup request |
| POST | `/api/v1/ci/sessions/{id}/finalize` | `ci:sessions`; idempotent final details |
| GET | `/api/v1/ci/sessions/{id}/artifacts` | `artifacts:read`; owned artifact list |
| POST | `/api/v1/artifacts` | `artifacts:write`; `201` verified upload |
| GET | `/api/v1/artifacts/{id}` | `artifacts:read`; metadata |
| GET | `/api/v1/artifacts/{id}/content` | `artifacts:read`; download |

Polling `GET /api/v1/ci/sessions/{id}` and `GET /api/v1/workflow-runs/{id}` remains the portable
progress mechanism.

## Create and run a CI session

Use an idempotency key when a provider may retry:

```http
POST /api/v1/ci/sessions
Authorization: Bearer <token>
Idempotency-Key: github_actions:acme/device:123456789:1
Content-Type: application/json
```

```json
{
  "provider": "github_actions",
  "external_run_id": "123456789",
  "repository": "acme/device",
  "ref": "refs/pull/42/merge",
  "commit_sha": "abc123",
  "actor": "ci-bot",
  "bench_request": {
    "explicit_bench_id": null,
    "required_capabilities": ["firmware", "serial", "reset", "probe"],
    "required_labels": {"board": "esp32"},
    "preferred_labels": {"location": "simulation"},
    "allow_simulated": true,
    "allow_physical": false,
    "maximum_wait_seconds": 600,
    "reservation_duration_seconds": 1800
  }
}
```

After assignment and artifact upload, launch a registered workflow:

```http
POST /api/v1/ci/sessions/{session_id}/run
Authorization: Bearer <token>
Idempotency-Key: github_actions:acme/device:123456789:workflow
Content-Type: application/json
```

```json
{
  "workflow_name": "esp32-ci-test",
  "inputs": {
    "firmware": {"artifact_id": "f07d3d83-7961-4449-a54c-7091e9404a87"},
    "expected_version": "0.6.0",
    "ready_timeout": 20
  }
}
```

Typed workflows accept `string`, `integer`, `boolean`, and `artifact` inputs. The only allowed
placeholder is `${{ inputs.<name> }}`; unknown inputs and arbitrary expressions fail validation.

## Artifact upload

`POST /api/v1/artifacts` is multipart with `file` and `ci_session_id`, plus optional `name`,
`artifact_type`, and `sha256`. `Idempotency-Key` is an HTTP header. Uploads are streamed,
size-limited, path-normalized, and checksum-verified. See [artifacts](docs/ARTIFACTS.md).

## Human reservation and serial examples

Timed reservation:

```json
{
  "bench_id": "bench-01",
  "owner": "michael",
  "starts_at": null,
  "duration_seconds": 1800,
  "queue_if_busy": false,
  "idempotency_key": "reservation-demo-1"
}
```

Serial read:

```json
{
  "owner": "michael",
  "timeout_seconds": 15,
  "until_pattern": "^READY$",
  "max_lines": 500,
  "include_timestamps": true
}
```

All stored timestamps are UTC. Offset-bearing RFC 3339 input is normalized before persistence.

## Error envelope

Handled errors use one shape:

```json
{
  "error": {
    "code": "BENCH_ALREADY_RESERVED",
    "message": "Bench bench-01 is reserved by another owner.",
    "details": {"bench_id": "bench-01"},
    "request_id": "16fd9449-8257-4303-b324-a93b359db3db"
  }
}
```

Typical statuses are 401 for missing/invalid/expired tokens, 403 for missing scopes or ownership,
404 for missing resources, 409 for state/idempotency conflicts, 413 for oversized request bodies or
artifacts, 422 for request validation, 503 for unavailable backends, and 504 for hardware timeouts. Clients should
record the stable error code and `X-Request-ID`, not sensitive headers.
