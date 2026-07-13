# SimLab integration

`SimLabBackend` is the sole adapter between platform services and the simulator. It implements the
same `LabBackend` protocol intended for future physical implementations:

- lifecycle and immutable bench snapshots
- power on/off/cycle
- asynchronous firmware progress
- stable translation of not-found, offline, capability, timeout, and injected failures

The simulator never returns mutable benches. Its frozen snapshots are mapped into shared
`BenchSnapshot` models with normalized capability names. Reservation state is deliberately absent
from SimLab and overlaid by `BenchService` from SQLite.

## Timing

Accelerated mode divides simulated delays by `speed_multiplier`; this is the default for demos.
Manual mode advances only when a test calls `backend.simulator.tick(seconds)`, allowing power-cycle
and flash behavior to be tested without nondeterministic wall-clock delays.

## Failure injection

Tests can inject `flash_failure`, `checksum_failure`, `usb_disconnect`, `kernel_panic`,
`boot_failure`, `overheat`, or `backend_timeout` through the adapter's development/testing simulator
control surface. Failures are translated into `SIMULATION_FAILURE` or `BACKEND_TIMEOUT` operation
errors. No production-facing failure-injection CLI is exposed.
