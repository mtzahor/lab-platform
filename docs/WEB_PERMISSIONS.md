# Web permissions

The dashboard is permission aware, but it is not an authorisation engine. Its job is to avoid
offering actions that are known to be unavailable and to explain denials. The control plane remains
the only source of truth for organisation, team, direct-role, resource-policy, ownership, parent
inheritance, credential restriction, and expiry decisions.

## Inputs used by the UI

After sign-in, `/api/v1/auth/me` supplies the current organisation, membership role, fixed role
names, and high-level effective permissions. Presentation-friendly resource responses can also
include an action map, for example:

```json
{
  "permissions": {
    "reserve": true,
    "flash": false,
    "reset": false,
    "run_workflow": true
  }
}
```

Those flags already include server-side inheritance and resource policy. The frontend must not
derive a bench permission from an Agent role or attempt to merge team assignments itself.

## Navigation and action behavior

- A section is omitted from primary navigation when no relevant high-level read permission is
  present.
- A detail page uses resource-specific flags for reserve, queue, flash, reset, serial, workflow,
  cancel, reconcile, drain, and administrative actions.
- Read-only information can remain visible while mutation controls are absent.
- A disabled action explains transient state (offline, reserved, stale, incompatible) separately
  from permission. When the action would reveal hidden policy, it is simply absent.
- Direct navigation to a disallowed section shows a permission page, but the API response remains
  decisive.
- A server `403` is shown as a denial with its request ID. A server `404` does not disclose whether
  the resource exists outside the caller's visibility.

## Fixed roles

The fixed roles are `ORGANISATION_OWNER`, `ORGANISATION_ADMIN`, `LAB_ADMIN`, `OPERATOR`,
`WORKFLOW_RUNNER`, `RESERVER`, `VIEWER`, and `AUDITOR`. Organisation membership also has
`OWNER`, `ADMIN`, `MEMBER`, or `VIEWER`. Exact permissions are defined by the backend and exposed
through the roles API; do not hard-code a second role matrix in client logic.

Broadly:

- organisation owners/admins manage identities and access;
- lab admins manage Agents and benches as well as operations;
- operators reserve and operate permitted benches and run permitted workflows;
- workflow runners and reservers have narrower execution/reservation scopes;
- viewers and auditors inspect only their allowed operational/audit surfaces.

Resource policies and assignments can make a broad description narrower or broader for one named
resource. Credential restrictions can only narrow a service account's effective permission.

## Administration

Administration routes and mutation buttons require the corresponding `users:*`, `teams:*`,
`service_accounts:*`, `credentials:*`, `roles:*`, `organisation:*`, or `audit:read` permission.
Role and policy forms send IDs and enum values to the server and display the returned effective
access; they do not promise an assignment will grant access before backend validation.

New credentials are shown once. The dashboard never expects the API to return a stored secret
later. Revoking roles, credentials, sessions, or principals uses an explicit confirmation and
refreshes affected access queries.

## Verification scenarios

At minimum, test these with real API decisions:

1. An Operator can reserve an assigned SimLab bench and run an assigned workflow.
2. The same Operator cannot mutate an unassigned restricted bench.
3. A Viewer sees inventory/operation history but no reserve, flash, reset, workflow-run, or Agent
   drain controls.
4. A Lab Admin can drain an Agent but cannot implicitly manage organisation ownership.
5. An Organisation Owner can inspect effective access and the audit record for a denied direct API
   request.
6. A service-account credential narrowed to CI permissions cannot use browser administration.

See [roles and permissions](ROLES_AND_PERMISSIONS.md) for the backend model and the
[browser demonstration](PHASE_7_BROWSER_DEMO.md) for a complete role-based flow.
