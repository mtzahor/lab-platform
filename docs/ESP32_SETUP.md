# ESP32 DevKit V1 setup

Phase 2 supports one ESP32 DevKit V1 connected directly to the Agent host over a USB data cable.
The USB connection supplies power, carries the ROM-bootloader flashing protocol, and exposes the
115200-baud serial console. A relay is not required and power operations are intentionally not
advertised.

## Install and connect

1. Install Python 3.11 or newer and `uv`.
2. Run `uv sync --all-extras` from the repository root. This installs the pinned esptool 4.x CLI
   and pySerial.
3. Connect exactly one ESP32 DevKit V1 with a known-good USB data cable.
4. List the ports:

   ```console
   .venv/bin/python -m serial.tools.list_ports -v
   ```

   Typical names are `/dev/cu.SLAB_USBtoUART`, `/dev/cu.wchusbserial*`, or
   `/dev/cu.usbserial*` on macOS and `/dev/ttyUSB*` or `/dev/ttyACM*` on Linux. These names are
   examples only; the platform discovers ports from pySerial metadata.

5. Copy [examples/esp32-local.yaml](../examples/esp32-local.yaml). If the board reports a USB
   serial number, set it under `connection.usb.serial_number`. Otherwise set VID and PID. An
   explicit `connection.serial_port` is also supported.

Do not leave every USB matcher empty when several serial devices are attached. The backend will
return `SERIAL_PORT_AMBIGUOUS` rather than guess.

## Build the reference firmware

The reference PlatformIO project is in [examples/esp32-firmware](../examples/esp32-firmware).
With PlatformIO installed separately:

```console
cd examples/esp32-firmware
pio run
```

The raw application image is `.pio/build/esp32dev/firmware.bin` and is configured for address
`0x10000`. It prints:

```text
BOOTING
FIRMWARE_VERSION=0.1.0
SELF_TEST=PASS
READY
```

The platform does not invoke PlatformIO and remains independent of the firmware build system.

## Run the physical workflow

```console
lab-agent --config examples/esp32-local.yaml
```

In another terminal:

```console
labctl bench list
labctl bench probe esp32-devkit-01
labctl bench reserve esp32-devkit-01 --owner michael
labctl bench flash esp32-devkit-01 \
  examples/esp32-firmware/.pio/build/esp32dev/firmware.bin \
  --owner michael --version 0.1.0
labctl operation watch <operation-id>
labctl bench serial read esp32-devkit-01 --owner michael --until '^READY$' --timeout 20
labctl bench reset esp32-devkit-01 --owner michael
labctl bench release esp32-devkit-01 --owner michael
```

Serial output captured by operations is stored in
`.lab-platform/artifacts/operations/<operation-id>/serial.log` and indexed in SQLite.

## Linux permissions

The Agent user must already have read/write access to the device. Depending on the distribution,
that commonly means membership in `dialout` or `uucp`, followed by a new login session. Lab
Platform never changes groups, udev rules, ownership, or permissions. See
[TROUBLESHOOTING_SERIAL.md](TROUBLESHOOTING_SERIAL.md).

## Reconnect or return to SimLab

If the board is unplugged, connect it again and run `labctl bench probe esp32-devkit-01`. A
successful probe restores online health. To return to simulation, start with the directory-based
configuration: `lab-agent --config-dir config`.
