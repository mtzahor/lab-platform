# Real backend

`RealLabBackend` implements the same `LabBackend` protocol as `SimLabBackend`. Backend selection
occurs only in `create_lab_backend()` at the Agent composition root:

```yaml
backend:
  type: real
```

The real backend owns configured `PhysicalTarget` implementations selected by the target-driver
registry. The ESP32 and OpenOCD/STM32 paths use that same factory boundary; the API, CLI,
reservation service, operation runner, event history, and persistence layers contain no
vendor-family branch.

```text
CLI -> REST API -> application services -> LabBackend
                                          |
                                          +-- SimLabBackend
                                          +-- RealLabBackend -> target registry -> PhysicalTarget
                                                                    +-- Esp32Target
                                                                    +-- OpenOcdTarget
```

## Capabilities

The ESP32 target advertises the stable `flash`, `serial`, `probe`, and `reset` capabilities and
retains `firmware` as a pre-1.0 input alias. It does not advertise
`power`, `power_on`, `power_off`, or `power_cycle`, because toggling DTR/RTS or resetting through
the ROM bootloader is not physical power control. Unsupported power requests return
`CAPABILITY_NOT_SUPPORTED`.

The OpenOCD/STM32 path advertises `probe`, `reset`, `flash`, `debug`, and optional serial through
the same backend contract. Independent power, debugger, CAN, or instrument devices are composed as
resources; they are not hidden as target-specific branches.

## Discovery and health

For `serial_port: auto`, resolution uses USB serial number first, then VID/PID, and finally accepts
one unique serial device. Multiple candidates fail with `SERIAL_PORT_AMBIGUOUS`; zero candidates
fail with `DEVICE_NOT_FOUND`. An explicit port must either appear in pySerial enumeration or exist
as a device path.

Startup probes each configured target but does not crash when the board is absent. The Agent also
re-probes idle, unreserved targets on a bounded cadence under a persistent maintenance lock; it
never runs that health check through an active reservation or operation. Target health is one of
`online`, `offline`, `degraded`, or `unknown`:

- `online`: the port resolved and esptool successfully read chip information.
- `offline`: no matching serial device exists.
- `degraded`: the port exists but esptool, permissions, communication, or target validation failed.
- `unknown`: no successful probe has completed yet, or recovery is required.

## Flash and boot verification

Phase 2 accepts one raw binary at the configured address. Before invoking esptool, the adapter
re-reads the artifact and verifies its SHA-256 and size. It invokes esptool without a shell, streams
output through the asynchronous process runner, maps progress monotonically, terminates the child
on timeout or cancellation, and translates low-level errors into stable platform codes.

After esptool hard-resets the target, the adapter captures serial output until the configured ready
regular expression matches. Failure patterns abort verification, and the named `version` group is
stored as the current firmware version. The boot capture is attached to the generic operation
artifact mechanism.

The supported esptool range is pinned by the package metadata and adapter tests. Treat any major
tool upgrade as a compatibility change: review command/reset vocabulary, rerun the fake process
contract, then attach new physical evidence before changing the compatibility status.

## Concurrency and recovery

The existing SQLite partial unique index admits one pending/running/cancel-requested operation per
bench. Flash, reset, and serial read all enter that same operation path. Cancelling a flash cancels
the runner task; the process runner terminates esptool, waits briefly, and kills it if necessary.
The target must be probed again after an uncertain hardware state.
