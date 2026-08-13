# Roles and permissions

Phase 6 uses a fixed additive RBAC model. Custom roles, policy expressions, and explicit deny
rules are intentionally out of scope.

The role mappings and evaluator are implemented and tested. Named operational routes apply them to
trusted exact resources, the main operational collections filter every item, and protected command,
reservation, workflow, CI, Agent lifecycle/enrollment, runtime lifecycle, and artifact services
independently enforce their decisions.

## Permission vocabulary

```text
organisation:read       organisation:manage
users:read              users:manage
teams:read              teams:manage
roles:read              roles:manage
service_accounts:read   service_accounts:manage
credentials:create      credentials:revoke
agents:read             agents:manage            agents:drain
benches:read            benches:manage            benches:reserve
benches:operate         benches:flash             benches:reset
benches:serial
workflows:read          workflows:run             workflows:manage
operations:read         operations:cancel
artifacts:read          artifacts:write           artifacts:delete
ci:sessions:create      ci:sessions:read          ci:sessions:cancel
audit:read
```

Unknown permissions are not granted.

## Built-in role matrix

| Role | Implemented permissions |
| --- | --- |
| `ORGANISATION_OWNER` | Every permission above |
| `ORGANISATION_ADMIN` | Every permission except `organisation:manage` |
| `LAB_ADMIN` | Organisation read; full Agent/bench/workflow/operation/artifact/CI operation and audit read |
| `OPERATOR` | Agent read; bench read/reserve/operate/flash/reset/serial; workflow read/run; operation read; artifact read/write |
| `WORKFLOW_RUNNER` | Bench read/operate; workflow read/run; operation read; artifact read/write; CI session create/read/cancel |
| `RESERVER` | Bench read and reserve |
| `VIEWER` | Bench read and operation read |
| `AUDITOR` | Organisation, Agent, bench, workflow, operation, and audit read |

Organisation membership roles are evaluated in addition to assignments: `OWNER` maps to all
permissions, `ADMIN` to organisation-admin, `MEMBER` to `organisation:read`, and organisation
`VIEWER` to organisation/bench/operation reads.

The mapping is code, not configuration. Changing it is a product/security change requiring matrix
tests and migration review.

## Subjects and scopes

Assignments can target a `USER`, `SERVICE_ACCOUNT`, or `TEAM` and one resource:

- `ORGANISATION`
- `AGENT`
- `BENCH`
- `WORKFLOW`

The implemented inheritance rules are:

```text
organisation assignment -> every resource in that organisation
Agent assignment        -> that Agent and benches with its trusted parent_agent_id
bench assignment        -> that exact bench
workflow assignment     -> that exact workflow
team assignment         -> users whose organisation-scoped membership includes that team
```

An Agent ID is not inferred from a client-provided bench string. The caller of the evaluator must
construct `AuthorisationResource` from trusted repository data, including `parent_agent_id` for a
bench.

## Effective permission algorithm

1. Fail closed if principal and resource organisations differ.
2. Load the user's organisation membership and team IDs.
3. Load assignments for the direct principal and those teams within that organisation.
4. Remove future, expired, cross-organisation, wrong-subject, and non-applicable assignments.
5. Union the permissions from organisation membership and remaining roles.
6. If the bearer credential has restrictions, intersect the union with that restriction set.
7. Grant only when the requested permission remains in the result.

The decision includes effective roles, permissions, and assignment IDs that granted the requested
permission. Those IDs can be persisted in an authorisation snapshot for remote-operation audit.

## Access policies

The evaluator consults persisted bench and workflow policies after selecting otherwise-applicable
assignments. Visibility and required-role rules narrow the additive role result, while an explicit
allowed-team list can grant bench read; this is not a general policy language.

Bench visibility behaves as follows:

| Visibility | Effect |
| --- | --- |
| `ORGANISATION` | Organisation membership and organisation/Agent/bench assignments retain their normal effect. |
| `RESTRICTED` | Organisation-wide implicit grants are removed for non-admins; only Agent/bench assignments apply. |
| `PRIVATE` | Non-admins require a direct principal assignment on the exact bench; team and Agent inheritance are removed. |

Organisation owners/admins bypass the visibility narrowing. A bench policy's `allowed_team_ids`
grants `benches:read` to members of those teams even when ordinary assignment filtering would not.
Optional `reservation_role` and `operation_role` values add exact role requirements for reserve and
operate/flash/reset/serial decisions respectively; organisation owners/admins satisfy those role
requirements.

Workflow visibility is `ORGANISATION`, `RESTRICTED`, or `ADMIN_ONLY`. `RESTRICTED` removes
organisation-wide implicit grants for non-admins and requires an assignment on the exact workflow.
`ADMIN_ONLY` permits organisation owners/admins only, even when another principal has a scoped
workflow assignment.

When a bench has no stored policy, `authorisation.default_bench_visibility` supplies its visibility
and is enforced at runtime. The supported identity-only management surface resolves the exact
same-organisation resource and requires `benches:manage` or `workflows:manage`:

```console
labctl access-policy bench get home-lab/bench-01
labctl access-policy bench set home-lab/bench-01 \
  --visibility restricted \
  --reservation-role reserver \
  --operation-role operator \
  --allowed-team embedded

labctl access-policy workflow get esp32-ci-test
labctl access-policy workflow set esp32-ci-test --visibility restricted
```

`get` returns `source: DEFAULT`/`configured: false` when no record exists. `set` replaces the
complete policy, resolves each allowed team by UUID/slug/name within the caller's organisation,
takes effect immediately, and audits `BENCH_ACCESS_POLICY_UPDATED` or
`WORKFLOW_ACCESS_POLICY_UPDATED`. The REST equivalents are:

