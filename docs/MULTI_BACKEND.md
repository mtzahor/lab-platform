# Multiple Backends

One Agent may expose benches from multiple SimLab and real backend instances. Each configured
backend has a stable unique ID, and every discovered bench ID must be unique across the registry.

```yaml
agent:
  name: local-lab-agent
  host: 127.0.0.1
  port: 8080

backends:
  - id: virtual-lab
    type: simlab
    config:
      config_path: ./examples/simlab-team.yaml
      benches: 4
      auto_start: true
      clock_mode: accelerated
      speed_multiplier: 5

  - id: local-hardware
    type: real
    config:
      benches:
        - id: esp32-devkit-01
          name: ESP32 DevKit V1
          target_type: esp32
          connection:
            serial_port: auto
            baud_rate: 115200
```

`config_path` may point to a legacy `simlab:` file, another full platform configuration, or a
direct SimLab settings mapping. Relative paths are resolved from the file that declares the
backend. Inline fields take precedence over values loaded from `config_path`. With
`auto_start: false`, the SimLab instance remains out of the Agent inventory.

## Registry behavior

Startup asks each backend for its inventory and builds a bench-to-backend routing table. Duplicate
bench IDs fail startup with `BENCH_ID_CONFLICT`; requests for unknown IDs return
`BACKEND_NOT_FOUND` or `BENCH_NOT_FOUND` as appropriate. All operations, probes, and serial streams
delegate through the same registry contract.

## Bench catalog

The catalog stores backend ownership, online/health state, target type, capabilities, labels, and
last-seen timestamps. Backend refresh updates observed state while retaining platform-owned labels
and metadata.

```bash
labctl bench list --online
labctl bench list --available
labctl bench list --capability flash
labctl bench list --label board=esp32 --label location=home-lab
```

## Compatibility

Existing `backend`, `simlab`, and `hardware` sections remain valid and are normalized into one
registered backend. This permits an incremental move from Phase 2 configuration.
