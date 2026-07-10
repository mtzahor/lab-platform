# Plugin API

Plugins are local capability providers. A plugin exposes immutable metadata and
implements three lifecycle methods:

```python
from collections.abc import Sequence

from lab_platform.models import Capability, PluginMetadata


class ExamplePlugin:
    @property
    def metadata(self) -> PluginMetadata: ...

    async def initialize(self) -> None: ...

    async def shutdown(self) -> None: ...

    def capabilities(self) -> Sequence[Capability]: ...
```

The easiest implementation subclasses `BasePlugin`; see
[`examples/temperature_plugin.py`](examples/temperature_plugin.py).

## Discovery

Installed distributions register factories through the `lab_platform.plugins` entry
point group:

```toml
[project.entry-points."lab_platform.plugins"]
temperature = "my_package.plugins:TemperaturePlugin"
```

For local development, a configuration item may also be an import path:

```yaml
plugins:
  - power
  - my_package.plugins:TemperaturePlugin
```

Built-in names are `power`, `serial`, and `firmware`. A factory must return an object
matching the `Plugin` protocol. Plugin metadata contains `name`, `version`, `author`,
`description`, and `capabilities`.

Initialization follows configuration order. Shutdown runs in reverse order. Names must
be unique, and any initialization failure rolls back plugins already started during the
same load attempt.
