# Lab Platform GitHub Action

This composite action runs the provider-neutral `labctl ci run` flow and publishes the identifiers
and result emitted by `labctl`. The API token is exposed to the process only through
`LAB_PLATFORM_TOKEN`; it is never added to the command line.

```yaml
- name: Run ESP32 hardware test
  id: hardware
  uses: your-org/lab-platform/integrations/github-action@v1
  with:
    server: ${{ secrets.LAB_PLATFORM_SERVER }}
    token: ${{ secrets.LAB_PLATFORM_TOKEN }}
    workflow: esp32-ci-test
    firmware: build/firmware.bin
    expected-version: ${{ github.sha }}
    required-labels: board=esp32,location=simulation
    allow-simulated: "true"
    allow-physical: "false"
    wait-timeout: 10m

- name: Publish JUnit
  uses: actions/upload-artifact@v4
  if: always()
  with:
    name: hardware-test-results
    path: |
      hardware-results.xml
      hardware-artifacts/
```

The runner must be able to reach the configured Agent. The action uses `uv` to run the bundled CLI
from the checked-out action repository. Set `LABCTL_BIN` only when intentionally testing another
CLI executable.
SimLab and physical benches use the same action inputs. Physical selection is disabled by default;
enable it explicitly in a gated job. Selection is controlled by capabilities, labels, and the
`allow-simulated` / `allow-physical` inputs.

Outputs are `session-id`, `bench-id`, `workflow-run-id`, `result`, and `artifact-directory`.
Cancellation signals are forwarded to `labctl`, which performs server-side cancellation and
finalization. Artifact publication should use `if: always()` so diagnostics survive test failures.
