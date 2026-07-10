# Architecture

Lab Platform uses Clean Architecture with a small composition root in the Agent
application. Dependencies point inward: applications and adapters depend on the core,
while the core knows only immutable domain models and injected interfaces.

## Package boundaries

| Area | Responsibility | May perform I/O |
| --- | --- | --- |
| `packages/models` | Pydantic data contracts | No |
| `packages/core` | Agent rules, events, health, capabilities, scheduling, state | No |
| `packages/config` | YAML loading, merging, validation, defaults | File reads only |
| `packages/logging` | Structured standard-library logging | Console logging |
| `packages/plugins` | Plugin contracts and dynamic discovery | Import discovery only |
| `packages/simlab` | Deterministic local bench simulation | No external I/O |
| `apps/agent` | Composition, lifecycle, read-only HTTP API | Yes |
| `apps/cli` | Human-facing local API client | Yes |

`packages/core` does not import web, CLI, presentation, ORM, or UI frameworks. Objects
such as `AgentCore`, `EventBus`, `HealthMonitor`, and `PluginManager` are constructed in
`create_agent()` and passed through constructors; there are no service globals.

## Startup and shutdown

Startup is ordered and asynchronous:

1. Load and validate YAML configuration.
2. Configure structured logging.
3. start the event-driven core.
4. Discover and initialize plugins.
5. Start SimLab and register its benches.
6. Mark the Agent ready and serve HTTP.

Shutdown reverses resource ownership: plugins and SimLab stop, core bench-offline
events are published, in-memory registries are cleared, and health becomes `warning`.
Plugin startup is transactional; already initialized plugins are stopped if a later
plugin fails.

## Events and health

`EventBus` supports exact event subscriptions and `*` subscriptions. Handlers may be
synchronous or asynchronous, and publication preserves subscription order. Phase 0
publishes `AgentStarted`, `AgentStopped`, `PluginLoaded`, `BenchRegistered`,
`BenchOffline`, and `HealthChanged`.

Each subsystem reports `healthy`, `warning`, or `unhealthy`. Agent health is the worst
current subsystem status, making degradation deterministic without subsystem coupling.

## Local data flow

```text
labctl -> HTTP GET -> Agent application -> AgentCore / PluginManager / SimLab
                                           |
                                           +-> EventBus -> subscribers
```

No state survives process shutdown in Phase 0.
