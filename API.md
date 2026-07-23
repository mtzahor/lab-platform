# REST API

The Agent serves interactive Swagger documentation at `/docs`, ReDoc at `/redoc`, and the OpenAPI
document at `/openapi.json`. Endpoints use the `/api/v1` prefix.

| Method | Path | Success |
| --- | --- | --- |
| GET | `/api/v1/health` | Agent/backend/database health |
| GET | `/api/v1/version` | API version |
| GET | `/api/v1/benches` | `items`; status/capability/reserved/online/available/label filters |
| GET | `/api/v1/benches/{id}` | Bench snapshot |
| POST | `/api/v1/benches/{id}/reservation` | `201` reservation |
| GET | `/api/v1/benches/{id}/reservation` | Active reservation |
| DELETE | `/api/v1/benches/{id}/reservation` | `204` |
| POST | `/api/v1/benches/{id}/actions/power-on` | `202` operation ID |
| POST | `/api/v1/benches/{id}/actions/power-off` | `202` operation ID |
| POST | `/api/v1/benches/{id}/actions/power-cycle` | `202` operation ID |
| POST | `/api/v1/benches/{id}/actions/flash` | `202` operation ID; multipart |
| POST | `/api/v1/benches/{id}/actions/probe` | Target health under an owner/maintenance lock |
| POST | `/api/v1/benches/{id}/actions/reset` | `202` operation ID |
| POST | `/api/v1/benches/{id}/actions/read-serial` | `202` operation ID |
| GET | `/api/v1/operations` | Filtered operation history |
| GET | `/api/v1/operations/{id}` | Operation and progress |
| GET | `/api/v1/operations/{id}/artifacts` | Operation artifact metadata |
| GET | `/api/v1/operations/{id}/artifacts/serial` | Captured serial text |
| POST | `/api/v1/operations/{id}/cancel` | Current cancellation state |
| GET | `/api/v1/events` | Filtered historical events |
| GET | `/api/v1/reservations` | Timed reservation history and filters |
| POST | `/api/v1/reservations` | `201` active/scheduled reservation or queue entry |
| GET | `/api/v1/reservations/{id}` | Reservation record |
| POST | `/api/v1/reservations/{id}/release` | Released reservation |
| POST | `/api/v1/reservations/{id}/extend` | Extended active reservation |
| POST | `/api/v1/reservations/{id}/cancel` | Cancelled scheduled/active reservation |
| GET | `/api/v1/benches/{id}/queue` | Waiting FIFO entries |
| POST | `/api/v1/benches/{id}/queue` | `201` queue entry |
| DELETE | `/api/v1/queue/{id}` | `204` |
| GET | `/api/v1/benches/{id}/timeline` | Aggregated bench timeline |
| GET | `/api/v1/workflows` | Stored workflow definitions |
| GET | `/api/v1/workflows/{name}` | Latest/specified workflow definition |
| POST | `/api/v1/workflows/{name}/runs` | `201` workflow run |
| GET | `/api/v1/workflow-runs/{id}` | Run plus persisted step results |
| POST | `/api/v1/workflow-runs/{id}/cancel` | Current cancellation state |

Owner requests use `{"owner":"demo-user"}`. Firmware upload uses the multipart fields `owner`,
optional `version`, and `firmware`.

Serial read requests use:

```json
{
  "owner": "michael",
  "timeout_seconds": 15,
  "until_pattern": "^READY$",
  "max_lines": 500,
  "include_timestamps": true
}
```

Probe may reset a physical target, so an online probe requires the active reservation owner and
uses the same per-bench operation lock as flash, reset, serial read, and power actions. An offline
recovery probe uses a maintenance lock so reconnect can be verified before a reservation activates.

Timed reservation creation uses:

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

All stored timestamps are UTC. Offset-bearing RFC 3339 input is normalized before persistence.
Workflow inputs perform literal `${name}` substitution only; they are never evaluated as code.

## Error envelope

Every handled API error has this shape and every response includes `X-Request-ID`:

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

Not-found errors use 404, ownership failures 403, state conflicts 409, invalid firmware 400,
oversized firmware 413, validation failures 422, hardware timeouts 504, and unavailable backend or
device failures 503. Hardware adapters return stable codes such as `DEVICE_NOT_FOUND`,
`SERIAL_PORT_AMBIGUOUS`, `ESPTOOL_FLASH_FAILED`, and `BOOT_TIMEOUT`.
