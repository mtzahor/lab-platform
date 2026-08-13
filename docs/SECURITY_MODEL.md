# Phase 6 security model

Phase 6 is designed around three separate authenticated identities:

```text
human user or CI service account --REST--> control plane
Agent infrastructure identity ------WS--> control plane
short-lived artifact capability ----HTTP-> scoped artifact transfer
```

The control plane is authoritative for user/service authorisation. An Agent remains authoritative
for local locks, hardware safety, and execution, but must not decide an organisation role from
caller-provided metadata.

## Implemented foundations

- Salted versioned scrypt local-password hashes.
- Opaque session and service credential formats with stored secret hashes.
- Session/credential expiry and revocation checks.
- Local-login rate-limit counters.
- Organisation-scoped identity repositories and cross-organisation evaluator rejection.
- Organisation-bearing persistence and scoped repository methods for Agents/enrollment, global
  benches, reservations, remote commands/operations, workflows, generic/remote artifacts, and CI
  sessions, including trusted parent-to-child organisation propagation.
- Schema v10 tenant-composite workflow definition/run/result relationships and organisation-scoped
  CI/artifact/reservation/queue/distributed-launch retry keys.
- Phase 6 route wiring that supplies the authenticated organisation to those collection/named
  lookups, validates artifact parents/transfer targets, and constrains distributed workflow/CI
  bench selection to the caller's organisation.
- Per-item RBAC/access-policy filtering on Agent, bench, workflow, operation, and reservation
  collections. CI lists use durable session ownership plus organisation-level grants for sessions
  owned by another principal.
- Additive fixed-role evaluation, trusted Agent-to-bench inheritance, assignment expiry, and
  credential narrowing.
- Runtime enforcement of persisted bench/workflow access policies, including restricted/private
  visibility, allowed read teams, required reservation/operation roles, admin-only workflows, and
  configured default bench visibility.
- Audit metadata redaction and an append-oriented repository.
- Strict configuration, TLS/WSS policy, and loopback-only development auto-login validation.
- `labctl` environment/native-store credential precedence without a plaintext file fallback.
- Offline first-owner bootstrap with hidden prompting, named environment input, owner-existence
  refusal, and explicit session-revoking recovery.
- Local login/refresh/logout/current-principal and session-management HTTP routes.
- OIDC authorization-code/PKCE login with discovery, strict RS256 ID-token validation, single-use
  state/nonce, and pre-provisioned username-claim mapping.
- Identity-only organisation, user, team, service-account, credential, role/effective-permission,
  bench/workflow access-policy, and audit-read administration routes plus matching CLI commands.
- Phase 6 session/service-credential authentication on existing protected routes, with exact
  resource resolution on named Agent, bench, reservation, workflow, and operation routes and
  configurable legacy-token fallback.
- Principal-bound ownership on Phase 6 reservations/workflows and authenticated `ActorContext` on
  identity-backed manual/workflow remote commands. Distributed CI sessions persist their
  initiating service-account or user identity instead of trusting `requested_by` text.
- Protected application-service boundaries for identity-facing Agent, bench, workflow, operation,
  and CI catalog reads; workflow registration; commands; reservations; workflow/CI execution;
  drain; enrollment administration; runtime Agent lifecycle; and artifact access. Operational
  collections enforce exact resource or CI ownership/organisation filtering inside the facade;
  nested Agent summaries use a non-admitting visible-bench query. Phase 6 calls require
  authenticated context; supported compatibility/infrastructure callers select an explicit legacy
  or internal escape, and conflicting modes are rejected. Trusted-resource checks precede protected
  detail reads, persistence, stream consumption, transfer issuance, or dispatch.
- Workflow-coordinator enforcement of exact `workflows:run` and selected-bench
  `benches:operate`, including actor/context integrity. Authenticated launches reload the exact
  tenant/name/version definition from the trusted catalog and reject caller-content mismatches
  before preflight or side effects. Coordinator replay state and derived workflow side-effect keys
  are tenant-qualified. The distributed CI service independently enforces creation, start,
  lifecycle, and ownership rules and passes the current authenticated context into workflow
  coordination.
- Parent-inherited artifact RBAC: operation and remote artifacts resolve their exact bench/Agent;
  workflow-run artifacts resolve the tenant run, definition, and actual bench; and linked CI
  artifacts require session access plus artifact permission on both workflow and selected bench.
  Lists filter inaccessible rows, upload authorises before consuming content, and transfers verify
  the target Agent tenant before issuing a capability.
- Authorisation snapshots on identity-backed manual/workflow/CI-launched commands and
  principal-initiated cancellation/reconciliation/inventory-refresh/drain controls, including
  permission, resource, granting assignment IDs, and evaluation time; durable commands and
  control-intent records reference that evidence.
- Principal- or system-attributed success/denial audit events across protected
  identity/access-policy administration, Agent enrollment/lifecycle, bench action, reservation,
  workflow/operation/CI lifecycle, and artifact upload/download/transfer/delete routes. Local and
  OIDC login failures are audited whenever the organisation can be resolved.
