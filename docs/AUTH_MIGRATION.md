# Authentication migration

Phase 6 uses an expand-and-transition migration so existing Phase 4/5 deployments keep working
while identity-bound ownership and authorisation are introduced.

## What schema versions 9 and 10 do

Schema v9 introduces the identity boundary additively:

- creates identity, membership, team, service-account, session, API-credential, assignment, policy,
  snapshot, audit, and login-attempt tables;
- seeds the fixed transitional `default` organisation;
- adds a non-null default `organisation_id` to existing central records;
- adds nullable principal ownership fields to reservations;
- adds nullable actor context and authorisation-snapshot fields to remote commands;
- adds organisation/query indexes.

Schema v10 closes retry and workflow tenant-key collisions:

- workflow definitions use `(organisation_id, name, version)` as their primary key;
- workflow runs and step results use tenant-composite foreign/unique relationships, and the active
  workflow-per-bench index includes the organisation;
- CI create, workflow-launch, and finalization retry keys are organisation-scoped;
- artifact upload retry keys are scoped by organisation and owner;
- reservation and queue retry keys are scoped by organisation and bench; and
- distributed CI launch retry keys are organisation-scoped.

During v9-to-v10 upgrade, workflow run/result organisation IDs are rebound to their referenced
definition/run before tenant foreign keys are installed. Existing rows remain attached to the
transitional organisation unless a trusted relationship identifies another tenant.

These migrations do **not** convert legacy tokens into service accounts, rewrite historical owner
strings, backfill historical remote actor context, or automatically make every old infrastructure
path tenant-native. Agent slugs/global bench IDs and some lower-priority internal tables remain
deployment-global or transitional. New persistence adapters scope the major distributed resources,
and protected Phase 6 services add principal ownership/actor context in application code.

## Legacy token compatibility

The checked-in configuration contains:

```yaml
authorisation:
  legacy_token_compatibility_enabled: true
```

The runtime now recognizes a Phase 6 identity token first and, when this switch is enabled, falls
back to Phase 4/5 token authentication. Disabling it rejects legacy bearer authentication and
disables legacy token create/list/revoke routes. The setting does not schedule a future deadline;
operators must keep it enabled until they have provisioned and verified replacement identities.

Legacy tokens have no organisation principal. Their operational queries and token inventory remain
deployment-global, and a Phase 6 principal with the mapped credential permission can reach that
global compatibility inventory. Keep compatibility available only on a trusted private deployment;
tenant-isolation claims apply to Phase 6 session/service credentials, not legacy bearers.

Existing commands therefore remain supported:

```console
labctl token create --name NAME --owner OWNER --scope SCOPE
labctl token list
labctl token revoke TOKEN_ID
```

See [API tokens](API_TOKENS.md) for their current security boundary.

## Planned token conversion

For each active legacy token owner, migration tooling must:

1. Create or select a service account in the backfilled organisation.
2. Translate old scopes to the least-privilege built-in roles and resources.
3. Use credential restrictions when a role would otherwise be broader than the old scopes.
4. Issue a new identity-bound credential and show its plaintext once.
5. Update the consuming CI secret and verify one run.
6. Revoke the legacy token before the compatibility deadline.

There is no safe universal one-role mapping for every legacy scope combination. For example,
mapping a narrow Agent-administration scope directly to `LAB_ADMIN` could grant unrelated bench,
workflow, artifact, or audit permissions. Migration tooling must compare effective permission sets
and refuse a broader result unless an administrator explicitly redesigns the assignment.

No token-conversion command exists yet. Never claim that schema migration alone migrated secrets:
the old plaintext cannot be recovered from its hash to manufacture a replacement credential.

## Owner-string transition

Reservations retain a readable owner string for compatibility, but identity-backed create, renew,
release, workflow, and reservation-gated operation paths now derive it from the authenticated
display name and persist `owner_principal_id`/`owner_principal_type`. Caller-supplied owner text is
ignored for a Phase 6 principal. Legacy tokens continue to use request owner text. The remaining
safe sequence is:

1. Keep authorising Phase 6 mutations by principal ID/type.
2. Backfill historical rows only where identity can be established without guessing.
3. Rotate every legacy client to an identity credential.
4. Remove the owner-string authorization path only after all clients have migrated.

Historical text with no trustworthy mapping must remain historical data; do not silently attach it
to a same-named user.

## Upgrade checklist

1. Back up the database, artifact metadata, and deployment configuration; verify that the backup is
   restorable.
2. Inventory active legacy tokens, owners, scopes, expiries, CI consumers, workflow
   names/versions, and retry-key producers.
3. Stop the control plane and every concurrent writer. The SQLite v10 workflow-table rebuild must
   run without another process writing; PostgreSQL constraint replacement should also run in a
   controlled maintenance window.
4. Run `lab-control-plane db migrate --config <path>` once with the target release.
5. Verify schema version 12. For SQLite, also run `PRAGMA foreign_key_check`; for PostgreSQL,
   inspect the migration result and tenant workflow constraints through normal database operations.
6. Reconcile counts by organisation for workflows, workflow runs/results, CI sessions, artifacts,
   reservations, and queues. Confirm each run matches its definition tenant and each step matches
   its run tenant. Investigate mismatches rather than editing IDs speculatively.
7. Verify that the same workflow name/version and supported retry key can be used independently in
   two test organisations, and that replay in one tenant never returns the other's resource.
8. Start the control plane privately with legacy compatibility still enabled, bootstrap or recover
   the first owner, and smoke-test local login plus stored-session refresh.
9. Create users, teams, service accounts, access policies, and resource assignments with the
   supported API or `labctl`; do not insert identity rows manually.
10. Rotate CI/provider secrets one consumer at a time, then exercise cross-organisation denial,
    credential revocation, audit attribution, and remote actor propagation.
11. Disable legacy compatibility only after telemetry and inventory prove that no legacy request
    remains. Keep the pre-upgrade backup until the observation window closes.

The current beta has schema v12 (including the earlier v10 identity migration), the
dual-authentication bridge, first-owner bootstrap, identity
and access-policy administration, principal ownership/actor context, parent-inherited artifact
RBAC, and protected operational services. Operators can complete the checklist manually. Automated
legacy-token conversion and an enforced compatibility deadline do not yet exist.
