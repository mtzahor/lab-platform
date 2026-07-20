# Phase 2: first physical target

Phase 2 adds one ESP32 DevKit V1 through a configuration-selected `RealLabBackend`. The validation
question is answered by keeping every ESP32 concern below `LabBackend`: application services use
the same generic methods and operation model for SimLab and physical hardware.

## Delivered scope

- configuration-selected SimLab or real backend at the composition root;
- `PhysicalTarget` protocol and `Esp32Target` implementation;
- deterministic serial discovery with explicit ambiguity failures;
- esptool probe, raw-binary flash, streamed progress, timeout, and cancellation cleanup;
- generic probe, reset, and serial-read backend operations;
- ready/failure pattern matching and firmware-version extraction;
- operation-scoped serial log files plus SQLite metadata;
- stable hardware error translation;
- unit, backend-contract, API/CLI, and opt-in hardware tests;
- PlatformIO reference firmware and a complete local configuration.

## Cut line

The supported workflow is:

```text
Reserve -> Probe -> Flash -> Reset -> Capture serial -> Detect READY -> Record version -> Release
```

Phase 2 does not add relay power control, multiple physical benches, remote Agents, JTAG, OTA,
Wi-Fi provisioning, dashboards, CI hardware runners, or generic support for every MCU.

## Validation

Run the normal suite with `uv run pytest -m 'not hardware'`. Follow
[HARDWARE_TESTING.md](HARDWARE_TESTING.md) for the explicitly enabled physical workflow and
[ESP32_SETUP.md](ESP32_SETUP.md) for wiring, firmware, and commands.
