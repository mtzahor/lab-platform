# REST API

The Agent serves interactive Swagger documentation at `/docs`, ReDoc at `/redoc`, and the OpenAPI
document at `/openapi.json`. Phase 1 endpoints use the `/api/v1` prefix.

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
| GET | `/api/v1/operations` | Filtered operation history |
| GET | `/api/v1/operations/{id}` | Operation and progress |
| POST | `/api/v1/operations/{id}/cancel` | Current cancellation state |
| GET | `/api/v1/events` | Filtered historical events |

Owner requests use `{"owner":"demo-user"}`. Firmware upload uses the multipart fields `owner`,
optional `version`, and `firmware`.

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
oversized firmware 413, validation failures 422, and backend failures 503.
