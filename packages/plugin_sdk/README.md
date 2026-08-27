# Lab Platform Plugin SDK

`lab-platform-plugin-sdk` is the standalone, Agent-independent Plugin API 1.x package for Lab
Platform hardware integrations. It provides stable metadata and lifecycle models, capability
protocols, compatibility checks, contract-test helpers, fakes, and the `lab-plugin init` scaffold.

Install it in a plugin project without importing Lab Platform Agent internals:

```console
python -m pip install lab-platform-plugin-sdk
lab-plugin init example-relay
```

Plugin API 1.x compatibility and authoring guidance are maintained in the main repository's
`PLUGIN_API.md` and `docs/PLUGIN_DEVELOPMENT.md`. The SDK version is independent of the Lab
Platform application version.
