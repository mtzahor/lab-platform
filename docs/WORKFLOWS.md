# Sequential Workflows

Workflows describe repeatable bench procedures in YAML. Phase 3 deliberately supports a small,
portable action set: `flash`, `reset`, `read_serial`, `assert_serial`, `wait`, and `probe`.

```yaml
name: esp32-smoke-test
version: 1
description: Flash and verify an ESP32 image

requirements:
  capabilities: [firmware, serial, reset]

steps:
  - action: flash
    firmware: "${firmware}"
    version: 0.4.0
  - action: read_serial
    until_pattern: READY
    timeout_seconds: 20
  - action: assert_serial
    pattern: SELF_TEST=PASS
```

Workflows contain no shell commands, arbitrary Python, network calls, loops, conditions, parallel
steps, or secrets.

## Running a workflow

Reuse an active reservation:

```bash
labctl workflow run esp32-smoke-test \
  --bench esp32-devkit-01 \
  --owner michael \
  --input firmware=./firmware.bin
```

Or request and optionally release an immediate reservation around the run:

```bash
labctl workflow run esp32-smoke-test \
  --bench esp32-devkit-01 \
  --owner michael \
  --reserve 30m \
  --release-after \
  --input firmware=./firmware.bin
```

```bash
labctl workflow list
labctl workflow show esp32-smoke-test
labctl workflow watch <workflow-run-id>
labctl workflow cancel <workflow-run-id> --owner michael
```

## Execution rules

The runner validates all required capabilities before step one. Steps run sequentially and persist
their result. A failed operation, serial assertion, expired reservation, unavailable target,
cancellation, or Agent restart fails the run. Completed steps remain in history; Phase 3 performs no
rollback.

Workflow actions route through backend-neutral services. A compatible definition can run on SimLab
and physical hardware without backend-specific workflow steps.
