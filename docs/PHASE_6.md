# Phase 6: identity, teams, and access control

Phase 6 changes Lab Platform from owner strings and broad technical scopes to authenticated human
and service principals, organisation isolation, additive resource-scoped roles, and a durable audit
trail. The target security flow is:

```text
authenticate principal
    -> resolve organisation
    -> evaluate permission on a trusted resource
    -> perform or deny the action
    -> record the outcome
```

## Current implementation status

Phase 6 is an **incremental 0.7.0-alpha foundation**, not a completed security boundary. The
following pieces are implemented and tested:

- Strict identity, local-authentication, OIDC, session, authorisation, audit, rate-limit, and
  development settings.
- Identity models for organisations, users, service accounts, principals, teams, memberships,
  sessions, credentials, role assignments, access policies, snapshots, and audit events.
- Salted, versioned scrypt password hashing; local login/session, refresh, logout, revocation, and
  rate-limit domain services.
- OIDC authorization-code login with discovery, single-use state/nonce, S256 PKCE, RS256 ID-token
  validation, configurable username-claim mapping to pre-provisioned OIDC users, and normal Lab
  Platform session issuance.
- Identity-bound API credential issuance, validation, expiry, revocation, IP restrictions, and
  permission narrowing in domain code.
- Control-plane local login, refresh, logout, current-principal, session-list, and session-revoke
  routes; existing protected routes accept Phase 6 sessions/service credentials before optional
  legacy-token fallback.
- Identity-only administration REST routes for the organisation, users, teams and memberships,
  service accounts and credentials, roles and assignments, effective permissions, bench/workflow
  access policies, and audit reads.
- Matching `labctl` administration commands for organisation, user, team, service-account,
  credential, role, effective-permission, access-policy, and audit workflows.
- Fixed built-in role mappings and additive evaluation of organisation, team, direct, Agent,
  bench, and workflow assignments, narrowed by persisted bench/workflow access policies and the
  configured default bench visibility.
- Trusted resource resolution and resource-scoped permission evaluation on named Agent, bench,
  reservation, workflow, and operation routes. Bench action and workflow dispatches persist the
  authorisation decision snapshot used by the remote command; denied Phase 6 checks are audited
  and can be collapsed to resource-specific `404` responses.
- Per-item resource RBAC/access-policy filtering on Agent, bench, workflow, operation, and
  reservation collections. CI collections apply principal ownership plus organisation-grant rules.
- Phase 6 success and denial audit events across identity administration, access-policy changes,
  authentication, Agent enrollment/lifecycle, bench actions, reservations, workflow/operation/CI
  lifecycle, and artifact upload/download/transfer/delete routes, with principal or system
  attribution and bounded metadata.
- Schema version 10 identity tables, default-organisation backfill columns, composite tenant
  workflow definition/run/result relationships, organisation-scoped retry keys for CI, artifacts,
  reservations, queues, and distributed CI launches, and scoped persistence methods for the major
  distributed resources.
- Phase 6 HTTP organisation propagation across the corresponding Agent/bench/reservation/workflow,
  operation, artifact, and CI collections and named-resource lookups, including artifact parent and
  transfer-target validation and organisation-scoped CI bench allocation.
- `lab-control-plane bootstrap-admin`, with confirmed hidden password prompting, named environment
  input, refusal when an owner exists, and explicit recovery mode.
- `labctl auth login`, `logout`, `status`, and `whoami`, including native OS credential storage.
- Principal-bound ownership for Phase 6 reservation/workflow requests, authenticated actor context
  and exact decision snapshots on manual/workflow/CI-launched remote commands, and service-account
  identity on distributed CI sessions.
- Application-service authorisation for identity-facing Agent, bench, workflow, operation, and CI
  catalog reads; workflow registration; command creation/cancellation; reservation lifecycle;
  workflow coordination; distributed CI; Agent drain/enrollment administration; runtime Agent
  lifecycle wrappers; and artifacts. Operational collections apply exact resource or CI
  ownership/organisation filtering inside the facade, including a non-admitting bench view for
  nested Agent summaries. Phase 6 calls carry authenticated context; supported
  compatibility/infrastructure callers must select an explicit legacy or internal escape, and
  conflicting modes are rejected. Checks precede mutations and protected detail reads.
