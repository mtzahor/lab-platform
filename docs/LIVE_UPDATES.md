# Live updates

The dashboard treats REST responses as authoritative and live events as an invalidation signal.
This keeps reconnection and duplicate delivery safe: receiving an event schedules a fresh query;
it does not replay a browser-side business transition.

## Event stream

When `web.live_updates.sse_enabled` is enabled, an authenticated browser opens:

```http
GET /api/v1/events
Accept: text/event-stream
Cookie: lab_session=...
```

The stream publishes `overview.snapshot` followed by `overview.updated` whenever the visible
overview digest changes. Each envelope contains an event ID, type, timestamp, and authorized
snapshot data. Named `heartbeat` events keep an otherwise idle connection observable without
invalidating resource queries. Responses disable proxy buffering and shared caching.

`EventSource` reconnects automatically and sends `Last-Event-ID` where supported. The current
channel is not a durable event journal: after any disconnect the client refetches affected REST
resources. Duplicate, delayed, or skipped events therefore cannot manufacture a resource state.

## Client states

| Indicator | Meaning | Resource behavior |
| --- | --- | --- |
| Live | EventSource is open and events/heartbeats are current | events invalidate cached queries |
| Reconnecting | a recent connection failed | last server state retained; no success/failure inference |
| Polling | repeated SSE failure or EventSource unavailable | key queries refetch at bounded intervals |
| Stale | no trustworthy update within the freshness window | last-known data remains labelled stale |

Collection and detail queries use five-to-fifteen-second fallback intervals, depending on resource
cost. Only visible/mounted queries poll. The overview endpoint aggregates related state to avoid a
request waterfall. Production proxies must not buffer `/api/v1/events`.

## Operation and serial output

Operation state stays in the operation API. `UNKNOWN` means the outcome is unknown;
`RECONCILING` means the control plane is actively resolving it. Neither is mapped to running,
failed, or succeeded.

Serial output uses cursor windows:

```http
GET /api/v1/operations/{operation_id}/serial?cursor=0&limit=500
```

The response includes `next_cursor`, `has_more`, operation status, connection state, and text
lines. The UI caps its in-memory buffer, pauses only rendering/follow behavior (not the server
operation), and escapes every line as text. Completed runs can be downloaded through the
authorized artifact route.

## Failure and recovery tests

Exercise these without physical hardware:

- stop and restart a simulated Agent;
- force an operation to `UNKNOWN`, then complete reconciliation;
- disconnect SSE while REST remains available and observe polling;
- restore SSE and confirm a full authoritative refetch;
- deliver the same invalidation twice;
- delay an event beyond a newer REST response;
- expire a reservation while its bench page is open;
- stream more lines than the browser buffer limit.

The correct outcome is always understandable uncertainty, bounded resource use, and eventual
agreement with the control plane—never a browser-invented terminal result.
