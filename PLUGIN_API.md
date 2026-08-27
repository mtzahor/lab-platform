# Plugin API 1.0

Plugin API `1.0` is the stable, Agent-independent contract for hardware integrations. Plugin
authors import `lab_platform.plugin_sdk`; they do not import Agent runtime internals. The public
capability names are:

```text
probe reset power serial flash debug gpio can capture measure command
```

A device advertises only the capabilities it actually implements. The pre-1.0 `firmware` name is
accepted as a migration alias and is normalized to `flash`; new plugins must use `flash`.

## Version contract

Plugin and platform versions are independent:

| Surface | Current contract | Compatibility rule |
| --- | --- | --- |
| Plugin API | `1.0` | All `1.x` API declarations share the same major-version contract. |
| Agent | product version | A plugin declares an inclusive minimum and optional maximum. |
| Entry points | `lab_platform.plugins` | Entry-point names are stable plugin identifiers. |

The Agent rejects a different Plugin API major, an out-of-range Agent version, or an unsupported
host platform before initialization. Rejection is reported as plugin-local diagnostics and must
not stop compatible plugins. See [the stability policy](docs/STABILITY_POLICY.md) for the complete
deprecation rules.

## Metadata and registration

Installed distributions expose a lazy `PluginRegistration` from the
`lab_platform.plugins` entry-point group:

```toml
[project.entry-points."lab_platform.plugins"]
example-relay = "example_relay.plugin:registration"
```

```python
from pydantic import BaseModel, ConfigDict

from lab_platform.plugin_sdk import (
    PLUGIN_API_VERSION,
    HardwarePlugin,
    PluginContext,
    PluginMetadata,
    PluginRegistration,
)


class PluginConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


METADATA = PluginMetadata(
    name="example-relay",
    version="1.0.0",
    plugin_api_version=PLUGIN_API_VERSION,
    vendor="Example Devices",
    description="Controls one USB relay.",
    supported_platforms=["linux", "macos"],
    supported_devices=["example-relay-v1"],
    capabilities=["power"],
    minimum_agent_version="1.0.0",
    maximum_agent_version=None,
)


def create(config: PluginConfig, context: PluginContext) -> HardwarePlugin:
    ...


registration = PluginRegistration(
    metadata=METADATA,
    config_model=PluginConfig,
    factory=create,
)
```

Metadata is immutable and strictly validated. Names, semantic versions, API versions, capability
names, supported platforms/devices, and Agent-version ranges are part of the compatibility
decision. Each plugin owns a strict Pydantic configuration model so all configured plugin errors
can be collected before hardware is touched.

## Lifecycle and device drivers

Every plugin implements:

```python
class HardwarePlugin(Protocol):
    @property
    def metadata(self) -> PluginMetadata: ...

    async def initialize(self) -> None: ...
    async def discover(self) -> list[DeviceDescriptor]: ...
    async def health(self) -> PluginHealth: ...
    async def get_driver(self, device_id: str) -> DeviceDriver: ...
    async def shutdown(self) -> None: ...
```

Every discovered device has a unique stable ID and a driver:

```python
class DeviceDriver(Protocol):
    @property
    def descriptor(self) -> DeviceDescriptor: ...

    @property
    def capabilities(self) -> set[str]: ...

    async def health(self) -> DeviceHealth: ...
    async def get_capability(self, name: str) -> Capability: ...
```

`initialize()` may allocate plugin-local resources but should not perform destructive target
operations. `discover()` must be repeatable. `shutdown()` must tolerate partial initialization and
is called in reverse startup order. Unknown device IDs raise `DeviceUnavailableError`; an
unadvertised capability raises `UnsupportedCapabilityError`.

The SDK exports typed interfaces and immutable models for every stable capability, including
streaming progress/serial/CAN operations and artifact-backed captures. Vendor exceptions must be
translated to the public `PluginSdkError` hierarchy. Preserve cancellation; do not convert
`asyncio.CancelledError` into an ordinary operation failure.

## Failure containment and diagnostics

Discovery, configuration, compatibility, initialization, health, and shutdown are separate
failure stages. The runtime records a stable error code and message per plugin. Plugin callbacks
are bounded by Agent timeouts; a failed J-Link or CAN plugin degrades only its own resources.

Plugins can implement `diagnostics()` and return `DiagnosticCheck` values for external tools,
device permissions, USB/serial access, configuration, and resource conflicts. Diagnostic messages
must redact tokens, passwords, private keys, device credentials, and command environment values.

Inspect a running Agent with:

```console
labctl plugin list
labctl plugin show openocd
labctl plugin doctor openocd
lab-agent doctor --config-dir config
```

## Discovery security

Production discovery uses built-ins and installed Python entry points. Development import paths
are permitted only when explicitly configured. The Agent does not recursively import Python from
writable directories. A plugin invoking an external tool must use a fixed executable plus an
argument vector, validate any tool script/config content, enforce a timeout, and avoid shell
interpolation.

## Contract tests

The SDK provides fake drivers/capabilities and `assert_plugin_contract`:

```python
import asyncio

import pytest
from lab_platform.plugin_sdk.testing import assert_plugin_contract

from example_relay.plugin import registration


@pytest.mark.plugin_contract
def test_plugin_contract() -> None:
    asyncio.run(assert_plugin_contract(registration))
```

Official and third-party plugins should additionally test error translation, cancellation,
timeout behavior, duplicate discovery IDs, dependency loss, unplug/replug, and safe shutdown after
partial initialization. Physical tests stay opt-in and must name the exact board, adapter,
firmware, host OS, and tool versions used.

## Create a plugin

```console
lab-plugin init example-relay
cd example-relay
python -m pip install -e ".[dev]"
pytest
```

The generated project contains strict configuration, registration metadata, a device driver,
translated errors, and contract/driver tests. Continue with the copy-ready
[plugin developer guide](docs/PLUGIN_DEVELOPMENT.md).

## Migrating a pre-1.0 plugin

1. Import public types from `lab_platform.plugin_sdk`, not `lab_platform.agent` or plugin-manager
   modules.
2. Replace the old `Capability` list with plugin metadata plus one or more `DeviceDriver` objects.
3. Rename the `firmware` capability to `flash`.
4. Add `plugin_api_version`, platforms/devices, and Agent version bounds to metadata.
5. Export a lazy `PluginRegistration` entry point rather than a constructed global plugin.
6. Translate vendor/tool exceptions to SDK errors and add diagnostics.
7. Run the reusable contract tests and attach hardware evidence before requesting `VERIFIED`.

The old in-tree lifecycle adapter remains a compatibility bridge during the documented 1.x
deprecation window; it is not the authoring API for new plugins.
