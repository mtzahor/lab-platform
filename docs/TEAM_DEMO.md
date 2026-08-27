# Phase 3 Team Demo

This demonstration exercises the Phase 3 cut line with three users and several benches.

## Start the Agent

Use the team example configuration and start one Agent:

```bash
lab-agent --config examples/phase3-team-agent.yaml
```

The supplied inventory contains ten `virtual-*` SimLab benches with mixed capabilities plus an
optional physical `esp32-devkit-01`.

## 1. Inspect unified inventory

```bash
labctl bench list
labctl bench list --capability flash
labctl bench list --label location=simulation
```

## 2. Reserve and queue

```bash
labctl reservation create esp32-devkit-01 --owner michael --duration 30m
labctl reservation queue esp32-devkit-01 --owner alice --duration 20m
labctl reservation queue-list esp32-devkit-01
```

Use a simulated firmware-capable bench when physical hardware is unavailable:

```bash
labctl reservation create virtual-01 --owner michael --duration 30m
```

## 3. Schedule Bob's future slot

```bash
labctl reservation create virtual-02 \
  --owner bob \
  --start 2026-07-21T10:00:00+03:00 \
  --duration 1h
```

## 4. Run the smoke test

For the physical ESP32, first build the reference firmware:

```bash
cd examples/esp32-firmware
pio run
cd ../..
```

Then run the workflow with the resulting raw application image:

```bash
labctl workflow run esp32-smoke-test \
  --bench esp32-devkit-01 \
  --owner michael \
  --input firmware=../esp32-firmware/.pio/build/esp32dev/firmware.bin

labctl workflow watch <workflow-run-id>
```

Without physical hardware, reserve `virtual-01` and use the deterministic SimLab fixture instead:

```bash
labctl workflow run esp32-smoke-test \
  --bench virtual-01 \
  --owner michael \
  --input firmware=../firmware/demo.bin
```

## 5. Release and observe promotion

```bash
labctl reservation release <michael-reservation-id> --owner michael
labctl reservation list --bench-id esp32-devkit-01
labctl reservation queue-list esp32-devkit-01
```

Alice is promoted when her complete duration fits before any protected scheduled slot.

## 6. Inspect history

```bash
labctl bench timeline esp32-devkit-01
labctl event list --bench-id esp32-devkit-01
```

The same workflow definition now runs on physical and simulated backends without route-specific
application logic.
