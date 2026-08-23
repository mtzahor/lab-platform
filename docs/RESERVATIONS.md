# Reservations

Reservations grant one owner exclusive mutating access to a bench for a bounded interval. They
survive Agent restarts and use UTC internally.

This page documents the standalone Agent's Phase 3 scheduling surface and its distributed control-
plane counterpart. The control plane exposes immediate and future `create`, lease-versioned
`renew`, owner `release`, administrator `revoke`, and Phase 7 durable FIFO bench queues. It does
not expose the standalone `extend`, `cancel`, or immediate `bench reserve/release` aliases. See
[Phase 5](PHASE_5.md#reservation-routing-and-command-safety) and [queueing](QUEUEING.md).

## Immediate reservations

```bash
labctl reservation create bench-01 --owner michael --duration 30m
```

The command activates immediately only when the bench is online and available. Use
`--queue-if-busy` to turn a busy immediate request into a queue entry.

## Future reservations

```bash
labctl reservation create bench-01 \
  --owner michael \
  --start 2026-07-21T10:00:00+02:00 \
  --duration 1h
```

Scheduled intervals for the same bench cannot overlap. Offset-bearing RFC 3339 input is converted
to UTC before persistence.

The distributed API uses the same concept through `POST /api/v1/reservations`:

```json
{
  "bench_id": "home-lab/virtual-esp32-01",
  "idempotency_key": "calendar:change-4821",
  "starts_at": "2026-07-21T08:00:00Z",
  "reservation_duration_seconds": 3600,
  "description": "Release-candidate validation"
}
```

`starts_at` and `description` are top-level request fields. The response and list endpoint report
`status: SCHEDULED`; there is no active Agent lease or lease version until the control-plane
maintenance loop activates the due slot. Dashboard cancellation calls owner `release` or
administrator `revoke` without an expected lease version. Once activation succeeds, ordinary
lease-version fencing applies.

## Inspecting reservations

```bash
labctl reservation list
labctl reservation list --owner michael --status active
labctl reservation show <reservation-id>
```

## Extending, releasing, and cancelling

```bash
labctl reservation extend <reservation-id> --owner michael --duration 15m
labctl reservation release <reservation-id> --owner michael
labctl reservation cancel <reservation-id> --owner michael
```

Only the owner may mutate a reservation. Extension applies to an active reservation and fails when
it would exceed the configured maximum duration or overlap the next scheduled reservation.
Releasing an already released reservation is idempotent.

A distributed workflow launch can reference the caller's existing active coordinated reservation.
The request must explicitly choose whether terminal/pre-dispatch cleanup releases that reservation;
omitting a reservation retains the original managed-grant behavior. Ownership, bench, tenant,
active state, and lease validity are revalidated by the workflow service.

Cancellation is for queued or scheduled work; release is for an active reservation. Phase 1
commands remain aliases where practical:

```bash
labctl bench reserve bench-01 --owner michael
labctl bench release bench-01 --owner michael
```

## Idempotency

API clients should provide `idempotency_key` when retrying reservation creation. Reusing a key
returns the original result and never creates a duplicate reservation.

## Expiry during an operation

If time ends while a mutating operation is running, the reservation becomes release-pending. New
operations are blocked while the current operation receives the configured grace period. Completion
releases the reservation; an overrun requests safe cancellation and degrades the bench until probe.
