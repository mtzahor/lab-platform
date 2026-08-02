# CI cleanup and cancellation

Every CI session has a persisted cleanup status and result. Cleanup is mandatory after workflow
success, test failure, cancellation, timeout, missed heartbeat, client disappearance, or recovery
from an Agent restart.

## Cleanup contract

The Agent attempts to:

1. Stop or cancel an active workflow when required.
2. Let an unsafe in-flight hardware action reach a safe cancellation boundary.
3. Confirm serial sessions closed and complete serial output was flushed.
4. Release and verify the persistent operation lock.
5. Release the CI reservation.
6. Finalize artifact records.
7. Persist each cleanup flag and any errors.
8. Set `cleanup_status` and finalize the session as `completed`.

The cleanup result records `reservation_released`, `workflow_stopped`, `locks_released`,
`serial_closed`, `artifacts_finalized`, and an error list. Repeating finalization is safe and
returns the existing result.

## Outcome is separate from cleanup

The workflow result and cleanup result are intentionally independent:

| Workflow | Cleanup | Overall outcome |
| --- | --- | --- |
| passed | succeeded | `succeeded` |
| failed assertion | succeeded | `failed` |
| cancelled | succeeded | `cancelled` |
| timed out | succeeded | `timed_out` |
| passed or failed | failed | `infrastructure_error` |

A passed workflow with a leaked reservation is never reported as success. `labctl ci run` returns
exit 18 for cleanup failure and still downloads available diagnostics.

## Client cancellation

`labctl ci run` installs `SIGINT` and `SIGTERM` handlers. On cancellation it:

1. posts `POST /api/v1/ci/sessions/{session_id}/cancel`;
2. stops the heartbeat worker;
3. allows the Agent to cancel the workflow;
4. waits briefly for cleanup;
5. posts the idempotent `finalize` request; and
6. returns exit 16 when cleanup succeeds, or exit 18 when cleanup fails.

Manual sequence:

```console
labctl ci session cancel SESSION_ID
labctl ci session watch SESSION_ID
labctl ci session finalize SESSION_ID
```

Provider wrappers must forward cancellation signals to `labctl`; they must not simply abandon a
child process. GitHub, GitLab, and Jenkins examples do this or rely on normal shell signal
propagation.

## Server-side fallback

Client cleanup is an optimization, not the sole guarantee. A background reaper detects a missing
heartbeat after `heartbeat_timeout_seconds` (120 by default), marks the session timed out, cancels
active work, and performs the same cleanup. This covers runner termination, network loss, and
machines being powered off.

On Agent restart, persisted sessions/reservations/locks are reconciled before accepting new work.
Active work is not blindly resumed. Recovery records stable interruption diagnostics and releases
resources when safe.

## Separate timeouts

```yaml
ci:
  heartbeat_timeout_seconds: 120
  session_timeout_seconds: 3600
  workflow_timeout_seconds: 1800
  step_timeout_seconds: 600
  cleanup_timeout_seconds: 60
```

The cleanup timeout bounds a single finalization attempt. A failed attempt remains persisted and
visible for diagnosis; it must not rewrite a successful test into a generic test failure. It
becomes an infrastructure error.

## Verification

After any result, confirm the session and bench:

```console
labctl ci session show SESSION_ID --output json
labctl bench show BENCH_ID --output json
```

Expected final fields include `status: completed`, a terminal `outcome`, and
`cleanup_status: succeeded`. The bench should have no active CI reservation or operation lock.
When connected directly to a standalone Agent, its additional local audit view is:

```console
labctl event list --event-type CI_CLEANUP_COMPLETED --output json
```

The control plane has no global `/events` compatibility route; use its persisted session details,
operation records, reservation history, and `labctl agent timeline AGENT_ID` for distributed
diagnosis.

For cancellation testing, start a workflow with a long wait step, send `SIGINT`, then perform these
checks. For missed-heartbeat testing, terminate the client without a post-job hook only in an
isolated SimLab environment and wait beyond the configured heartbeat timeout.

## Audit events

Cleanup emits `CI_CLEANUP_STARTED`, `CI_CLEANUP_COMPLETED`, or `CI_CLEANUP_FAILED`. Related session
events include cancellation, timeout, missed heartbeat, and finalization. Use request IDs and the
session UUID to correlate records; do not include bearer tokens in diagnostic payloads.
