# Restart Recovery

This page documents standalone Agent startup recovery. Phase 5 adds a second, distributed recovery
layer: heartbeat loss makes global benches offline and remote operations/reservation leases
`UNKNOWN`; the control plane retains ownership for bounded grace/reconciliation periods instead of
guessing success or failure. Reconnect compares the Agent boot ID, durable command journal, local
leases, current inventory, and acknowledged event-buffer watermark. See
[Phase 5 disconnect, restart, and reconciliation](PHASE_5.md#disconnect-restart-and-reconciliation).

The Agent reconciles persisted state before accepting new operations. Recovery is idempotent and
records a report and timeline events.

Each attempt creates a durable `recovery_records` row before reconciliation starts and fills in
the completed report afterward. An incomplete row therefore remains available when the process
stops during recovery.

Startup recovery performs these actions:

- marks pending, running, and cancel-requested operations failed with `AGENT_RESTARTED`;
- removes operation locks that no longer belong to a live operation;
- expires overdue reservations;
- activates valid due scheduled reservations;
- preserves queue ordering and promotes only eligible entries;
- refreshes SimLab status and re-probes physical benches;
- reconciles the bench catalog without replacing platform-owned labels;
- marks interrupted workflow runs failed.

## Operation locks

Each mutating operation acquires a persistent lock containing the bench ID and operation ID. Normal
success, failure, and cancellation release it. Recovery removes stale locks only after determining
that their operation is no longer live.

## Physical safety

Reservation expiry does not silently terminate an unsafe physical operation. The Agent blocks new
work, grants the configured grace period, requests cancellation only where safe, and degrades a
bench after an overrun. A successful probe is required before new work.

## Failure isolation

A backend that cannot start or refresh is reported unavailable. Other registered backends and their
benches continue serving traffic. A missing or failed physical connection must therefore not prevent
SimLab scheduling and recovery.

## Inspecting recovery

Use the bench timeline for affected resources and the event API for system-wide records:

```bash
labctl bench timeline bench-01 --category system
labctl event list --event-type RECOVERY_COMPLETED
```
