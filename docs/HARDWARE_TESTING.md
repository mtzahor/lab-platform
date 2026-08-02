# ESP32 hardware testing

Hardware tests are excluded from the default suite and require explicit opt-in plus an exact bench
identifier. This is a safety boundary against flashing an unintended connected device.

## Normal suite

```console
uv run ruff format --check .
uv run ruff check .
uv run mypy .
uv run pytest -m 'not hardware'
```

The normal suite uses SimLab, fake physical targets, fake serial handles, and fake esptool process
runners. It does not require an ESP32.

## Physical probe

Review `examples/esp32-local.yaml` and set a USB serial number or explicit port whenever possible.
Then run:

```console
export LAB_PLATFORM_ENABLE_HARDWARE_TESTS=1
export LAB_PLATFORM_HARDWARE_BENCH=esp32-devkit-01
export LAB_PLATFORM_HARDWARE_CONFIG=examples/esp32-local.yaml
uv run pytest -m hardware -k discovery_and_probe -v
```

The configured bench list must exactly equal `LAB_PLATFORM_HARDWARE_BENCH`.
This environment gate is read by pytest only. Manual `labctl` physical operations are authorized by
an explicit bench selection, a matching reservation where required, and the command's physical
selection flags; the CLI does not read `LAB_PLATFORM_ENABLE_HARDWARE_TESTS`.

## Destructive flash workflow

Build and inspect a known-good image before setting the firmware variable:

```console
export LAB_PLATFORM_HARDWARE_FIRMWARE=examples/esp32-firmware/.pio/build/esp32dev/firmware.bin
export LAB_PLATFORM_HARDWARE_FIRMWARE_VERSION=0.1.0
uv run pytest -m hardware -k flash_boot_reset_and_serial -v
```

This test flashes the configured address, verifies `READY`, extracts serial output, resets, reads a
second boot, and confirms invalid firmware metadata is rejected.

## Phase 5 distributed physical smoke test

The Phase 5 test is separate from the local-backend checks above. It starts a temporary loopback
control-plane process, enrolls a real-hardware Agent, and sends the firmware workflow through the
authenticated Agent gateway. The test then downloads the synchronized flash/serial artifacts,
restarts the Agent around its durable SQLite state, and verifies that the control-plane operation,
reservation, artifact, and connection history remains consistent without flashing a second time.

Review the one-bench configuration and firmware, then opt in with the distributed-specific gate:

```console
export LAB_PLATFORM_ENABLE_DISTRIBUTED_HARDWARE_TESTS=1
export LAB_PLATFORM_DISTRIBUTED_HARDWARE_BENCH=esp32-devkit-01
export LAB_PLATFORM_DISTRIBUTED_HARDWARE_CONFIG=examples/esp32-local.yaml
export LAB_PLATFORM_DISTRIBUTED_HARDWARE_FIRMWARE=examples/esp32-firmware/.pio/build/esp32dev/firmware.bin
export LAB_PLATFORM_DISTRIBUTED_HARDWARE_FIRMWARE_VERSION=0.1.0
uv run pytest tests/hardware/test_phase5_distributed_esp32_hardware.py -m hardware -v
```

`LAB_PLATFORM_DISTRIBUTED_HARDWARE_BENCH` must exactly match the sole bench in the sole real
backend. Set `LAB_PLATFORM_DISTRIBUTED_HARDWARE_TIMEOUT_SECONDS` only when the default 300-second
bound is too short. This test binds only to loopback and stores all temporary control-plane and
Agent state under pytest's temporary directory.

The distributed gate is intentionally distinct from `LAB_PLATFORM_ENABLE_HARDWARE_TESTS`; setting
the local-test gate alone cannot start the control-plane smoke test. The test creates its own
short-lived API/enrollment/Agent credentials and does not require an operator-supplied credential.

## Unplugged-device check

Unplug the configured board, ensure no replacement device matches its identifiers, and run:

```console
export LAB_PLATFORM_EXPECT_UNPLUGGED=1
uv run pytest -m hardware -k unplugged -v
```

Unset all hardware-test variables after the session. Never enable these tests in ordinary CI until
a dedicated, isolated hardware runner is available.
