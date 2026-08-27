# Plugin development

This guide takes a fake hardware plugin from an empty directory to Agent discovery. The generated
project uses only the standalone Plugin SDK and is designed to complete in under 30 minutes.

## Prerequisites

- Python 3.11 or newer
- Lab Platform Plugin SDK `1.x`
- pytest for contract tests
- a disposable virtual environment

Do not develop a plugin while its process owns production hardware. Start with a fake driver, then
move to a dedicated development device.

## Generate and test a plugin

```console
lab-plugin init example-relay
cd example-relay
python -m venv .venv
. .venv/bin/activate
python -m pip install -e ".[dev]"
pytest
```

The generator refuses to overwrite an existing directory. It creates:

```text
example-relay/
├── pyproject.toml
├── README.md
├── src/example_relay/
│   ├── __init__.py
│   ├── config.py
│   ├── driver.py
│   ├── errors.py
│   └── plugin.py
└── tests/
    ├── test_contract.py
    └── test_driver.py
```

The entry point exports a `PluginRegistration`, so importing the distribution does not open a
device. The Agent first validates registration metadata, version compatibility, platform, and
plugin configuration; only then does it call the factory and lifecycle.

## Add one power device

Give the plugin configuration a stable device selector. Strict fields catch typos before the
relay is touched:

```python
from pydantic import BaseModel, ConfigDict, Field


class PluginConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    serial_number: str = Field(min_length=1)
    channel: int = Field(default=1, ge=1, le=16)
    cycle_delay_seconds: float = Field(default=1.0, ge=0.1, le=60)
```

Implement `PowerCapability` with idempotent `on()` and `off()` operations. `cycle()` must turn the
configured channel off, wait the bounded delay, and turn it on. Never expose a raw shell command
or allow arbitrary channel selection through workflow input.

The driver descriptor and reported capability set must agree:

```python
DeviceDescriptor(
    id=f"example-relay:{serial_number}:channel-{channel}",
    name=f"Example relay channel {channel}",
    type="usb-relay-channel",
    serial_number=serial_number,
    capabilities={"power"},
    metadata={"channel": channel},
)
```

Return the same driver for that stable ID until it disappears. A device that is unplugged becomes
`offline`; do not silently bind a different relay to the ID.

## Translate errors and preserve cancellation

Call vendor APIs through a narrow transport. Convert expected failures at that boundary:

```python
try:
    await transport.set_channel(channel, enabled)
except asyncio.TimeoutError as exc:
    raise PluginTimeoutError(
        "Relay did not respond before the configured timeout",
        device_id=device_id,
    ) from exc
except VendorDeviceMissing as exc:
    raise DeviceUnavailableError("Relay was disconnected", device_id=device_id) from exc
```

Do not catch `BaseException`. In particular, `asyncio.CancelledError` must propagate so workflow
cancellation and Agent shutdown remain timely. Error details are machine-facing and must not
contain secrets or an unredacted environment.

External commands use a fixed executable and a list of validated arguments with `shell=False`.
Apply a timeout, cap captured output, and redact it before diagnostics or logs. Keep firmware paths
and device selectors separate from vendor script languages.

## Extend the contract tests

`assert_plugin_contract()` covers lifecycle, health, unique discovery IDs, descriptor/capability
agreement, and unsupported-capability behavior. Add plugin-specific cases for:

- initialize then repeated discovery
- shutdown before and after partial initialization
- timeout and cancellation
- missing executable or shared library
- permission denied
- unplug and replug without identity reassignment
- invalid configuration aggregation
- concurrent calls to an exclusive channel
- redaction of diagnostic output

Use fakes for ordinary CI. Mark tests that touch real devices with `@pytest.mark.hardware`, keep
them disabled by default, and require explicit device IDs/environment gates.

## Diagnostics

Implement `diagnostics()` when the plugin depends on host state. Return one named check per useful
remediation unit, for example:

```text
executable       PASS      OpenOCD 0.12.0
usb-device       PASS      ST-Link V3 detected
permissions      FAIL      /dev/bus/usb/... is not accessible
target-config    PASS      target/stm32f4x.cfg
```

Messages explain the observation; `remediation` explains a safe next step. Plugins never change
groups, udev rules, kernel interfaces, ownership, permissions, or firmware automatically.

After editable installation:

```console
labctl plugin list
labctl plugin show example-relay
labctl plugin doctor example-relay
lab-agent doctor --config-dir config
```

An absent plugin usually means the editable install used a different Python environment, the
entry-point group/name is wrong, or registration import failed. An incompatible plugin remains
listed with a stable diagnostic instead of crashing the Agent.

## Version and compatibility choices

- Increment the plugin patch version for compatible fixes.
- Increment the plugin minor version for backward-compatible device/capability additions.
- Increment the plugin major version for plugin-specific configuration or behavior breaks.
- Keep `plugin_api_version="1.0"` while using Plugin API 1.x.
- Raise `minimum_agent_version` only when a required runtime feature is unavailable on older
  Agents.
- Set `maximum_agent_version` only for a demonstrated incompatibility; prefer testing newer Agents
  and releasing a compatible fix.

The Plugin API major and plugin package major are independent. A plugin `3.2.0` can correctly use
Plugin API `1.0`.

## Requesting official status

A contribution includes a maintainer, supported OS/tool/device versions, known limitations,
failure modes, troubleshooting, contract evidence, and security review of every external command.
Compatibility status is evidence-based:

- `SUPPORTED` requires an actively maintained contract and reproducible integration test.
- `VERIFIED` additionally requires repeated physical evidence for the exact listed setup.
- Third-party ownership is recorded as `COMMUNITY`, even when its test quality is high.

See [hardware compatibility](HARDWARE_COMPATIBILITY.md), [contributing](../CONTRIBUTING.md), and
[security reporting](../SECURITY.md) before submitting a plugin.
