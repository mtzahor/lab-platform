# Identity

Phase 6 represents every human or automation caller as an organisation-scoped principal. Models,
authentication services, persistence, first-owner bootstrap, auth/admin HTTP integration, and
admin CLI workflows are implemented. Named operational routes perform trusted exact-resource
checks, covered collections apply per-item RBAC, and protected command, reservation, workflow, CI,
Agent lifecycle/enrollment, runtime lifecycle, and artifact services independently recheck their
identity decisions.

## Concepts

| Concept | Purpose | Current state |
| --- | --- | --- |
| Organisation | Tenant and data-isolation boundary | Model, persistence, show/update API and CLI implemented |
| User | Human account using local auth or OIDC | Login and source-aware administration API/CLI implemented |
| Service account | Non-human CI or automation identity | Credential and administration API/CLI implemented |
| Principal | Uniform authorisation view of a user or service account | Implemented |
| Team | Group of users that inherits role assignments | Persistence, evaluator, administration API/CLI implemented |
| Membership | Organisation or team relationship and broad role | Implemented in model/persistence |
| Role assignment | Fixed role attached to a subject and resource | Persistence, evaluator, API/CLI implemented |
| Session | Revocable local/OIDC user login | Domain service and persistence implemented |
| API credential | Revocable service-account bearer credential | Issue/list/revoke API and CLI implemented |
| Audit event | Append-only record of actor, action, target, and outcome | Persistence and read API implemented |

## Principal boundary

Authorisation code consumes this shape regardless of authentication method:

```text
id
type: USER | SERVICE_ACCOUNT
organisation_id
display_name
```

Agent authentication is deliberately separate. An Agent is infrastructure owned by an
organisation; it is not a service account and does not receive human/team roles. The control plane
evaluates user or service permissions before dispatch. Identity-backed manual and workflow remote
commands, including workflows launched from a principal-owned CI session, carry the initiating
principal's `ActorContext` and persist the exact granting authorisation-decision snapshot.
Principal-initiated command/CI cancellation, operation reconciliation, Agent inventory refresh,
and drain/undrain control messages carry matching actor/snapshot evidence. CI cancellation persists
the exact `CI_SESSION` decision and initiating actor atomically with `CANCEL_REQUESTED` so
restart-time delivery retry can reuse it. A timeout-originated cancellation cannot later adopt a
caller's identity.
Exact idempotent replay remains bound to the stable principal, tenant, request content, and
still-allowed required permissions and retains the snapshot accepted with the original command.
Workflow replay also fingerprints effective credential restrictions. System timeout/maintenance
without identity evidence and legacy paths deliberately have no Phase 6 initiating actor or
snapshot.

## Status values

- Organisations: `ACTIVE`, `SUSPENDED`, `ARCHIVED`.
- Users: `ACTIVE`, `DISABLED`, `LOCKED`, `DELETED`.
- User authentication sources: `LOCAL`, `OIDC`.
- Service accounts: `ACTIVE`, `DISABLED`, `REVOKED`.

Authentication services reject inactive organisations, non-active users, and non-active service
accounts. Username and organisation slug lookup is normalized case-insensitively. Usernames are
unique within an organisation rather than globally.

## Organisation resolution

The intended request flow derives organisation context from the authenticated principal. Clients
must not send an organisation ID to widen an ordinary request. Repositories expose
organisation-scoped methods and the Phase 6 schema adds `organisation_id` to central records.

The major distributed persistence adapters expose organisation-scoped methods, and Phase 6 HTTP
collections/lookups propagate the authenticated organisation through them. Agent, bench, workflow,
operation, reservation, CI, and artifact collection results apply their documented
resource/ownership filters. Artifact operations resolve trusted operation, workflow-run,
CI-session, command, Agent, and bench parents before evaluating permissions. Internal/background
paths and lower-priority tables remain transitional. The schema default is a migration bridge, not
evidence that every cross-organisation path passes. See
[Organisations](ORGANISATIONS.md).

## Provisioning and administration

Models and repositories support creating users, password records, memberships, teams, service
accounts, credentials, and assignments. `lab-control-plane bootstrap-admin` is the supported
first-owner path. After login, the identity-only administration API and `labctl organisation`,
`user`, `team`, `service-account`, `role`, and `access-policy` families manage the current
organisation. They do not accept legacy tokens. See
[Local authentication](LOCAL_AUTH.md#bootstrap-administrator) and
[CLI](../CLI.md#phase-6-administration-commands).

`labctl user create` and `POST /api/v1/users` accept a `LOCAL` or `OIDC` authentication source.
Local users require a password; OIDC users must omit it and are created atomically with their
organisation membership. OIDC password reset is rejected. There is no JIT provisioning, so the
pre-provisioned username must match the configured provider claim; see
[OpenID Connect](OIDC.md).

Do not modify identity tables manually in a production deployment. Bootstrap the first owner with
the control-plane command, authenticate with `labctl auth login`, and use the supported admin
surface. There is deliberately no cross-organisation super-administrator.
