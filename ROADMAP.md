# Roadmap

## Phase 0 — local foundation (complete)

- Typed core, plugins, lifecycle, health, structured logging, and deterministic SimLab benches
- Read-only local API and CLI

## Phase 1 — remote control and core bench workflow (complete)

- Versioned FastAPI REST boundary and HTTP-only CLI
- Persistent reservation ownership/history and asynchronous operations
- SHA-256-addressed firmware, SQLite repositories, locks, cancellation, and restart recovery
- Deterministic SimLab failure injection and complete CLI E2E coverage

## Phase 2 — first physical target (complete)

- Configuration-selected `RealLabBackend` and physical-target abstraction
- ESP32 DevKit V1 discovery, probe, raw-binary flash, reset, and serial capture
- Boot verification, operation artifacts, stable hardware errors, and gated physical tests

## Phase 3 — shared labs and scheduling (complete)

- Globally unique mixed SimLab/physical inventory across multiple backend instances
- Timed reservations, automatic expiry, persistent FIFO queues, and safe extension
- Atomic promotion/operation locks, restart reconciliation, and unified bench timelines
- Capability-filtered sequential workflows with stored runs and step results

## Phase 4 — hardware CI and developer workflow integration (complete)

- Hashed scoped API tokens with one-time plaintext, expiry, revocation, owner checks, and audit
  events
- Persistent CI sessions coordinating deterministic atomic bench selection, reservation, heartbeat,
  workflow, outcome, cleanup, and recovery
- Typed workflow inputs (`string`, `integer`, `boolean`, `artifact`) with a deliberately restricted
  interpolation language
- Safe generic artifact upload/download, JSON test results, and JUnit XML export
- Stable `labctl token`, `labctl ci`, and `labctl workflow results` interfaces and CI exit codes
- GitHub composite action plus generic GitLab CI and Jenkins templates
- SimLab-only local E2E demonstration and an explicit gated ESP32 path using the same workflow
- Guaranteed cleanup after success, failure, cancellation, timeout, and abandoned clients

Phase 4's machine authentication is intentionally limited. It does not make the Agent safe for
direct exposure to the untrusted public internet.

## Phase 5 — distributed control plane and multi-Agent labs (reference implementation complete)

- Independent control-plane service with authenticated protocol `1.0` WebSockets, one-time Agent
  enrollment, unique rotatable/revocable credentials, heartbeat presence, drain mode, and unified
  global inventory
- Control-plane-owned reservations with Agent-confirmed versioned leases, durable remote commands,
  pre-execution acknowledgment, Agent-side safety validation, idempotent execution, and persisted
  user-facing operations
- Durable Agent command journal, bounded event buffer with explicit acknowledgments, boot-aware
  reconnect reconciliation, unknown-state/grace handling, and safe offline/Agent-restart behavior
- Complete remote sequential workflows, checksummed scoped artifact transfers, bounded Agent cache,
  and provider-neutral distributed CI selection across Agent labels and locations
- Protocol/recovery/fault/migration coverage and a SimLab correctness test for 10 Agents, 1,000
  benches, 100 simultaneous routes, 500 queued CI sessions, and mass reconnect
- Existing ESP32 backend available through the same remote workflow path with explicit manual
  hardware gating; normal CI remains deterministic and hardware-free

The central production persistence adapter uses PostgreSQL, with explicit schema migrations and
restart coverage. SQLite remains supported for the loopback demo and Agent-local durable state.

See [the Phase 5 architecture, demo, and limitations](docs/PHASE_5.md).

## Later phases

Deferred work includes full organizational identity, SSO, advanced RBAC, secret-vault integration,
relays, hosted control plane, high availability, dashboards, arbitrary pipeline graphs, advanced
analytics, billing, and production deployment hardening.

The `0.6.0-alpha` reference control plane supports PostgreSQL and development-mode per-Agent bearer
credentials. mTLS, active-active/HA coordination, and broader production hardening remain later
deployment work rather than hidden claims of Phase 5.