```http
GET /api/v1/access-policies/benches/{bench_id}
PUT /api/v1/access-policies/benches/{bench_id}
GET /api/v1/access-policies/workflows/{workflow_id}
PUT /api/v1/access-policies/workflows/{workflow_id}
```

## Required operation mapping

The required checks include:

| Operation | Required permission |
| --- | --- |
| List/read benches | `benches:read` |
| Create reservation | `benches:reserve` |
| Flash | `benches:flash` and `artifacts:write` on the exact bench |
| Reset | `benches:reset` |
| Read serial | `benches:serial` |
| Run workflow | `workflows:run` on workflow and `benches:operate` on selected bench |
| Drain Agent | `agents:drain` |
| Cancel operation | `operations:cancel` |
| Read audit events | `audit:read` |

These mappings describe the Phase 6 cut line.

## Currently active compatibility bridge

Existing protected control-plane routes recognize Phase 6 credentials before optionally falling
back to Phase 4/5 technical-scope tokens. Major Phase 6 operational collections/lookups are
organisation-scoped, with these resource decisions:

| Route family | Current Phase 6 resource evaluation |
| --- | --- |
| Agent/bench collection reads | `agents:read` or `benches:read` on every returned resource |
| Named Agent read/manage/drain | `agents:read`, `agents:manage`, or `agents:drain` on the exact Agent |
| Named bench read/actions | Exact bench, including trusted parent-Agent inheritance |
| Reservation create | `benches:reserve` on the requested bench |
| Reservation list/read/renew/release | List filters by `benches:read`; named actions use the reservation's exact bench |
| Workflow read by name | Exact workflow |
| Workflow run | `workflows:run` on the workflow, then `benches:operate` on each candidate/selected bench |
| Workflow registration/list | Registration uses the organisation; list filters by `workflows:read` on each workflow |
| Operation list/read/cancel | List/read/cancel evaluate the operation's exact bench |
| Operation reconciliation | `agents:manage` on the operation's parent Agent |
| Artifact list/read/content | `artifacts:read` inherited from each trusted parent; collections filter inaccessible records |
| Artifact upload/transfer | `artifacts:write` inherited from the trusted owner; transfer target Agent is tenant-validated |
| Artifact delete | `artifacts:delete` inherited from the trusted parent for platform-managed artifacts |
| CI create | `ci:sessions:create` from any scoped assignment, with durable principal binding |
| CI start | Stored owner only; `ci:sessions:create` and `workflows:run` on the workflow, then `benches:operate` on the selected bench |
| CI read/cancel/lifecycle | An owner may use its scoped permission; another same-org principal needs the permission on the organisation |

Legacy token administration is specially mapped to `credentials:create` or
`credentials:revoke`. A Phase 6 flash route requires both `artifacts:write` and `benches:flash` on
the bench resource.

This bridge now provides per-resource least privilege on named routes and the main collections.
Protected services repeat command, reservation, workflow, CI, drain/enrollment/runtime-lifecycle,
and artifact checks before side effects. Operation, workflow-run, remote-command, and linked
CI-session artifacts resolve trusted parent resources; linked CI artifacts require session access
plus the artifact permission on both exact workflow and selected bench. `WORKFLOW_STEP` parents
currently fail closed for Phase 6 access, and remote artifacts cannot be deleted. Legacy tokens
have no organisation principal and retain deployment-global compatibility queries.

## Credential narrowing

A service account may have `WORKFLOW_RUNNER` while a particular credential has a smaller
restriction set. The intersection step ensures a credential cannot create permissions absent from
the principal's roles. A credential intended for an end-to-end CI workflow must retain every
permission used by that path, including `ci:sessions:create`, `workflows:run`, and
`benches:operate` and `operations:read`, plus `artifacts:read`/`artifacts:write` when the job uploads
or downloads linked artifacts. Omitting `benches:operate` prevents candidate-bench selection;
omitting operation/artifact permission fails when the CLI polls or transfers that resource. An
empty restriction set grants nothing; `null` means no additional narrowing.

## Debugging a denial

Today, inspect the API error code/details and confirm:

1. The bearer is active and belongs to the expected organisation.
2. The principal or one of the user's teams has a non-expired assignment.
3. The assignment scope matches the trusted resource and parent Agent.
4. The bench/workflow policy permits the assignment and any required role is present.
5. Credential restrictions contain the requested permission.
6. The operation is entering the protected principal-facing service rather than an explicit
   legacy/internal infrastructure path.

The identity-only role API and CLI are implemented. For example:

```console
labctl role list
labctl role permissions ROLE
labctl role assign \
  --subject user:alice \
  --role operator \
  --resource agent:AGENT_UUID
labctl role assign \
  --subject user:bob \
  --role viewer \
  --resource bench:home-lab/virtual-esp32-01
labctl role effective \
  --subject user:alice \
  --resource bench:home-lab/esp32-devkit-01 \
  --permission benches:flash \
  --parent-agent-id AGENT_UUID
labctl role revoke ASSIGNMENT_ID
```

`role effective` returns whether the selected permission is allowed together with all effective
roles, permissions, and granting assignment IDs. `--parent-agent-id` is required to exercise
Agent-to-bench inheritance safely because it must come from trusted inventory in an operational
route. The administration endpoint allows authorised debugging for another user or service
account in the same organisation. When `hide_unauthorised_resources` is false, denial responses
expose the required permission without revealing credential secrets. With the default hiding
setting, named operational resource denials are audited and returned as that resource's ordinary
`404` shape.

The REST equivalents are:

```http
GET    /api/v1/roles
GET    /api/v1/roles/{role}/permissions
GET    /api/v1/role-assignments
POST   /api/v1/role-assignments
DELETE /api/v1/role-assignments/{assignment_id}
GET    /api/v1/permissions/effective
```
