# Reservation Queueing

Queueing lets an owner wait for a busy or offline bench without racing other users. Queue entries
are persistent and ordered FIFO per bench.

The standalone Agent retains its Phase 3 queue API and `labctl reservation queue*` commands:

```bash
labctl reservation queue bench-01 --owner alice --duration 20m
labctl reservation queue-list bench-01
labctl reservation queue-cancel <queue-entry-id> --owner alice
```

Phase 7 also adds an organisation-scoped durable FIFO queue to the distributed control plane. The
browser uses these generic REST routes:

```text
GET    /api/v1/benches/{global_bench_id}/queue
POST   /api/v1/benches/{global_bench_id}/queue
DELETE /api/v1/queue/{queue_entry_id}
```

The response includes the current position, expected availability when known, and whether the
authenticated caller may cancel the entry. An identity principal can see/cancel its own entry;
legacy-token compatibility remains owner bound. Queue creation requires the same effective bench
reservation permission as a direct grant. The control plane does not expose another caller's
identity merely to describe queue order.

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

The distributed control-plane maintenance loop applies the same FIFO head rule per organisation
and global bench. Promotion creates an authoritative coordinated lease and marks the queue row
promoted only after the Agent confirms it `ACTIVE`. A stable grant idempotency key closes the
restart window between lease creation and queue transition. `UNKNOWN`, offline, incompatible, or
otherwise unavailable grants leave the entry waiting for reconciliation rather than claiming that
the caller owns the bench.

## Fairness and concurrency

FIFO is the only Phase 3 policy. The selection policy is behind a protocol so later phases can add
other policies without changing reservation services. Queue promotion is transactionally serialized;
concurrent scheduler calls cannot promote two owners onto one bench.

The Phase 7 central queue serializes each maintenance promotion pass, attempts only the FIFO head
for a tenant/bench, and relies on coordinated reservation idempotency and bench fencing across
workers/restarts. A cancellation that wins the narrow grant-to-mark race triggers a compensating
release so cancelled work does not silently retain ownership.

## Offline benches

Entries remain in place while a bench is offline and retain their order through restarts. Reconnect
and catalog refresh make the bench eligible at the next scheduler tick.

## Idempotent requests

Supply an idempotency key through the API when a client may retry. The same key returns the existing
entry rather than changing queue order.