- Bounded runtime audit retention and stale login-attempt pruning.
- Loopback-only development auto-login that resolves an existing active user and passes through
  normal authorisation.
- Security headers on success and error responses: MIME sniffing/frame/referrer/permissions/CSP
  protections, HTTPS-only HSTS, and no-store/no-cache on auth and credential paths.

## Not yet an enforced platform boundary

The following gaps prevent a claim of complete multi-tenant security:

- Some lower-priority internal/background mutation, event/timeline,
  connection/protocol/reconciliation, artifact-transfer, and legacy catalog/backend/firmware/lock
  paths do not yet carry tenant scope or use protected facades uniformly.
- Schema v10 makes workflow names/versions and supported CI/artifact/reservation/queue retry keys
  tenant-safe. Some historical human-readable identifiers, notably Agent slugs/global bench IDs,
  remain deployment-global.
- `WORKFLOW_STEP` artifact ownership has no tenant-scoped trusted lookup, so Phase 6 named access
  and mutations fail closed and lists filter those records. Platform-managed artifact deletion is
  supported; remote-artifact deletion is not. Legacy scope compatibility retains its historical
  visibility and is outside the Phase 6 tenant boundary.
- Legacy-token requests still use caller-provided owner text during compatibility; Phase 6
  reservation mutations use principal ID/type.
- Legacy tokens have no organisation principal: their operational reads and token inventory remain
  deployment-global while compatibility is enabled, including administration by a Phase 6
  principal with the mapped credential permission.
- Principal-facing Agent drain/undrain, inventory-refresh, operation-reconciliation, and
  operation/CI-cancellation routes persist or retain exact decision evidence and put the same
  actor/snapshot identity on the resulting Agent control payload. Principal-requested CI cancel
  atomically persists a `CI_SESSION` decision (including granting assignments) and actor with
  `CANCEL_REQUESTED`, verifies the trusted session-to-command/operation/Agent/bench/reservation
  binding, and reuses that actor/evidence for a maintenance retry after restart. System timeouts and
  other automatic/background maintenance plus legacy calls deliberately have no Phase 6 initiating
  actor/snapshot; an already timeout-originated cancellation cannot later adopt a caller identity.
- Phase 6 audits cover the protected principal-facing action families. Lower-level Agent protocol,
  background, and compatibility paths may retain their Phase 5 event/journal trail rather than a
  duplicate Phase 6 principal event.
- OIDC pending state is process-local, mapping is by a configured username claim rather than a
  stored issuer/subject binding, and there is no JIT provisioning or external-group mapping.
- Cookie/CSRF policy, trusted-proxy resolution, and a post-login browser redirect are unfinished;
  the bearer API's security-header baseline is implemented.
- Rate limiting currently protects local password login; OIDC initiation/callback, bearer-token
  authentication, and broader public-API throttling do not have an equivalent general limiter.
- Legacy tokens have no implemented automatic migration/deadline.

Keep this release on a trusted private network; do not expose it as a public multi-tenant service.

## Authentication decisions

Local login uses generic invalid-credential errors to avoid username enumeration. Suspended
organisations and inactive users/service accounts fail authentication. Session refresh rotates the
bearer secret. Service credentials support expiry, revocation, source CIDRs, and restrictions.

OIDC uses authorization code plus S256 PKCE. State is removed before code exchange so a callback
cannot be replayed even after an exchange failure. The control plane validates provider issuer,
RS256 signature, audience/authorized party, nonce, subject, expiry, and optional time bounds before
looking up an existing `OIDC` user in the selected organisation. It does not create users. Pending
transactions are process-local, so multi-process deployments require same-process callback routing
until a shared transaction store exists. See [OpenID Connect](OIDC.md).

Bearer credentials belong only in authorization headers or protected environment/native secret
stores. They must not appear in URLs, YAML, CLI arguments, logs, audit metadata, issue trackers, or
workflow definitions.

On macOS, `labctl` invokes Keychain's `security` command and supplies the versioned session bundle
on standard input with the password option last. On Linux it uses `secret-tool`. Stored sessions
handle one explicit expiry response by serializing refresh, replacing the bundle, and retrying the
request once. A failed post-rotation native write removes stale local data and triggers
best-effort server logout. If no supported store is available, interactive login fails rather than
writing plaintext. `LAB_PLATFORM_TOKEN` remains the explicit non-interactive route, overrides any
stored token, and is neither rewritten nor auto-refreshed.

## Authorisation decisions

Named-resource route dependencies resolve `AuthorisationResource` from trusted Agent, bench,
reservation, workflow, or operation records and compare exact resource assignments. Permissions
are additive; credential restrictions only reduce the result. A missing, expired, future,
cross-organisation, or mismatched assignment grants nothing.

For benches and workflows, persisted visibility/role policies filter otherwise-applicable grants;
an explicit allowed-team list can add bench-read access. Organisation owners/admins retain the
documented policy override, while `ADMIN_ONLY` workflows reject non-admin scoped assignments.

