# Roadmap

## Phase 0 — local foundation (complete)

- Typed core, plugins, lifecycle, health, structured logging, and deterministic SimLab benches
- Read-only local API and CLI

## Phase 1 — remote control and core bench workflow (complete)

- Versioned FastAPI REST boundary and HTTP-only CLI
- Persistent reservation ownership and history
- Asynchronous power and firmware operations with polling and cancellation
- SHA-256-addressed firmware artifacts
- SQLite repositories, atomic per-bench operation locking, and restart recovery
- SimLab backend contract, deterministic failure injection, and complete CLI E2E coverage

## Phase 2 — future specification required

Physical hardware drivers, authentication, RBAC, multi-Agent coordination, live serial streaming,
CI-provider integrations, dashboards, and production deployment remain out of scope until a Phase 2
specification defines those boundaries.
