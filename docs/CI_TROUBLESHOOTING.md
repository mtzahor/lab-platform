# CI troubleshooting

Start with the command's stable exit code, then inspect the CI session and downloaded artifacts:

```console
labctl ci session show SESSION_ID --output json
labctl ci artifacts SESSION_ID --output json
labctl workflow results WORKFLOW_RUN_ID --format json
```

## CI exit codes

| Code | Meaning | First checks |
| ---: | --- | --- |
| 0 | success | Session completed and cleanup succeeded |
| 10 | workflow failed | Step results and workflow summary |
| 11 | hardware test failed | Assertion details and serial log |
| 12 | immediate zero-wait selection failed | Compatibility filters and current availability |
| 13 | positive bench-wait deadline elapsed | Filters, queue/reservations, and `--wait-timeout` |
| 14 | authentication failed | Token environment, expiry, revocation, scopes |
| 15 | artifact upload failed | File, size limit, checksum, write scope |
| 16 | workflow cancelled | Runner/user cancellation and cleanup status |
| 17 | session timed out | Heartbeats and session/workflow timeout settings |
| 18 | cleanup failed | Reservation, lock, serial close, finalization errors |
| 19 | backend unavailable | Agent health and physical/SimLab backend state |
| 20 | client or protocol error | Server URL, API version, response/request IDs |

Earlier non-CI commands retain their existing exit codes; use the 10–20 range to make CI job
policy explicit.

## Authentication failed (14)

- Confirm `LAB_PLATFORM_TOKEN` is set in the same process without printing it.
- If using `--token-env`, confirm the named environment variable exists.
- Check `labctl token list` from the trusted management interface for expiry/revocation.
- Grant the least required missing scope. A standalone Agent `ci run` uses all seven local Phase 4
  scopes. A control-plane run uses `ci:sessions`, `artifacts:write`, `operations:read`, and
  `artifacts:read`; Agent-administration scopes are not job scopes.
- Confirm the runner is talking to the intended control plane or standalone Agent and TLS endpoint.

Never retry by moving the token onto a command-line option or enabling shell tracing.

## Immediate selection failed (12)

Exit 12 means a request with a zero bench-wait duration could not reserve a matching available
bench on its first attempt. It does not prove that no compatible bench exists: the pool can be
structurally incompatible, offline, excluded by policy, or compatible but currently busy.

List candidates with the same filters:

```console
labctl bench list \
  --online \
  --label board=esp32 \
  --output json
```

Inspect each candidate's capabilities for `firmware`, `serial`, `reset`, and `probe`. Both backend
kinds are allowed by default; make sure the corresponding `--no-allow-*` flag has not excluded the
desired kind. An explicit bench still must be online and satisfy the workflow's declared
requirements. Label comparisons are exact.

## Bench wait deadline elapsed (13)

Exit 13 means a request with a positive wait duration reached the client/server bench-selection
deadline without acquiring a reservation. The terminal result does not necessarily distinguish a
compatible-but-busy pool from filters for which no compatible online bench exists. Recheck the
same capabilities, labels, explicit bench, and backend allow flags before inspecting timelines and
reservations:

```console
labctl reservation list --bench-id BENCH_ID
labctl operation list --bench-id BENCH_ID
```

For a distributed bench, also inspect `labctl agent timeline AGENT_ID`; find the Agent ID in
`labctl bench show BENCH_ID --output json`. `labctl bench timeline` is a standalone Agent
compatibility route and returns `404` when the CLI is pointed at the control plane.

Increase `--wait-timeout` only when pipeline latency permits it. Do not inflate session or workflow
timeouts to solve queue pressure.

## Hardware test failed (11)

Download `serial.log`, probe output, and flash logs. Verify that the firmware emits full-line
markers matching the example workflow:

```text
READY
SELF_TEST=PASS
FIRMWARE_VERSION=<expected-version>
```

Patterns are regular expressions; anchors and case matter. Confirm the supplied
`expected_version` matches what was flashed. A test failure is different from backend error 19.

## Artifact upload failed (15)

- Confirm the path exists, is a readable non-empty regular file, and is within the client and
  server size limits.
- Verify the token has `artifacts:write` and owns the target session.
- If sending `sha256`, recompute it and retry with the same idempotency key.
- Use a simple logical name; never depend on directory components in an uploaded filename.
- Check reverse-proxy request-body limits when the service's configured limit is higher.

## Cancellation or timeout (16/17)

Inspect `heartbeat_at`, `timeout_at`, workflow status, and cleanup status. Network interruption can
produce a missed heartbeat even when the runner process remains alive. Keep
`heartbeat_interval_seconds` comfortably below `heartbeat_timeout_seconds`; the defaults are 30
and 120 seconds.

Do not use one global timeout. Bench wait, session, workflow, step, heartbeat, and cleanup limits
have separate meanings.

## Cleanup failed (18)

Treat this as an infrastructure incident even if all tests passed. Inspect the persisted cleanup
result and `CI_CLEANUP_FAILED` event, then verify reservation and lock state. Repeating:

```console
labctl ci session finalize SESSION_ID
```

is safe. Do not manually release another owner's reservation. If a physical action is still at an
unsafe cancellation boundary, allow it to complete before retrying finalization.

## Backend unavailable (19)

```console
labctl health --output json
labctl bench show BENCH_ID --output json
```

For a distributed bench, use its record to run `labctl agent show AGENT_ID --output json` and
`labctl agent timeline AGENT_ID`; control-plane health describes the central service, not a local
USB device. For physical ESP32 benches, check USB presence/serial identity, permissions, cable,
port ambiguity, esptool, and boot-marker configuration on the owning Agent. For SimLab, check the
configured backend is enabled and auto-started. Provider-specific CI settings do not fix a backend
health problem.

## Client/protocol error (20)

Confirm `LAB_PLATFORM_SERVER`, `/api/v1/version`, network reachability, and compatible
`0.6.0-alpha` client/server versions. Record the `X-Request-ID` and stable error code from the
response. Polling remains the supported fallback if optional event streaming is interrupted.

## Missing results or artifacts

Results require `operations:read`; downloads require `artifacts:read`. A session owner can list its
own artifacts after workflow failure and cancellation. Provider jobs must publish output paths in
an `always`/post section. A passing JUnit suite does not override a cleanup failure.

## Security reminder

Do not post token values, full environment dumps, or unredacted device logs in tickets. Standalone
Agent authentication is limited; keep it private and terminate TLS at a trusted proxy. For the
control plane, use direct configured TLS or the explicit loopback-only TLS-termination proxy mode
described in [Phase 5](PHASE_5.md#security-posture-and-limits). See
[API tokens](API_TOKENS.md).