The major API-facing operational records and routes persist/query the authenticated organisation,
and focused integration tests reject cross-tenant collections, reads, and mutations in both tenant
directions. Agent, bench, workflow, operation, and reservation lists evaluate every returned
resource; CI lists apply owner-or-organisation-grant rules; artifact lists resolve and evaluate
each durable parent. Protected application services repeat the critical decisions, including the
Agent/bench/workflow/operation/CI read/catalog facade and service-level workflow-registration
check/audit. Remaining boundary work is concentrated in lower-priority/internal storage and
explicit compatibility paths.

`hide_unauthorised_resources` is active on named Agent, bench, workflow, reservation, and operation
routes. The permission denial is audited first and then returned through the resource's ordinary
not-found error (`404`) when hiding is enabled, avoiding a separate existence oracle. With hiding
disabled, the same authenticated denial is a structured `403` containing safe details such as
`required_permission`. Missing/invalid authentication remains `401`. Organisation-scoped
collection filtering and the documented per-item resource-policy filtering are active. Artifact
named denials use the same policy and artifact lists silently filter inaccessible parents.

## Distributed commands

The dispatch record can contain principal ID/type/display name/organisation and an authorisation
snapshot naming permission, resource, granting assignment IDs, and evaluation time.
Removing a role blocks new commands; an already safe running command need not be cancelled, but its
original decision remains auditable.

Identity-backed manual and workflow commands populate and serialize `ActorContext` to the Agent.
CI-launched workflow commands do the same using the session's durable initiating principal and the
current authenticated start context. Authenticated operation/CI cancellation, reconciliation,
inventory-refresh, and drain/undrain control messages carry matching actor/snapshot fields and
reject a payload whose top-level snapshot ID differs from the actor's snapshot ID. Bench action
routes persist their exact decision snapshot. Workflow launch persists the workflow decision, then
the coordinator independently checks both the workflow and each candidate bench before reservation,
transfer, or dispatch; the selected-bench decision is not separately snapshotted. CI sessions
retain their initiating principal and enforce owner/grant lifecycle rules. Background cancellation
retries reuse a principal-requested CI cancellation's persisted actor/`CI_SESSION` snapshot after
restart, but a system timeout or other maintenance action with no identity evidence remains
unattributed. An already timeout-originated `CANCEL_REQUESTED` session cannot adopt a later caller's
identity. Legacy messages likewise omit actor/snapshot fields rather than manufacturing a Phase 6
principal.

At the command acceptance boundary, an exact idempotent retry remains tied to stable principal
identity, tenant, request content, and required permissions that remain available under credential
restrictions. Workflow replay additionally fingerprints the effective restriction set. A rotated
authentication handle or fresh route snapshot does not make accepted work new: replay returns the
original command and its original durable snapshot. Changed content, another principal, or
credential restrictions that no longer include the required permissions are rejected. HTTP route
and workflow-coordinator checks still evaluate current authorisation before reaching that replay
boundary, and every new command requires current authorisation.

For Agent-level controls, the runtime appends a principal-linked Agent timeline intent before it
enqueues the in-memory WebSocket message. The existing protocol journal separately hashes a message
after the writer successfully sends it. Together they distinguish an authorised intent from a wire
send, but they are not a transactional outbox: restart-time replay of arbitrary drain, refresh, or
reconciliation messages would require a dedicated control-intent state machine and acknowledgement
protocol.

## Audit and privacy

Audit outcomes include succeeded, failed, and denied. Metadata is bounded and strips secret-like
keys, firmware content, and serial logs. Organisation scoping is mandatory. The audit log is not a
secrets vault, serial archive, or firmware store.

The monitor loop deletes audit events older than `audit.retention_days` in bounded batches and
prunes login-attempt rows after the rate-limit window. Ordinary audit read APIs remain read-only;
retention deletion is a separate maintenance path. Identity administration, resolvable login
failures, permission denials, access-policy changes, and protected
Agent/enrollment/bench/reservation/workflow/operation/CI/artifact actions are audited. Raw Agent
protocol and background maintenance events continue to use their existing journals where no Phase
6 principal initiated the action.

## Development mode

`development.auto_login_user` is valid only when development mode is explicit and both bind and
public hosts are loopback. The checked-in loopback configuration demonstrates that validation. The
runtime resolves that existing active user in the default organisation when a request has no
bearer, then performs the same permission evaluation as an authenticated session. It does not
create a user, bypass RBAC, or fall back to anonymous access if the configured user is unavailable.
Startup emits a prominent warning whenever this setting is configured. The narrowly constrained
first-legacy-token bootstrap remains an exception: on a fresh loopback deployment with
compatibility enabled and no legacy token, the existing bootstrap guard runs before auto-login
resolution.

Never copy the loopback configuration into a non-loopback deployment. Production should use
PostgreSQL, verified TLS, protected database credentials, a private network, and the production
configuration as a starting point.

## Out of scope

SAML, SCIM, LDAP sync, nested organisations, guest access, custom/attribute-based policy languages,
impersonation, delegated administration, hardware security modules, a secrets vault, multi-region
identity replication, and high availability remain outside Phase 6.
