# Phase 9: hardware ecosystem and production maturity

Phase 9 is the final roadmap phase. It converts the distributed deployment foundation into a
stable hardware-automation platform through Plugin API 1.0, vendor-neutral capabilities,
composable/locked resources, a broader reference ecosystem, operational analytics, production
observability, reliability evidence, and explicit support/upgrade contracts.

```text
CLI / Web / CI
      ↓
Control Plane
      ↓
Agent Protocol
      ↓
Agent
      ↓
Backend / resource registry
      ↓
stable capabilities
      ↓
hardware plugins
```

Hardware differences remain below the capability boundary. Core workflow, reservation, API, and
UI code does not branch on MCU/vendor names.

## Delivered engineering surfaces

- standalone `lab_platform.plugin_sdk` with immutable metadata, compatibility rules, lifecycle,
  drivers, diagnostics, stable capability models, fakes, contract helper, and `lab-plugin init`
- Plugin API adapters for ESP32, OpenOCD/STM32, RP2040 (picotool/UF2), and nRF52
  (Nordic/J-Link), plus independent USB/network relay power, SocketCAN, and generic instrument
  drivers; physical verification status remains governed by the compatibility matrix
- composed bench resource models/backend and atomic channel-aware locking contracts with
  leases/fencing; production reservation-flow integration remains an open readiness item
- stable workflow envelope (`lab.platform/v1`) and configuration `config_version: 1`
- operational utilisation, wait/reliability/failure/flaky/maintenance/alert models, dashboard,
  and aggregate metrics; lifecycle histogram instrumentation remains an open readiness item
- compatibility, stability/deprecation, support/LTS, hardware, plugin-author, reliability, and
  release-readiness records

The exact code/evidence state is tracked in
[`release/phase9-readiness.yaml`](../release/phase9-readiness.yaml), not inferred from this summary.

## Audited integration boundary

The reusable composition backend and resource coordinator pass deterministic contract tests, but
the existing durable control-plane reservation path does not yet acquire those resource locks.
Likewise, plugin startup failures are isolated and diagnosed, but controlled runtime
reinitialisation is not implemented, and queue/reservation/workflow histogram methods are not yet
fed by their lifecycle events. These are intentionally recorded as `partial`; simulator/unit
evidence must not be presented as end-to-end production evidence.

## Release boundary

The code line remains `0.9.0-beta`. Phase 9 implementation does not automatically authorize
`1.0.0`; release requires reviewed reference-scale load, 24-hour soak, interruption/recovery,
backup/restore, security, clean-install/upgrade/RC, final demo, and physical ESP32 + second MCU +
external accessory evidence. Those external gates are currently open.

Use:

```console
python scripts/release/check_phase9_readiness.py --allow-incomplete
```

to validate records. The same command without `--allow-incomplete` is the fail-closed `1.0` gate.
See [the readiness review](PHASE_9_READINESS.md) for the evidence process.

## Product documentation

- [Getting started](../README.md)
- [Architecture](../ARCHITECTURE.md) and [core concepts](MULTI_BACKEND.md)
- [Self-hosting](SELF_HOSTING.md) and [production deployment](PRODUCTION_DEPLOYMENT.md)
- [Agent and physical hardware setup](HARDWARE_SETUP.md)
- [Workflows](WORKFLOWS.md), [CI](CI_SESSIONS.md), and [dashboard](WEB_DASHBOARD.md)
- [Plugin API 1.0](../PLUGIN_API.md) and [plugin development](PLUGIN_DEVELOPMENT.md)
- [Hardware compatibility](HARDWARE_COMPATIBILITY.md)
- [Operations/reliability validation](RELIABILITY_VALIDATION.md)
- [Backup/recovery](BACKUP_RESTORE.md) and [upgrading](UPGRADING.md)
- [Stability/deprecation](STABILITY_POLICY.md) and [release support](SUPPORT_POLICY.md)

Earlier phase documents remain development history rather than the primary navigation.

## Final cut line

There is no Phase 10. Work after this cut line belongs to the user-driven backlog, experiments,
community proposals, commercial extensions, or post-1.0 maintenance. `1.0.0` is tagged only when
every release criterion has accepted evidence.
