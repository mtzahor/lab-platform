# GitHub Actions

The composite action in [`integrations/github-action`](../integrations/github-action) wraps the
provider-neutral `labctl ci run` command. It detects GitHub run metadata, maintains the session
heartbeat, forwards cancellation signals, writes a GitHub job summary, exports JUnit, and downloads
artifacts. Scheduling and workflow behavior remain in common services.

## Prerequisites

1. Run a Lab Platform Agent on a network reachable from the GitHub runner.
2. Register [`esp32-ci-test.yaml`](../examples/workflows/esp32-ci-test.yaml) with that Agent.
3. Create a scoped, expiring API token as described in [API tokens](API_TOKENS.md).
4. Add `LAB_PLATFORM_SERVER` and `LAB_PLATFORM_TOKEN` as GitHub Actions secrets.

For a private lab, a self-hosted runner on the same private network is normally safer than exposing
the Agent. If traffic crosses a network boundary, terminate TLS at an authenticated reverse proxy.
Phase 4 is not a hardened public-internet service.

## Complete workflow

```yaml
name: ESP32 Hardware Test

on:
  pull_request:
  workflow_dispatch:

permissions:
  contents: read

jobs:
  build-and-test:
    runs-on: self-hosted

    steps:
      - uses: actions/checkout@v4

      - name: Build firmware
        run: ./scripts/build-firmware.sh

      - name: Run ESP32 hardware test
        id: hardware
        uses: your-org/lab-platform/integrations/github-action@v1
        with:
          server: ${{ secrets.LAB_PLATFORM_SERVER }}
          token: ${{ secrets.LAB_PLATFORM_TOKEN }}
          workflow: esp32-ci-test
          firmware: build/firmware.bin
          expected-version: ${{ github.sha }}
          required-capabilities: firmware,serial,reset,probe
          required-labels: board=esp32,location=simulation
          allow-simulated: "true"
          allow-physical: "false"
          wait-timeout: 10m
          reservation-duration: 30m
          junit-output: hardware-results.xml
          artifacts-directory: hardware-artifacts

      - name: Publish hardware-test diagnostics
        uses: actions/upload-artifact@v4
        if: always()
        with:
          name: hardware-test-results
          path: |
            hardware-results.xml
            hardware-artifacts/
          if-no-files-found: warn
```

The repository includes a dogfooding example at
[`.github/workflows/hardware-test.yml`](../.github/workflows/hardware-test.yml).

## Action inputs

| Input | Required | Default | Meaning |
| --- | --- | --- | --- |
| `server` | yes | — | Agent base URL |
| `token` | yes | — | Scoped API token; passed only through `LAB_PLATFORM_TOKEN` |
| `workflow` | yes | — | Registered workflow name |
| `firmware` | yes | — | Firmware file uploaded as the `firmware` artifact input |
| `expected-version` | yes | — | Expected target version string |
| `required-capabilities` | no | `firmware,serial,reset,probe` | Comma-separated capability filter |
| `required-labels` | no | `board=esp32` | Comma-separated exact label filters |
| `allow-simulated` | no | `true` | Permit SimLab candidates |
| `allow-physical` | no | `false` | Permit real-backend candidates only when explicitly enabled |
| `wait-timeout` | no | `10m` | Maximum bench wait |
| `reservation-duration` | no | `30m` | Requested reservation duration |
| `junit-output` | no | `hardware-results.xml` | JUnit destination |
| `artifacts-directory` | no | `hardware-artifacts` | Download directory |

## Outputs

With `id: hardware`, later steps can read:

```yaml
- name: Record selected bench
  run: |
    echo "Session: ${{ steps.hardware.outputs.session-id }}"
    echo "Bench: ${{ steps.hardware.outputs.bench-id }}"
    echo "Workflow: ${{ steps.hardware.outputs.workflow-run-id }}"
    echo "Result: ${{ steps.hardware.outputs.result }}"
```

Outputs are `session-id`, `bench-id`, `workflow-run-id`, `result`, and
`artifact-directory`. Do not echo the token or enable `set -x` in a step that handles secrets.

## SimLab-only pull requests

For required PR validation, avoid consuming or depending on physical hardware:

```yaml
- name: Run deterministic SimLab test
  uses: your-org/lab-platform/integrations/github-action@v1
  with:
    server: ${{ secrets.LAB_PLATFORM_SERVER }}
    token: ${{ secrets.LAB_PLATFORM_TOKEN }}
    workflow: esp32-ci-test
    firmware: build/firmware.bin
    expected-version: ${{ github.sha }}
    required-capabilities: firmware,serial,reset,probe
    required-labels: board=esp32,location=simulation
    allow-simulated: "true"
    allow-physical: "false"
```

## Gated physical ESP32 job

Use an environment or manual dispatch gate and a self-hosted runner:

```yaml
physical-esp32:
  if: github.event_name == 'workflow_dispatch'
  runs-on: self-hosted
  environment: physical-hardware
  steps:
    - uses: actions/checkout@v4
    - run: ./scripts/build-firmware.sh
    - uses: your-org/lab-platform/integrations/github-action@v1
      with:
        server: ${{ secrets.LAB_PLATFORM_SERVER }}
        token: ${{ secrets.LAB_PLATFORM_TOKEN }}
        workflow: esp32-ci-test
        firmware: build/firmware.bin
        expected-version: ${{ github.sha }}
        required-capabilities: firmware,serial,reset,probe
        required-labels: board=esp32,location=local
        allow-simulated: "false"
        allow-physical: "true"
```

When an exact device is required rather than a label-selected pool, call the generic CLI with
`--bench esp32-devkit-01` in a shell step.

## Results and summaries

The action reports the selected bench, backend kind, duration, workflow steps, cleanup status, and
artifact names in `$GITHUB_STEP_SUMMARY`. GitHub formatting is generated by the adapter; the
domain stores provider-neutral records. Upload diagnostics with `if: always()` so failures and
cancellations do not discard JUnit and serial logs.

## Cancellation

GitHub sends a termination signal when a job is cancelled. The action forwards it to `labctl`,
which requests session/workflow cancellation and finalization. If the runner is killed before the
handler finishes, the Agent detects the missing heartbeat and performs server-side cleanup. See
[cleanup guarantees](CI_CLEANUP.md).

## Troubleshooting

- A GitHub-hosted runner generally cannot reach a private localhost/LAN Agent; use a self-hosted
  runner or an approved private-network connection.
- Exit 12 means immediate zero-wait selection failed; exit 13 means a positive bench-wait deadline
  elapsed. Neither result necessarily distinguishes incompatible filters from compatible benches
  that are currently busy.
- Publish `hardware-results.xml` and `hardware-artifacts/` even when the action fails.
- Verify the token has all scopes and has not expired or been revoked.

See [CI troubleshooting](CI_TROUBLESHOOTING.md) for the complete exit-code table.
