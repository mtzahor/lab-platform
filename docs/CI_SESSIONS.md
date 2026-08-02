# CI sessions

A CI session is the durable coordinator for one external CI attempt. It is separate from a bench
reservation, workflow run, or individual operation because it must recover and clean up all of
them as one unit.

In Phase 5 the same public aggregate is hosted by the control plane. Its durable workflow binding
points to a global bench, confirmed Agent lease, remote command, and distributed operation. The
owning Agent runs the complete sequential workflow locally; a CI provider never calls an Agent
directly. The standalone behavior described below remains the Phase 4 compatibility mode. See
[Phase 5](PHASE_5.md#workflows-ci-and-artifacts) for disconnect and lease semantics.

```text
CI session
|- provider and external run metadata
|- bench request and selected bench
|- reservation and heartbeat
|- input and output artifacts
|- workflow run and step results
`- outcome and cleanup record
```

## State and outcome

The normal status path is:

```text
created -> waiting_for_bench -> reserved -> running
        -> succeeded | failed | cancelled | timed_out
        -> cleanup_pending -> completed
```

Cancellation may pass through `cancel_requested`. `status=completed` means lifecycle processing is
finished; it does not mean the test passed. Read `outcome`, whose terminal values are `succeeded`,
`failed`, `cancelled`, `timed_out`, and `infrastructure_error`. Read `cleanup_status` separately.

## Creating a session

The one-command path is preferred for CI jobs:

```console
labctl ci run \
  --workflow esp32-ci-test \
  --artifact firmware=build/firmware.bin \
  --input expected_version=0.6.0 \
  --require capability=firmware \
  --require capability=serial \
  --require capability=reset \
  --require capability=probe \
  --label board=esp32 \
  --agent-label environment=development \
  --preferred-location simulation \
  --allow-simulated \
  --no-allow-physical \
  --wait-timeout 10m \
  --reservation-duration 30m
```

For debugging or custom orchestration, use the lower-level commands:

```console
labctl ci session create \
  --external-run-id local-42 \
  --require capability=firmware \
  --label board=esp32 \
  --allow-simulated \
  --no-allow-physical
labctl ci session show SESSION_ID
labctl ci session watch SESSION_ID
labctl ci session cancel SESSION_ID
labctl ci session finalize SESSION_ID
```

Finalization is idempotent. Repeating it returns the existing finalized session instead of
performing cleanup twice.

For a distributed run, finalization can temporarily return `cleanup_pending` while the control
plane retries checksum-verified output uploads from the owning Agent. `labctl ci run` keeps polling
and does not download results until the session is `completed`. The retry state survives a control
plane restart. If an upload is still incomplete after
`artifacts.finalization_timeout_seconds`, cleanup fails boundedly and the final outcome is
`infrastructure_error`; it is never reported as a successful cleanup with missing content.

## REST flow

The standalone Agent's session-creation route requires `ci:sessions`, `benches:read`, and
`reservations:write`. The Phase 5 control-plane route requires `ci:sessions`; its central service
enforces selection and lease safety. Across the complete one-command flow, a standalone Agent job
uses all seven local scopes. A distributed `labctl ci run` uses `ci:sessions`, `artifacts:write`,
`operations:read`, and `artifacts:read` to create/run the session, upload its inputs, read results,
and download outputs; it does not need Agent-administration scopes.

```http
POST /api/v1/ci/sessions
Authorization: Bearer <token>
Idempotency-Key: github_actions:acme/device:123456789:1
Content-Type: application/json
```

```json
{
  "provider": "github_actions",
  "external_run_id": "123456789",
  "repository": "acme/device",
  "ref": "refs/pull/42/merge",
  "commit_sha": "abc123",
  "actor": "ci-bot",
  "bench_request": {
    "required_capabilities": ["firmware", "serial", "reset", "probe"],
    "required_labels": {"board": "esp32"},
    "preferred_labels": {"location": "simulation"},
    "required_agent_labels": {"environment": "development"},
    "preferred_location": "simulation",
    "allow_simulated": true,
    "allow_physical": false,
    "maximum_wait_seconds": 600,
    "reservation_duration_seconds": 1800
  }
}
```

The server derives `requested_by` from the authenticated token; a client cannot impersonate a
different owner. Continue with:

| Method | Route | Purpose |
| --- | --- | --- |
| GET | `/api/v1/ci/sessions/{session_id}` | Inspect and synchronize lifecycle state |
| POST | `/api/v1/ci/sessions/{session_id}/heartbeat` | Keep a live reservation healthy |
| POST | `/api/v1/ci/sessions/{session_id}/run` | Launch a registered workflow |
| POST | `/api/v1/ci/sessions/{session_id}/cancel` | Request cancellation and cleanup |
| POST | `/api/v1/ci/sessions/{session_id}/finalize` | Idempotently complete cleanup |
| GET | `/api/v1/ci/sessions/{session_id}/artifacts` | List session artifacts |

Workflow launch accepts typed inputs. Artifact inputs use a record returned by the artifact upload
API:

```json
{
  "workflow_name": "esp32-ci-test",
  "inputs": {
    "firmware": {"artifact_id": "f07d3d83-7961-4449-a54c-7091e9404a87"},
    "expected_version": "0.6.0",
    "ready_timeout": 20
  }
}
```

## Bench selection

An explicit `--bench` takes precedence. Otherwise candidates are filtered by online state,
required capabilities, required labels, and allowed backend kind. Distributed selection also
requires matching Agent labels and excludes offline or draining Agents. Available candidates are
ranked before busy ones, then by preferred location/labels, Agent load, least-recent use, and bench
ID. The ordering is deterministic and central reservation creation is confirmed by a versioned
Agent lease, so two sessions cannot acquire the same bench.

With `maximum_wait_seconds: 0`, failure to reserve a matching available bench on the first attempt
is reported as immediate selection failure; that result cannot distinguish an incompatible pool
from compatible benches that are busy. With a positive duration, selection retries until the
bench-wait deadline, whose timeout likewise does not necessarily distinguish busy from
incompatible. Simulated benches are allowed by default, while physical selection requires an
explicit `--allow-physical`. Use `--no-allow-simulated --allow-physical` with an explicit bench
for gated hardware runs. At least one backend kind must be allowed.

## Provider metadata

`labctl ci run` detects metadata from the runner environment:

| Provider | Detection and common values |
| --- | --- |
| GitHub Actions | `GITHUB_ACTIONS`, `GITHUB_RUN_ID`, `GITHUB_REPOSITORY`, `GITHUB_SHA`, `GITHUB_REF`, `GITHUB_ACTOR` |
| GitLab CI | `GITLAB_CI`, `CI_PIPELINE_ID`, `CI_PROJECT_PATH`, `CI_COMMIT_SHA`, `CI_COMMIT_REF_NAME`, `GITLAB_USER_LOGIN` |
| Jenkins | `JENKINS_URL`, `BUILD_ID`, `JOB_NAME`, `GIT_COMMIT`, `BUILD_USER_ID` |
| Local | explicit flags or generated local run ID |

Detection belongs to the CLI integration layer. The persisted provider is metadata; it does not
change scheduling or workflow semantics.

## Heartbeats and abandoned clients

While waiting, reserved, or running, the CLI posts a heartbeat every 30 seconds by default. If no
heartbeat is seen for 120 seconds, the Agent marks the session abandoned, requests workflow
cancellation, waits for safe operation completion, releases the reservation and locks, records
cleanup, and finalizes the outcome. The server-side reaper provides this guarantee even if the
runner process disappears.

The heartbeat worker is cancellable and stops before finalization. Do not implement an external
client by keeping a reservation alive without also finalizing the session.

## Cancellation

`labctl ci run` handles `SIGINT` and `SIGTERM`. It requests session cancellation, stops its
heartbeat, requests workflow cancellation, waits briefly for server cleanup, requests finalization,
and exits with code 16. If a custom runner cannot execute a post-job hook, the missed-heartbeat
reaper still cleans the session.

Manual cancellation:

```console
labctl ci session cancel SESSION_ID
labctl ci session watch SESSION_ID
labctl ci session finalize SESSION_ID
```

See [cleanup guarantees](CI_CLEANUP.md) for the exact responsibilities.

## Timeouts

Bench wait, session, workflow, step, heartbeat, and cleanup timeouts are configured separately.
Their defaults are 600, 3600, 1800, 600, 120, and 60 seconds respectively. A session heartbeat
does not allow a workflow or individual step to exceed its own limit, and a reservation cannot be
extended beyond `maximum_reservation_minutes`.

## Idempotency

Use unique idempotency keys for session creation, artifact upload, workflow launch, and
finalization. A useful session key is:

```text
<provider>:<repository>:<external-run-id>:<attempt>
```

Reuse the same key only when retrying the same logical request. Repeated requests return the
existing resource instead of creating duplicate reservations or workflow runs.
