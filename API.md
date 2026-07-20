# REST API

The Agent serves interactive Swagger documentation at `/docs`, ReDoc at `/redoc`, and the OpenAPI
document at `/openapi.json`. Endpoints use the `/api/v1` prefix.

| Method | Path | Success |
| --- | --- | --- |
| GET | `/api/v1/health` | Agent/backend/database health |
| GET | `/api/v1/version` | API version |
| GET | `/api/v1/benches` | `items` collection; filters: `status`, `capability`, `reserved` |
| GET | `/api/v1/benches/{id}` | Bench snapshot |
| POST | `/api/v1/benches/{id}/reservation` | `201` reservation |
| GET | `/api/v1/benches/{id}/reservation` | Active reservation |
| DELETE | `/api/v1/benches/{id}/reservation` | `204` |
| POST | `/api/v1/benches/{id}/actions/power-on` | `202` operation ID |
| POST | `/api/v1/benches/{id}/actions/power-off` | `202` operation ID |
| POST | `/api/v1/benches/{id}/actions/power-cycle` | `202` operation ID |
| POST | `/api/v1/benches/{id}/actions/flash` | `202` operation ID; multipart |
| POST | `/api/v1/benches/{id}/actions/probe` | Physical/simulated target health; no reservation |
| POST | `/api/v1/benches/{id}/actions/reset` | `202` operation ID |
| POST | `/api/v1/benches/{id}/actions/read-serial` | `202` operation ID |
| GET | `/api/v1/operations` | Filtered operation history |
| GET | `/api/v1/operations/{id}` | Operation and progress |
| GET | `/api/v1/operations/{id}/artifacts` | Operation artifact metadata |
| GET | `/api/v1/operations/{id}/artifacts/serial` | Captured serial text |
| POST | `/api/v1/operations/{id}/cancel` | Current cancellation state |
| GET | `/api/v1/events` | Filtered historical events |

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

Probe is read-only. Flash, reset, and serial read use the same reservation ownership and per-bench
operation lock as power actions.

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
