# Reservations

Reservations grant one owner exclusive mutating access to a bench for a bounded interval. They
survive Agent restarts and use UTC internally.

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
