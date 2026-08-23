# Organisations

An organisation is the intended tenant and ownership boundary for every human, service account,
Agent, bench, reservation, workflow, operation, artifact, and audit event.

## Model

Organisations have a lowercase slug, display name, status, ID, and timestamps. Valid statuses are
`ACTIVE`, `SUSPENDED`, and `ARCHIVED`. Local and service authentication reject a non-active
organisation.

Organisation memberships give a user one broad role:

| Membership role | Current domain permissions |
| --- | --- |
| `OWNER` | Every permission |
| `ADMIN` | Organisation-admin permissions; excludes `organisation:manage` |
| `MEMBER` | `organisation:read` only |
| `VIEWER` | Organisation, bench, and operation reads |

Additional operational access comes from resource-scoped role assignments.

## Transitional default organisation

The v9 identity migration, retained in the current upgrade chain (latest schema v11), seeds this fixed row
when missing:

```text
id:   00000000-0000-0000-0000-000000000001
slug: default
name: Default Organisation
```

The expand migration adds a non-null `organisation_id` column with that ID as the default to
existing central records. This preserves old Phase 5 rows while organisation-scoped repositories
are adopted. Agent-local execution journals are intentionally outside this backfill because Agent
authentication is a separate trust domain.

The configuration also exposes `identity.default_organisation_slug` and
`identity.default_organisation_name`. Migration first provides the fixed safe row above; when the
identity-enabled runtime starts, it ensures that same fixed ID has the configured slug/name. This
keeps all backfilled rows attached while allowing a deployment-specific label. Changing the
settings renames that transitional organisation; it does not move rows to a different ID.

## Isolation rule

The completed request path must:

- derive organisation context from the authenticated principal;
- include organisation scope in every database query;
- reject a principal/resource organisation mismatch before evaluating assignments;
- avoid fetching global data and filtering in memory;
- use `404` when configured to hide an inaccessible resource.

The authorisation evaluator fails closed on a cross-organisation resource. Identity repositories
are scoped, and the major Agent/bench/reservation/command/operation/workflow/artifact/CI persistence
adapters now carry and query organisation IDs. Phase 6 HTTP collections/lookups pass the
authenticated organisation through these adapters, validate key cross-resource ownership, and
apply per-item RBAC to Agent, bench, workflow, operation, and reservation collections. CI
collections apply session ownership rules; artifacts inherit trusted operation/workflow/CI/bench
parents and filter inaccessible rows. Protected application services recheck critical decisions.
Schema v10 makes workflow definition/run/result relationships and supported
CI/artifact/reservation/queue retry keys tenant-scoped. Internal/background callers and several
lower-priority tables remain transitional, while human-readable Agent slugs/global bench IDs remain
deployment-global. Full public multi-tenant hardening is therefore still beyond this alpha.

## Administration

The identity-only API exposes `GET /api/v1/organisation` and `PATCH /api/v1/organisation`; the CLI
wraps them as:

```console
labctl organisation show
labctl organisation update --name "Embedded Lab"
```

Both operate only on the authenticated principal's organisation and require the corresponding
organisation permission. There is no cross-organisation super-administrator in Phase 6. Create or
explicitly recover the first owner with `lab-control-plane bootstrap-admin`, described in
[Local authentication](LOCAL_AUTH.md#bootstrap-administrator).

Before a production upgrade, back up the database, stop concurrent writers, run
`lab-control-plane migrate`, verify schema version 11 plus tenant/foreign-key counts, bootstrap an
owner, and keep legacy-token compatibility enabled until every consumer has an identity-bound
replacement. Follow the complete [authentication upgrade checklist](AUTH_MIGRATION.md#upgrade-checklist).