- Workflow execution verifies `workflows:run` on the definition and `benches:operate` on the
  selected bench before reservation, artifact transfer, or remote command. Authenticated launches
  reload the exact tenant/name/version definition from the trusted catalog and reject missing or
  mismatched caller content before input preflight or side effects. Coordinator replay state and
  derived reservation/artifact/command keys are tenant-qualified. CI verifies durable actor
  integrity: inert creation requires scoped `ci:sessions:create` somewhere, only the stored
  principal may start, start requires `ci:sessions:create` on the exact workflow and passes the
  current context to workflow coordination, and own-session read/cancel may use scoped permissions
  while another same-organisation principal needs the corresponding organisation grant.
- Snapshot-safe command/workflow idempotency: exact retries stay bound to stable principal identity,
  organisation, request content, and still-allowed required permissions, ignore fresh
  session/decision-snapshot handles, and return the already accepted work with its original durable
  evidence. Workflow replay also fingerprints effective credential restrictions. Changed content,
  another principal, or credential restrictions that remove a required permission are rejected;
  new work is evaluated against current authorisation.
- Principal-facing Agent drain/undrain, inventory refresh, operation reconciliation, and
  operation/CI cancellation carry matching actor/snapshot evidence on the Agent control payload;
  mismatched or unresolved evidence fails before dispatch. A principal-requested CI cancel persists
  an exact `CI_SESSION` decision (including granting assignments) and initiating actor atomically
  with `CANCEL_REQUESTED`, then verifies its trusted session-to-command route before enqueue; retry
  after restart reuses that evidence. Agent-control and direct-operation-cancellation paths persist
  an attributed timeline intent before socket enqueue.
- Parent-inherited artifact authorisation for platform and remote lists/reads/content, platform
  upload/delete, CI artifact lists, and transfer issuance. Trusted operation, workflow-run,
  CI-session, command, Agent, and bench relationships are resolved before permission checks; lists
  silently filter inaccessible items and named denials follow the configured `404`/`403` policy.
- Bounded runtime pruning for audit-event retention and stale login-attempt rows, loopback-only
  development auto-login through normal RBAC, and a conservative HTTP security-header baseline.

The following remain security-hardening and compatibility work:

- Completing organisation propagation and protected-facade adoption in lower-priority
  internal/background mutation, event/timeline, connection/protocol/reconciliation,
  artifact-transfer, and legacy catalog/backend/firmware/lock paths. Explicit internal escapes are
  required for the migrated application services, but lower layers remain trusted infrastructure.
- Reworking or explicitly retaining deployment-global human-readable identifiers such as Agent
  slugs/global bench IDs. Schema v10 permits the same workflow name/version and the same supported
  retry key in different organisations, but it does not make every historical identifier tenant
  local.
- Automatic/background work without an originating Phase 6 decision, legacy compatibility calls,
  and any path without a Phase 6 initiating principal deliberately omit actor/snapshot attribution.
  A maintenance retry may reuse evidence persisted for a principal-requested CI cancellation.
  An already timeout-originated `CANCEL_REQUESTED` session remains system-attributed and cannot
  adopt a later caller's identity.
  Durable control intents plus the successful-send protocol journal are not a transactional replay
  outbox; replay of arbitrary control messages after restart would require a dedicated state
  machine and acknowledgement protocol.
- Resolving `WORKFLOW_STEP` artifact ownership through a tenant-scoped trusted parent. Phase 6
  access currently fails closed and collection reads filter such records. Platform-managed
  artifacts support deletion; remote-artifact deletion is not a supported operation.
- Legacy-token migration tooling and an enforced compatibility-window end date.
- Removing the deployment-global legacy-token inventory/query trust boundary; compatibility
  requests have no organisation principal and are excluded from Phase 6 isolation guarantees.
- Broadening rate limiting beyond local password login; adding explicit trusted-proxy address
  policy; adding cookies/CSRF/browser redirects only if a browser login is introduced; and extending
  tenant/security coverage through lower-priority background and protocol paths.
- Providing shared OIDC transaction state, durable issuer/subject account binding, JIT lifecycle or
  group mapping if those features are adopted. Current mapping remains pre-provisioned username
  matching.

