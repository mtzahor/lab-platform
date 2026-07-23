# Scheduling

The Agent runs a small in-process scheduling worker. The scheduling logic is also exposed as
independent service methods so tests can invoke it without starting a worker or sleeping.

Each scheduler tick performs idempotent work:

1. activate due scheduled reservations when their bench is online and free;
2. expire active reservations whose end time has passed;
3. promote eligible FIFO queue entries.

Database transactions serialize transitions for each SQLite database. Repeating a tick at the same
clock value does not duplicate activations, expirations, promotions, or transition events.

## Configuration

```yaml
reservations:
  default_duration_minutes: 30
  maximum_duration_minutes: 240
  expiry_grace_seconds: 30
  scheduled_protection_window_minutes: 5
  queue_enabled: true

scheduler:
  poll_interval_seconds: 1
  automatic_assignment: true
```

## Time rules

- Persisted timestamps are aware UTC datetimes.
- API input accepts RFC 3339 timestamps with offsets.
- CLI durations accept `30m`, `2h`, and compound expressions such as `1h30m`.
- Tests inject a fake clock and advance it directly.
- Scheduler tests must not rely on real wall-clock sleeps.

## Scheduled protection

A queued request is eligible only when its complete duration fits before the next scheduled start,
including the configured protection window. Partial queue reservations are not supported.

## Manual processing

Applications and tests can call `process_due_reservations`, `expire_reservations`, and
`promote_queues` separately. Production startup recovery runs the same reconciliation once before
the periodic worker begins.
