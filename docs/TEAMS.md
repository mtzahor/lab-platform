# Teams

Teams group users so one role assignment can grant the same resource access to multiple people.
The team model, memberships, persistence, role inheritance, administration API, and CLI commands
are implemented.

## Model

A team belongs to one organisation and has a unique lowercase slug, display name, optional
description, ID, and timestamps. A user can belong to several teams.

Team membership roles are:

- `MANAGER`
- `MEMBER`
- `VIEWER`

These roles describe membership administration intent; they do not themselves grant bench or
workflow permissions in the current evaluator. Operational permission comes from a built-in role
assigned to the team.

Example target state:

```text
team embedded
    -> OPERATOR
    -> Agent home-lab

team validation
    -> WORKFLOW_RUNNER
    -> workflow esp32-smoke-test
```

At evaluation time, a user receives the additive union of active direct assignments and active
assignments for every team ID returned by the organisation-scoped repository. A service account
is not currently expanded through team membership; direct assignment is the intended CI pattern.

## Safety properties

- Team IDs are resolved server-side for the authenticated user.
- Cross-organisation assignments are ignored.
- Future or expired assignments do not apply.
- Team permissions never override credential narrowing.
- There are no nested teams and no explicit deny rules.

## Administration

The identity-only REST surface supports create/list/get/delete plus member add/remove. The CLI can
resolve a team by UUID, slug, or name and a user by UUID or username:

```console
labctl team create --slug embedded --name "Embedded Team"
labctl team list
labctl team show embedded
labctl team add-member embedded --user alice --role member
labctl role assign \
  --subject team:embedded \
  --role operator \
  --resource agent:AGENT_UUID
labctl team remove-member embedded --user alice
labctl team delete embedded
```

All lookups and mutations remain scoped to the caller's organisation and require the matching
`teams:*` or `roles:*` permission. The REST API can update a team's slug, name, and description;
the current CLI family does not expose that PATCH operation.

The REST routes are:

```http
GET    /api/v1/teams
POST   /api/v1/teams
GET    /api/v1/teams/{team_id}
PATCH  /api/v1/teams/{team_id}
DELETE /api/v1/teams/{team_id}
POST   /api/v1/teams/{team_id}/members
DELETE /api/v1/teams/{team_id}/members/{user_id}
```

See the [Phase 6 team-access demo](PHASE_6_TEAM_DEMO.md) for a complete example in which `alice`
inherits Agent-scoped Operator and workflow-scoped Workflow Runner roles from two teams, while a
Viewer and a CI service account receive different exact-resource access.