Consequently, Phase 6 credentials have an enforced principal-facing path across the documented
Agent, bench, reservation, workflow, operation, CI, and artifact APIs. Major records carry
organisation scope; collections apply resource/ownership filtering; and critical application
services recheck identity before side effects. Routes may still fall back to Phase 5 client tokens,
whose deployment-global compatibility queries are outside these guarantees. Do not treat this
alpha as an internet-hardened public multi-tenant deployment yet.

## Configuration foundation

The checked-in loopback configuration enables the Phase 6 settings contract:

```yaml
identity:
  enabled: true
  default_organisation_slug: default
  default_organisation_name: Default Organisation
  local_auth:
    enabled: true
    minimum_password_length: 12
  oidc:
    enabled: false
    scopes: [openid, profile, email]
  sessions:
    access_token_minutes: 15
    session_hours: 12
    maximum_session_days: 7

authorisation:
  default_bench_visibility: organisation
  hide_unauthorised_resources: true
  legacy_token_compatibility_enabled: true

audit:
  enabled: true
  retention_days: 90

security:
  login_rate_limit:
    attempts: 10
    window_minutes: 15
```

These values configure the identity service, auth and OIDC routes, resource-level
permission enforcement, legacy-token fallback, denial auditing, and bounded audit retention.
`default_bench_visibility` is applied when a bench has no stored access policy. OIDC is active only
when explicitly enabled with complete provider settings and a non-empty secret in the named
environment variable. Configuration still does not by itself wire organisation scope through every
operational query. See [Security model](SECURITY_MODEL.md) and [OIDC](OIDC.md) for the exact
boundary.

The loopback file also sets `development.auto_login_user: local-admin`. Configuration validation
allows that value only with `development.enabled: true` and loopback bind/public hosts. Runtime
auto-login resolves that existing active user in the default organisation only when no bearer is
provided, then applies normal Phase 6 authorisation. It never creates the user or grants permissions
outside the user's stored membership and role assignments. A missing or inactive configured user
fails authentication rather than falling back to anonymous access. The sole compatibility
exception is the existing loopback-only, first-legacy-token bootstrap: when compatibility is
enabled and no token exists, that explicit bootstrap check runs before development auto-login so a
fresh database remains recoverable. Runtime startup logs a prominent warning whenever auto-login is
configured.

## Identity and authorisation design

Human users and service accounts become a common `Principal` before permission evaluation. The
principal supplies the organisation; an ordinary client never chooses an arbitrary organisation
ID. Agent credentials remain a separate infrastructure trust domain.

Permissions come from fixed roles. Valid permissions are the union of organisation membership,
direct role assignments, and team assignments. Organisation assignments apply to all resources;
Agent assignments apply to benches only when the server supplies the trusted parent Agent ID;
bench and workflow assignments are exact. Credential restrictions can only intersect that result.
Bench/workflow access policies can filter those grants or require a specific role, and allowed
teams can grant bench read. There are no arbitrary explicit deny rules.

See [Roles and permissions](ROLES_AND_PERMISSIONS.md) for the implemented matrix.

## Authentication and CLI

The CLI surface is implemented:

```console
labctl auth login --server https://lab.example.internal
labctl auth status
labctl auth whoami
labctl auth logout
```

`login` accepts `--username` and `--organisation`; omitted usernames and all passwords are
prompted. There is deliberately no password argument. On macOS the refreshable session bundle is
stored in Keychain through `security`; on Linux it is stored through `secret-tool`/Secret Service.
Stored sessions transparently rotate on one explicit expiry response and retry the request once.
No file fallback is implemented. `LAB_PLATFORM_TOKEN` (or the variable named by `--token-env`)
always takes precedence, is not rewritten, and does not auto-refresh.

The control plane exposes login/logout/refresh/me and user-session list/revoke routes. A fresh
installation can create its first owner with `lab-control-plane bootstrap-admin`, then use the
`labctl auth` commands above. See [Local authentication](LOCAL_AUTH.md) and [CLI](../CLI.md).

## Bootstrap administrator

The supported first-owner flow is:

```console
lab-control-plane bootstrap-admin \
  --config config/control-plane.yaml \
  --username michael \
  --display-name "Michael Tzahor"
```

The command prompts twice for a hidden password by default. Automation may use `--password-env
NAME`; the value is read from that named environment variable and never printed. Optional
`--email`, `--organisation-slug`, and `--organisation-name` values override the configured
defaults. There is deliberately no plaintext password argument.

