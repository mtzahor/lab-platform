# Identity-bound API credentials

Phase 6 API credentials authenticate service accounts and derive permission from the principal's
roles. They are distinct from legacy Phase 4/5 scope-bearing API tokens and from Agent gateway
credentials.

## Format and storage

The implemented credential service generates:

```text
lp_<credential-uuid-without-dashes>_<random-url-safe-secret>
```

The credential UUID supports indexed lookup. The database stores the UUID and SHA-256 hash of the
32-byte random secret, never the plaintext token. Authentication compares hashes in constant time.
The issued token is returned by the domain service once.

Metadata includes:

- organisation and service-account principal IDs;
- name, creation time, optional expiry, revocation time, and last-use time;
- up to 64 normalized IPv4/IPv6 CIDR restrictions;
- an optional set of permission restrictions.

An IP-restricted credential fails closed when no source IP is available or the address is outside
every allowed range.

## Permission derivation

Credentials do not carry an independent grant set. The authorisation evaluator computes the
service account's active role permissions and then intersects them with
`permission_restrictions`, when present:

```text
effective = role_permissions intersection credential_restrictions
```

Restrictions may only remove permissions. `null` means no credential-specific narrowing; an empty
set means the credential can authenticate but authorises no protected operation.

## Revocation and expiry

Authentication rejects a revoked or expired credential, a disabled/revoked service account, and a
non-active organisation. Successful use updates credential and service-account `last_used_at`.
Revocation is checked on every new authentication; it does not retroactively erase audit history
or necessarily cancel a safely running operation.

## HTTP and CLI administration

Credential domain and persistence services are implemented, and existing protected control-plane
routes recognize this token format and authenticate its service account. Named Agent, bench,
reservation, workflow, and operation routes evaluate exact Phase 6 resources, while their main
collections filter every returned item. Workflow/CI orchestration independently rechecks the
service principal, credential restrictions, resources, and stored CI owner. Protected command,
reservation, Agent lifecycle/enrollment, runtime lifecycle, and artifact services also recheck the
authenticated context before side effects. Artifact routes inherit `artifacts:*` from trusted
parents instead of treating the organisation as the ordinary resource. Major operational
repositories and route lookups carry the service account's organisation; explicit legacy/internal
paths and lower-priority background storage remain transitional.

The identity-only administration surface is implemented:

```http
POST   /api/v1/service-accounts/{account_id}/credentials
GET    /api/v1/service-accounts/{account_id}/credentials
DELETE /api/v1/credentials/{credential_id}
```

Creation returns `{credential, token}`. The credential object excludes `secret_hash`; the
plaintext token is shown once. List responses never reproduce it. The `CREDENTIAL_CREATED` audit
event attributes the action to the authenticated administrator and identifies the new credential;
it does not misattribute creation to the service account receiving the secret. The matching CLI is:

```console
labctl service-account credential create \
  --service-account github-ci \
  --name github-main \
  --expires-at 2026-09-01T00:00:00Z \
  --allowed-ip 192.0.2.0/24 \
  --permission ci:sessions:create \
  --permission ci:sessions:read \
  --permission ci:sessions:cancel \
  --permission workflows:run \
  --permission benches:operate \
  --permission operations:read \
  --permission artifacts:read \
  --permission artifacts:write
labctl service-account credential list --service-account github-ci
labctl service-account credential revoke CREDENTIAL_ID
```

The administration routes require a Phase 6 identity bearer and matching permission; legacy
tokens are rejected. Existing operational control-plane routes can still accept legacy tokens
while the configured compatibility switch is enabled.

The example assumes workflow- and bench-scoped principal assignments. Those built-in roles provide
artifact permissions on the same resources, while the credential restrictions retain only the
read/write operations needed by the CI flow. A restriction cannot manufacture a grant: linked CI
artifacts require session access plus `artifacts:*` on both the exact workflow and selected bench.
Omitting either the role assignment or the restriction fails closed.

Do not generate credentials by inserting a hash manually: correct token construction, random
generation, audit recording, IP normalization, and one-time display belong in this administration
service.

## Safe CI handling

- Put the one-time plaintext only in the provider's protected secret store.
- Inject it as `LAB_PLATFORM_TOKEN`; never add it to CLI arguments or URLs.
- Disable shell tracing while handling it.
- Use separate credentials for separate providers/repos/environments.
- Prefer expiry, IP restrictions where trustworthy, and the smallest restriction set.
- Revoke immediately after suspected disclosure.

See [Service accounts](SERVICE_ACCOUNTS.md), the
[Phase 6 team-access demo](PHASE_6_TEAM_DEMO.md), and
[Authentication migration](AUTH_MIGRATION.md).
