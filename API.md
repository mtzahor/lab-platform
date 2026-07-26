# REST API

The Agent exposes Swagger at `/docs`, ReDoc at `/redoc`, and OpenAPI at `/openapi.json`. Public
resources use the `/api/v1` prefix. Every response includes `X-Request-ID`.

## Phase 4 authentication

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

## Endpoint summary

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
    "allow_physical": true,
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
    "expected_version": "0.5.0",
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
