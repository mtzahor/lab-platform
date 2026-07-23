# Reservation Queueing

Queueing lets an owner wait for a busy or offline bench without racing other users. Queue entries
are persistent and ordered FIFO per bench.

```bash
labctl reservation queue bench-01 --owner alice --duration 20m
labctl reservation queue-list bench-01
labctl reservation queue-cancel <queue-entry-id> --owner alice
```

## Promotion rules

When a bench becomes available, the scheduler atomically:

1. verifies that no active reservation exists;
2. verifies that the bench is online;
3. checks the next scheduled reservation and protection window;
4. selects the first waiting entry by creation time and serialized insertion order;
5. creates its active reservation;
6. marks the queue entry promoted;
7. records transition events.

If the first valid entry does not fit before the next scheduled reservation, it remains waiting. The
scheduler does not shorten the requested duration or skip ahead for a smaller request.

## Fairness and concurrency

FIFO is the only Phase 3 policy. The selection policy is behind a protocol so later phases can add
other policies without changing reservation services. Queue promotion is transactionally serialized;
concurrent scheduler calls cannot promote two owners onto one bench.

## Offline benches

Entries remain in place while a bench is offline and retain their order through restarts. Reconnect
and catalog refresh make the bench eligible at the next scheduler tick.

## Idempotent requests

Supply an idempotency key through the API when a client may retry. The same key returns the existing
entry rather than changing queue order.