User/password/owner creation is atomic. The command refuses when an organisation owner already
exists. `--recovery` is an explicit repair path: it may reactivate/update the named user, replace
the password, convert the identity to `LOCAL`, grant `OWNER`, and revoke that user's existing
sessions. Treat recovery as a privileged offline operation and clear any password environment
variable immediately afterward.

## Administration surface

Identity administration is available under `/api/v1` and requires a Phase 6 user-session or
service-account credential; legacy Phase 4/5 tokens are not accepted by these routes. The matching
CLI families are:

```text
labctl organisation show|update
labctl user create|list|show|disable|enable|reset-password
labctl team create|list|show|add-member|remove-member|delete
labctl service-account create|list|show|disable|delete
labctl service-account credential create|list|revoke
labctl role list|permissions|assign|revoke|effective
labctl access-policy bench get|set
labctl access-policy workflow get|set
labctl audit list|show
```

Role and administration services apply their own organisation-scope permission checks and audit
successful changes. Audit history is readable through `GET /api/v1/audit-events` and
`GET /api/v1/audit-events/{event_id}` and through `labctl audit list/show`. See the topic-specific
references below for practical examples.

## Migration boundary

The v9 migration creates the identity tables, seeds a fixed transitional `default` organisation,
and adds organisation/actor/owner columns without breaking Phase 5 rows. Schema version 10 then
makes workflow definition keys tenant-composite, binds workflow runs and step results to the same
tenant, and replaces global CI/artifact/reservation/queue retry uniqueness with organisation-scoped
keys. Identity-backed reservation mutations bind principal ID/type while retaining a readable
display owner; legacy requests continue to use old owner text during compatibility.

Read [Authentication migration](AUTH_MIGRATION.md) before upgrading a shared deployment.

## Validation and remaining definition of done

The current slice proves that local/OIDC sessions and service credentials can authenticate, a team
role can operate an exact bench while a Viewer is denied mutation, a CI service account cannot
spoof its stored requester identity, and an authorised bench command persists both its actor and
the granting authorisation snapshot. The copy-ready
[Phase 6 team-access demo](PHASE_6_TEAM_DEMO.md) exercises that complete human/team/service-account
story on the SimLab ESP32 workflow and reviews both success and denial audit records.

A focused two-organisation integration matrix exercises both tenant directions and proves
non-overlapping Agent, bench, workflow, operation, reservation, CI-session, artifact, and
nested-CI-artifact collections; own-resource reads; foreign-resource `404`s; and rejection of
cross-tenant drain, probe, workflow, cancellation, reservation, CI, artifact-parent, and
transfer-target mutations for Phase 6 principals. Authorisation integration also verifies
per-resource collection filtering, access-policy changes, protected application services, artifact
parent inheritance, and workflow/CI service decisions. Storage tests prove same-named workflow
definitions and supported retry keys coexist across tenants after v10 without cross-tenant replay.

The principal-facing definition of done is implemented, but the deployment is still transitional:
lower-priority background/internal paths and historical tables are not uniformly tenant-native,
some global Agent/bench identifiers remain, and rate limiting is concentrated on local password
login. Automatic/background work without an originating Phase 6 decision and legacy work have no
initiating actor/snapshot; CI-cancellation maintenance may reuse evidence from its principal
request. Durable control intents are not a transactional replay outbox. `WORKFLOW_STEP` artifact
parent resolution and remote-artifact deletion are absent. Legacy-token compatibility remains a
separate deployment-global trust boundary.

The included demo shows a human and CI service account authenticating with different scoped access,
dispatching authorised work with actor context, receiving a safe denial for an unauthorised
mutation, and reviewing both outcomes in organisation-scoped audit history without an owner-string
bypass.

Related references:

- [Identity](IDENTITY.md)
- [Organisations](ORGANISATIONS.md)
- [Teams](TEAMS.md)
- [Service accounts](SERVICE_ACCOUNTS.md)
- [API credentials](API_CREDENTIALS.md)
- [Audit log](AUDIT_LOG.md)
- [Security model](SECURITY_MODEL.md)
- [Phase 6 team-access demo](PHASE_6_TEAM_DEMO.md)
