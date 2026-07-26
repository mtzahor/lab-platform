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

## Later phases

Deferred work includes full organizational identity, SSO, advanced RBAC, secret-vault integration,
distributed/multi-Agent coordination, relays, hosted control plane, high availability, dashboards,
arbitrary pipeline graphs, advanced analytics, billing, and production deployment hardening.

The immediate post-Phase-4 priority is to harden identity and multi-Agent coordination without
moving CI-provider-specific behavior into the domain layer.
