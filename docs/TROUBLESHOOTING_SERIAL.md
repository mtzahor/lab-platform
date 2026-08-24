# Serial troubleshooting

Start with `labctl bench probe BENCH_ID --owner <owner> --output json`. Against a standalone Agent,
an online probe requires that owner's active reservation. The Phase 5 control-plane probe is a
read-only distributed command and does not require a central reservation. Both paths hold an Agent
operation lock and apply local capability/health/safety checks; an offline recovery probe uses an
exclusive maintenance lock. The stable error code identifies the layer that failed without
exposing raw pySerial or subprocess exceptions.

| Error | Meaning | Checks |
| --- | --- | --- |
| `DEVICE_NOT_FOUND` | No auto-discovery candidate matched | Cable carries data; board is connected; serial/VID/PID is correct |
| `SERIAL_PORT_NOT_FOUND` | Explicit path is absent | Re-list ports; update the path after reconnect |
| `SERIAL_PORT_AMBIGUOUS` | Several candidates remain | Configure USB serial number or VID/PID; disconnect unrelated adapters |
| `SERIAL_PERMISSION_DENIED` | Agent cannot open the device | Inspect device ownership; use the distribution's serial group; log in again |
| `SERIAL_PORT_BUSY` | Another process owns the port | Close PlatformIO monitor, Arduino Serial Monitor, screen, minicom, or another Agent |
| `SERIAL_DISCONNECTED` | Device disappeared during I/O | Replace cable; avoid hubs; probe after reconnect |
| `ESPTOOL_NOT_AVAILABLE` | Agent Python lacks esptool | Run `uv sync --all-extras`; verify `.venv/bin/python -m esptool version` |
| `ESPTOOL_CONNECTION_FAILED` | ROM bootloader did not answer | Hold BOOT while tapping EN; lower flash baud; verify target type and cable |
| `ESPTOOL_FLASH_FAILED` | esptool returned a write/verify failure | Read operation error, retry at a lower baud, validate image/address |
| `ESPTOOL_TIMEOUT` | Flash/probe exceeded configured timeout | Increase timeout only after checking cable and boot mode |
| `WRONG_TARGET_TYPE` | Detected chip does not match `flash.chip` | Select the intended board or correct configuration |
| `BOOT_TIMEOUT` | Ready marker was not observed | Confirm firmware baud and ready regex; inspect stored `serial.log` |
| `BOOT_VERIFICATION_FAILED` | Failure marker appeared or ready marker was absent | Inspect boot log for panic, abort, or brownout |
| `FIRMWARE_VERIFICATION_FAILED` | Uploaded file changed or disappeared | Upload again; do not edit content-addressed artifacts |

## Useful local checks

```console
.venv/bin/python -m serial.tools.list_ports -v
.venv/bin/python -m esptool --chip esp32 --port /dev/ttyUSB0 read-mac
```

On macOS, prefer `/dev/cu.*` for initiating outbound serial sessions. On Linux, confirm the Agent
process sees the same device path and permissions as the interactive shell. Lab Platform does not
install drivers, edit udev rules, change groups, or force-close other processes.

After any interrupted flash, run a new probe before trusting reported health. A failed flash does
not imply that the previous firmware remains intact.
