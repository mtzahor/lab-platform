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

## Phase 6 — identity, teams, and access control (functional foundation implemented)

Implemented in the 0.7.0-alpha foundation:

- Organisation-scoped user, service-account, principal, team, membership, session, credential,
  fixed-role, policy, snapshot, audit, and login-attempt models.
- Strict configuration for local auth, OIDC, sessions, authorisation defaults, audit, login
  throttling, and loopback-only development auto-login; safe runtime auto-login resolves an
  existing user and applies normal RBAC.
- Salted scrypt local passwords, opaque rotating sessions, revocable/IP-restrictable identity-bound
  service credentials, audit sanitization, and login-attempt domain services.
- Additive organisation/direct/team/resource RBAC with trusted Agent-to-bench inheritance,
  assignment expiry, cross-organisation rejection, credential narrowing, and persisted
  bench/workflow policy enforcement.
- Schema version 10 identity and tenant persistence: transitional default-organisation backfill,
  composite tenant workflow keys, organisation-scoped CI/artifact/reservation/queue retry keys, and
  scoped adapters for the major distributed resources.
- Supported `lab-control-plane bootstrap-admin` first-owner/recovery flow without plaintext
  password arguments.
- `labctl auth login/logout/status/whoami`, native macOS/Linux versioned session-bundle storage,
  and transparent single-retry token rotation for stored interactive logins.
- Control-plane local and OIDC login, refresh/logout/me/session routes and dual Phase 6/legacy
  bearer acceptance on existing protected routes.
- OIDC authorization-code login with discovery, S256 PKCE, strict RS256 ID-token validation, and
  configurable username-claim mapping to pre-provisioned users without JIT provisioning.
- Identity-only organisation/user/team/service-account/credential/role and bench/workflow
  access-policy administration REST APIs, audit-read REST APIs, and matching `labctl` families.
- Principal-bound identity reservation ownership, authenticated actor context on
  manual/workflow/CI-launched remote commands, and service-account identity propagation into
  distributed CI sessions.
- Trusted exact-resource checks on named Agent, bench, reservation, workflow, and operation routes,
  configurable resource-specific `404` hiding, durable authorisation snapshots for identity-backed
  remote commands, and matching actor/snapshot attribution on principal-facing cancellation,
  reconciliation, inventory-refresh, and drain/undrain control payloads.
- Snapshot-safe idempotent command/workflow replay that binds the stable principal, tenant, request
  content, and required permissions under credential restrictions while retaining the snapshot
  accepted with the original work; workflow replay also fingerprints the effective restrictions.
- Per-item RBAC filtering on Agent, bench, workflow, operation, and reservation collections;
  application-service enforcement for commands, reservations, workflows, CI, drain, enrollment,
  runtime lifecycle, and identity-facing artifact operations; and explicit legacy/internal escapes.
- Parent-inherited artifact access across trusted operation, workflow-run, CI-session, and remote
  command/bench relationships, including platform artifact deletion and fail-closed handling for
  unresolved workflow-step parents.
- Attributable success/denial events across protected identity administration, Agent/enrollment,
  bench, reservation, workflow, operation, CI, access-policy, and artifact actions; bounded runtime
  audit/login-attempt retention; and HTTP security headers on successful and error responses.
- A copy-ready human/team/service-account access demonstration with an authorised SimLab ESP32
  workflow, Viewer denial, least-privilege CI credential, and organisation-scoped audit review.

Remaining before the Phase 6 cut line can be called complete:

- Internal/background organisation propagation, lower-priority legacy storage conversion, and
  removal or documented retention of remaining deployment-global Agent/bench identifiers.
- A transactional replay outbox/state machine if principal control intents must be redelivered
  automatically after restart. Current durable intents distinguish authorisation from a wire send,
  while automatic/background work with no originating Phase 6 decision and legacy work
  deliberately has no initiating actor/snapshot. CI cancellation maintenance can reuse evidence
  persisted by the principal request.
- Tenant-safe resolution for `WORKFLOW_STEP` artifact parents, and remote-artifact deletion if it
  becomes a supported product operation.
- Legacy-token conversion tooling and removal of its deployment-global compatibility trust
  boundary.
- Broader public-API/OIDC rate limiting, trusted-proxy/client-address policy, and broader
  background/distributed/browser hardening.

See [Phase 6 status and limitations](docs/PHASE_6.md).

