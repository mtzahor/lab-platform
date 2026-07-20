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

## Destructive flash workflow

Build and inspect a known-good image before setting the firmware variable:

```console
export LAB_PLATFORM_HARDWARE_FIRMWARE=examples/esp32-firmware/.pio/build/esp32dev/firmware.bin
export LAB_PLATFORM_HARDWARE_FIRMWARE_VERSION=0.1.0
uv run pytest -m hardware -k flash_boot_reset_and_serial -v
```

This test flashes the configured address, verifies `READY`, extracts serial output, resets, reads a
second boot, and confirms invalid firmware metadata is rejected.

## Unplugged-device check

Unplug the configured board, ensure no replacement device matches its identifiers, and run:

```console
export LAB_PLATFORM_EXPECT_UNPLUGGED=1
uv run pytest -m hardware -k unplugged -v
```

Unset all hardware-test variables after the session. Never enable these tests in ordinary CI until
a dedicated, isolated hardware runner is available.
