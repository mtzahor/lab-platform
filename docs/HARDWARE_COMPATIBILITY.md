# Hardware compatibility

The source of truth is [`compatibility/hardware.yaml`](../compatibility/hardware.yaml). It records
the plugin/API version, exact board or accessory, host operating system, debugger/tooling, known
limitations, automated contract, and latest physical evidence independently. Documentation and
the dashboard may render that file but must not promote an integration without changing its
evidence record.

## Status definitions

| Status | Meaning |
| --- | --- |
| `VERIFIED` | Repeated automated or physical hardware evidence is attached for the exact listed setup and is within its review interval. |
| `SUPPORTED` | Maintained and expected to work through a reproducible contract, but not continuously exercised in the reference lab. |
| `EXPERIMENTAL` | Integration or its public behavior is incomplete, newly introduced, or awaiting qualifying evidence. |
| `COMMUNITY` | Maintained outside the official reference set; evidence and support are provided by the named maintainer. |
| `DEPRECATED` | Available only during a documented removal window; the replacement and last-supported release are recorded. |

Status applies to a matrix row, not a whole silicon family. Evidence for an STM32 Nucleo-F446RE
with ST-Link and OpenOCD on Linux does not verify every STM32, debugger, OpenOCD release, or host
OS. Unknown combinations remain unlisted and therefore unsupported.

## Current Phase 9 matrix

The checked-in adapters and contracts cover the following engineering paths. All physical rows
remain `EXPERIMENTAL` until dated device evidence is attached; automated contract success alone is
not represented as a physical pass.

| Integration | Automated path | Physical gate |
| --- | --- | --- |
| ESP32 DevKit V1 | final Plugin API adapter plus real-backend contract | Re-run the reference flash/serial/reset workflow. |
| STM32 Nucleo-F446RE | target registry and OpenOCD argument/error contract | Record ST-Link/OpenOCD probe, flash verification, reset, and serial evidence. |
| Raspberry Pi Pico / RP2040 | Plugin API/config contract where present | Record picotool or UF2 discovery/flash/re-enumeration evidence. |
| Nordic nRF52 | Plugin API/config contract where present | Record nrfjprog or J-Link probe/flash/reset evidence. |
| USB relay | independent power-resource contract | Record channel isolation and safe power-cycle evidence. |
| J-Link | debug/tool contract where available | Record installed tool/device/version and one real target. |
| SocketCAN | CAN send/receive contract | Record interface setup, standard/extended frames, filters, timeout, and PCAP artifact. |

Read the exact status and evidence path from the YAML rather than inferring it from this overview.

## Evidence requirements

A qualifying physical record includes:

- UTC start/end time and immutable result location
- commit and release-candidate version
- Agent, Plugin API, and plugin versions
- host OS/architecture and kernel where relevant
- board/accessory model, hardware revision, and non-secret serial identity
- external tool, driver, and firmware versions
- configuration with credentials and host-specific paths redacted
- repeated probe/reset/flash/stream or accessory operations
- negative-path result such as disconnect, timeout, or conflict handling
- reviewer and expiry/review date

The evidence path may point to a signed CI artifact, durable test report, or a repository record
containing hashes and links. A local terminal statement without preserved output is not sufficient
for `VERIFIED`.

Suggested review interval is 90 days for reference hardware and after every material tool,
firmware, plugin, Agent, or OS upgrade. Expired evidence does not prove regression; it moves the
row back to `SUPPORTED` or `EXPERIMENTAL` until refreshed.

## Reference physical validation

The minimum Phase 9 release gate is one composed-lab run with:

```text
ESP32 DevKit V1
+ one second MCU family (reference: STM32 Nucleo-F446RE)
+ one external accessory (reference: USB relay)
```

The run must demonstrate target discovery, an independent resource reservation, resource-level
conflict prevention, power cycle, flash, serial verification, artifact capture, cleanup, unplug or
plugin-failure recovery, and successful reuse. J-Link and SocketCAN can be additional contract or
physical evidence depending on host tooling.

## Adding or changing a row

1. Add the narrowest accurate device/tool/OS entry to `compatibility/hardware.yaml`.
2. Link automated and physical evidence separately.
3. List external dependencies, known limitations, and failure modes.
4. Start at `EXPERIMENTAL` unless evidence already satisfies a stronger definition.
5. Obtain maintainer review; never self-promote a community path to official `VERIFIED`.
6. Add deprecation fields before using `DEPRECATED`.

For setup and diagnostic commands, see [hardware setup](HARDWARE_SETUP.md). For authoring rules,
see [Plugin API 1.0](../PLUGIN_API.md) and [plugin development](PLUGIN_DEVELOPMENT.md).