## Phase 7 — web dashboard and operations UX (alpha cut line implemented)

Implemented in the 0.8.0-alpha release:

- React/TypeScript operations shell with responsive, accessible, permission-aware navigation and a
  generated OpenAPI client.
- Local and OIDC browser login using rotating `HttpOnly` session cookies, double-submit CSRF,
  browser-safe auth discovery, safe return paths, and explicit bearer precedence for CLI/CI.
- Aggregated overview, searchable/filterable benches, bench/Agent details and timelines,
  immediate and scheduled reservations with owner-safe queues, workflow launch/history,
  operation/CI inspection, bounded serial text, artifacts, audit, and identity/access
  administration.
- Generic presentation APIs for overview counts, action permissions, current reservations and
  operations, workflow-run summaries, queue position, timelines, and cursor-based serial windows.
- SSE snapshot/invalidation updates with reconnect behavior and bounded query polling fallback;
  unknown and reconciling distributed states remain distinct and truthful.
- Existing-reservation workflow launch with an explicit retain/release lifecycle choice.
- Integrated static serving with fail-fast bundle validation, immutable fingerprinted assets,
  restrictive security/cache headers, SPA exclusions, package data, and wheel inspection.
- A same-origin separate-static layout through a reverse proxy; permissive cross-origin cookie
  authentication is intentionally excluded.
- Frontend format/lint/type/test/build/client-freshness/dependency checks alongside Python,
  OpenAPI, real two-Agent SimLab browser integration, package, and fixture browser validation.
- Operational, deployment, authentication, permission, live-update, troubleshooting,
  accessibility, and complete SimLab browser-demo documentation.

See [Phase 7 status](docs/PHASE_7.md) and the
[browser demonstration](docs/PHASE_7_BROWSER_DEMO.md).

## Phase 8 — open-core product and deployment (beta implementation in progress)

The `0.9.0-beta` preview line productizes the existing lab capabilities rather than adding hardware
families:

- Apache-2.0 community licensing, NOTICE/trademark/security policy, an explicit open-core
  feature-provider seam, and a release gate that keeps commercial modules out of the community
  wheel.
- Inspectable production and disposable demo Compose templates plus packaged `lab-platform init`
  and `lab-platform dev` commands.
- Non-root control-plane and Agent container definitions, amd64/arm64 release builds, immutable
  version metadata, checksums, SPDX SBOMs, provenance/signatures, security scanning, and dependency
  update automation.
- Development/test/production profiles, mounted secret files, strict precedence/validation,
  reverse-proxy trust, HTTPS/PostgreSQL production requirements, resource limits, API category rate
  limits, structured/redacted logs, metrics, graceful shutdown, and live/ready endpoints.
- Read-only schema status/check plus explicit forward migration; the current schema-12 target,
  minimum schema-11 source policy declares restore-backup-required rollback.
- Versioned backup create/verify/restore, local/S3 artifact storage interfaces, class-based artifact
  retention, bounded audit retention, and auditable deletion.
- Control-plane/Agent/API/protocol/plugin version reporting, Agent upgrade statuses/enforcement,
  `labctl version --all`, `upgrade check`, deployment diagnostics, and production preflight.
- Self-hosting, Docker, upgrading, storage, retention, backup/restore, release-channel,
  compatibility, licensing/open-core, managed-boundary, disaster-recovery, and security runbooks.

Phase 8 is complete only after the hardware-free published-artifact deployment gate proves a fresh
production deployment, owner bootstrap, Agent enrollment, SimLab workflow/artifacts, complete
backup and isolated restore, previous-minor migration, post-upgrade readiness/history, and Agent
reconnect. Documentation and unit components are not substitutes for that acceptance run. See
[the Phase 8 contract](docs/PHASE_8.md).

## Later phases

Deferred work includes SAML/SCIM/LDAP, external group-role mapping, custom policy languages,
secret-vault integration, relays, active-active/HA coordination, a Kubernetes operator, arbitrary
pipeline graphs, advanced analytics, mature public multi-tenant managed-service hardening, billing,
payment processing, and a customer portal.

The Phase 8 self-hosted target remains one control plane. A hosted design-partner deployment is
technically possible with the same Agent, but it does not imply an SLA, completed public
multi-tenant certification, automatic remote Agent installation, or `1.0` stability.
