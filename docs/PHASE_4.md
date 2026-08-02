# Phase 4: Hardware CI and developer workflow integration

Phase 4 connects Lab Platform to ordinary build pipelines. A CI job can authenticate, select and
reserve a compatible simulated or physical bench, upload firmware, execute a registered workflow,
collect results, and release the bench without manual intervention. The release is
`0.5.0-alpha`.

The validation question is:

> Can a build pipeline safely execute a repeatable test against simulated or physical hardware
> without manual intervention?

## Supported flow

```text
CI runner
  -> labctl ci run
  -> Agent API and CI session
  -> atomic bench selection and reservation
  -> artifact upload
  -> sequential workflow runner
  -> SimLabBackend or RealLabBackend
  -> JSON/JUnit results and downloaded artifacts
  -> cleanup, reservation release, and finalization
```

CI providers never call SimLab, serial tools, or hardware drivers directly. GitHub Actions is the
first polished integration. GitLab CI and Jenkins use the same provider-neutral CLI and API.

## Quick local demonstration

Install the workspace and run the self-contained SimLab demonstration:

```console
uv sync --all-extras
./scripts/local-ci-demo.sh
```

The script starts a temporary Agent, creates a scoped token, copies the deterministic demo
firmware, runs `esp32-ci-test` on a SimLab bench, writes `hardware-results.xml`, downloads output
artifacts, finalizes the CI session, and verifies that the bench is released. It does not require
internet access or physical hardware. See [Local CI demo](#local-ci-demo-details) for environment
overrides.

For an already-running Agent, create a token on its trusted management interface:

```console
labctl token create \
  --name local-ci \
  --owner local-ci \
  --scope ci:sessions \
  --scope benches:read \
  --scope reservations:write \
  --scope workflows:run \
  --scope operations:read \
  --scope artifacts:read \
  --scope artifacts:write
```

Copy the one-time plaintext token into an environment variable, then run:

```console
export LAB_PLATFORM_SERVER=http://127.0.0.1:8080
export LAB_PLATFORM_TOKEN='the-one-time-token'

labctl ci run \
  --workflow esp32-ci-test \
  --artifact firmware=build/firmware.bin \
  --input expected_version=0.5.0 \
  --require capability=firmware \
  --require capability=serial \
  --require capability=reset \
  --require capability=probe \
  --label board=esp32 \
  --allow-simulated \
  --no-allow-physical \
  --wait-timeout 10m \
  --reservation-duration 30m \
  --junit-output hardware-results.xml \
  --artifacts-directory hardware-artifacts
```

The command detects provider metadata, creates a session, uploads artifacts, maintains a
heartbeat, displays progress, exports results, downloads artifacts, finalizes cleanup, and exits
with a stable CI result code.

## One workflow, two backends

[`examples/workflows/esp32-ci-test.yaml`](../examples/workflows/esp32-ci-test.yaml) declares typed
inputs, required capabilities, and `board: esp32`. Its expression language is deliberately limited
to `${{ inputs.<name> }}`; unknown inputs and other expressions are rejected.

SimLab-only selection:

```console
labctl ci run \
  --workflow esp32-ci-test \
  --artifact firmware=build/firmware.bin \
  --input expected_version=0.5.0 \
  --label board=esp32 \
  --allow-simulated \
  --no-allow-physical \
  --wait-timeout 2m
```

Explicit physical ESP32 selection:

```console
labctl ci run \
  --bench esp32-devkit-01 \
  --workflow esp32-ci-test \
  --artifact firmware=build/firmware.bin \
  --input expected_version=0.5.0 \
  --no-allow-simulated \
  --allow-physical \
  --junit-output hardware-results.xml \
  --artifacts-directory hardware-artifacts
```

The explicit bench still has to be online and satisfy the workflow requirements. Physical tests
are opt-in; normal development and CI tests use SimLab. The CLI safety boundary is the explicit
bench plus `--no-allow-simulated --allow-physical`. `LAB_PLATFORM_ENABLE_HARDWARE_TESTS=1` gates
only the local physical pytest suite and is not consulted by `labctl`. Configure the physical
bench with `labels: {board: esp32}` to match the shared workflow.

## Lifecycle guarantees

A CI session coordinates its selected bench, reservation, input artifacts, workflow run, output
artifacts, heartbeat, and cleanup record. Cleanup runs after success, test failure, cancellation,
timeout, runner disappearance, or restart recovery. It cancels active work when necessary,
releases the reservation and operation lock, closes serial resources, and finalizes artifacts.

The terminal session status is `completed`; the durable `outcome` preserves whether the run
succeeded, failed, was cancelled, timed out, or ended with an infrastructure error. A passed
workflow with failed cleanup is an infrastructure error, never a successful CI run.

See [CI sessions](CI_SESSIONS.md) and [cleanup guarantees](CI_CLEANUP.md).

## Results and artifacts

Workflow assertions and test-producing steps are available as JSON and JUnit XML:

```console
labctl workflow results WORKFLOW_RUN_ID --format json
labctl workflow results WORKFLOW_RUN_ID --format junit --output hardware-results.xml
labctl ci artifacts SESSION_ID
labctl ci download ARTIFACT_ID --output hardware-artifacts/serial.log
```

See [artifacts](ARTIFACTS.md) and [JUnit results](JUNIT_RESULTS.md).

## Configuration

The Phase 4 defaults separate each failure boundary:

```yaml
agent:
  max_request_body_size_mb: 1

artifacts:
  max_upload_size_mb: 100

ci:
  default_reservation_minutes: 30
  maximum_reservation_minutes: 120
  heartbeat_interval_seconds: 30
  heartbeat_timeout_seconds: 120
  bench_wait_timeout_seconds: 600
  session_timeout_seconds: 3600
  workflow_timeout_seconds: 1800
  step_timeout_seconds: 600
  cleanup_timeout_seconds: 60

serial:
  stream_buffer_lines: 500
  artifact_max_size_mb: 50
  decode_errors: replace
  redact_patterns: []
```

Do not collapse these into one global timeout. A bench wait, workflow execution, individual step,
heartbeat, and cleanup fail for different reasons and map to different diagnostics.

## Security boundary

Phase 4 adds hashed bearer tokens, scopes, expiry, revocation, a global request-body limit,
upload-specific limits, generated artifact paths, request validation, and structured audit events.
Workflows are declarative and cannot run arbitrary shell code. Terminate TLS at a trusted reverse
proxy and restrict the Agent and token management endpoints to a private network.

This is intentionally limited machine authentication. It does not provide SSO, organization
identity, advanced RBAC, a secrets vault, tenant isolation, or a hardened public control plane.
**Do not expose Phase 4 directly to the untrusted public internet or describe it as
production-secure.** Read [API tokens](API_TOKENS.md) before connecting a CI runner.

## Provider integrations

- [GitHub Actions](GITHUB_ACTIONS.md)
- [GitLab CI](GITLAB_CI.md)
- [Jenkins](JENKINS.md)

Provider detection and job-summary rendering live in integration adapters and the CLI, not in the
domain or scheduler. Common API and application services remain vendor-neutral.

## Local CI demo details

`scripts/local-ci-demo.sh` accepts environment overrides without putting secrets on the command
line:

| Variable | Default | Purpose |
| --- | --- | --- |
| `LAB_PLATFORM_TOKEN` | token created by the script | Token for `LAB_DEMO_USE_EXISTING_AGENT=1` |
| `LABCTL_BIN` | workspace `labctl` | Alternate CLI executable |
| `LAB_AGENT_BIN` | workspace `lab-agent` | Alternate Agent executable |
| `LAB_DEMO_OUTPUT_DIR` | `build/phase4-ci-demo` | JUnit and artifact output |
| `LAB_DEMO_PORT` | `18080` | Temporary Agent port |
| `LAB_DEMO_USE_EXISTING_AGENT` | `0` | Set to `1` to use `LAB_PLATFORM_SERVER` instead |

The script traps `INT`, `TERM`, and normal exit. The CLI performs session cancellation and
finalization; the script then stops its temporary Agent and removes only its temporary directory.

## Phase 4 cut line

Phase 4 stops at a dependable authenticate → select → reserve → upload → run → collect → release →
finalize loop. Distributed Agents, a hosted cloud control plane, SSO, billing, high availability,
arbitrary user code, pipeline graphs, and a full dashboard remain later work.
