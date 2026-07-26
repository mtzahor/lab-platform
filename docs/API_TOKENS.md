# API tokens

Phase 4 API tokens provide limited machine authentication for CI jobs. Records are stored with a
one-way hash; the plaintext token is returned exactly once when it is created. Tokens have an
owner, explicit scopes, optional expiry, revocation timestamp, and last-used timestamp.

## Create and store a token

Create tokens only from the Agent's trusted management environment. On a new Agent with an empty
token store, the first token can be created without a bearer token. That token must grant every
supported scope and becomes the initial admin-equivalent credential:

```console
labctl token create \
  --name lab-admin \
  --owner lab-operators \
  --scope ci:sessions \
  --scope benches:read \
  --scope reservations:write \
  --scope workflows:run \
  --scope operations:read \
  --scope artifacts:read \
  --scope artifacts:write \
  --expires-at 2026-10-01T00:00:00Z
```

Copy the printed token immediately into an operator-only password manager or secret store. It
cannot be retrieved later. Do not put it in a repository, workflow file, CLI argument, URL, log,
or issue.

Once any token record exists, every token create, list, and revoke request requires an active
bearer token granting all seven scopes. The empty-store exception never reopens, even if all
administrator credentials later become inactive. Export the initial token before creating
narrower credentials:

```console
export LAB_PLATFORM_TOKEN='lp_...'
labctl token create \
  --name reporting-job \
  --owner reporting-job \
  --scope operations:read \
  --scope artifacts:read
```

There is no separate administrator scope in Phase 4. Any token granting all seven scopes can
administer tokens, including the standard all-scope `labctl ci run` credential. Keep a dedicated
admin credential outside CI even when a job token necessarily has the same authority.

The CLI reads the token from `LAB_PLATFORM_TOKEN`:

```console
export LAB_PLATFORM_TOKEN='lp_...'
labctl ci session show SESSION_ID
```

An alternate environment-variable name can be selected without exposing its value:

```console
export DEVICE_LAB_CI_TOKEN='lp_...'
labctl --token-env DEVICE_LAB_CI_TOKEN ci session show SESSION_ID
```

There is deliberately no `--token VALUE` option. Ordinary command-line values can leak through
shell history and process listings.

## Scopes

Grant only the scopes required by the job:

| Scope | Allows |
| --- | --- |
| `benches:read` | Inspect and select benches |
| `reservations:write` | Acquire, extend, and release CI reservations |
| `workflows:run` | Launch and cancel registered workflows |
| `operations:read` | Read workflow/operation status and JSON or JUnit results |
| `artifacts:read` | List metadata and download owned artifacts |
| `artifacts:write` | Upload artifacts to an owned CI session |
| `ci:sessions` | Create, heartbeat, cancel, and finalize CI sessions |

The standard `labctl ci run` flow normally needs all seven. A reporting-only job can use
`operations:read` plus `artifacts:read`. Artifact and session APIs also enforce token-owner
isolation: one token owner cannot access a session owned by another.

## List and revoke

```console
labctl token list --output json
labctl token revoke TOKEN_ID
```

These commands require an active all-scope bearer after bootstrap. Listings never contain the
hash or plaintext. Revocation is immediate for subsequent requests; already-running operations
are still brought to a safe stop by cancellation/cleanup. The Agent refuses to revoke the last
active all-scope token. Keep a second, non-expired admin credential and rotate by creating a
replacement, updating the provider secret, verifying one run, and then revoking the old record.
The last-admin check cannot prevent every administrator credential from expiring.

## HTTP use

Send the token only in the bearer header:

```http
Authorization: Bearer lp_...
```

Example:

```console
curl --fail --silent --show-error \
  -H "Authorization: Bearer ${LAB_PLATFORM_TOKEN}" \
  "${LAB_PLATFORM_SERVER}/api/v1/ci/sessions/${SESSION_ID}"
```

Do not enable shell tracing around commands that expand the token. Prefer `labctl`, which builds
the header internally.

## Token administration API

The management routes are:

| Method | Route | Authorization / purpose |
| --- | --- | --- |
| POST | `/api/v1/tokens` | All-scope bearer; empty-store exception for the all-scope first token; return plaintext once |
| GET | `/api/v1/tokens` | All-scope bearer; list public token metadata |
| POST | `/api/v1/tokens/{token_id}/revoke` | All-scope bearer; revoke a token except the last active administrator |

Create request:

```json
{
  "name": "github-device-tests",
  "owner": "github-actions",
  "scopes": [
    "ci:sessions",
    "benches:read",
    "reservations:write",
    "workflows:run",
    "operations:read",
    "artifacts:read",
    "artifacts:write"
  ],
  "expires_at": "2026-10-01T00:00:00Z"
}
```

`expires_at` must include a UTC offset. The first unauthenticated request is accepted only while
the token store is empty and only when `scopes` contains all seven supported values. Thereafter,
the bearer authorizing token administration must itself be active, unexpired, unrevoked, and grant
all seven scopes.

Treat this as a limited bootstrap/admin mechanism: Phase 4 does not introduce a distinct
administrator identity or advanced RBAC. Bind the Agent to a trusted interface and restrict it
with a private network, firewall, or authenticated reverse proxy.

Before the first token is created, legacy operational routes keep their Phase 1–3 local bootstrap
behavior. After the token store contains a record, those routes require bearer authentication too:
bench reads use `benches:read`, reservation and queue operations use `reservations:write`, target
actions and workflow mutations use `workflows:run`, and operation/workflow/event/timeline reads use
`operations:read`. Owner fields cannot be used to impersonate a different token owner, and list
responses are owner-filtered. Revocation does not reopen bootstrap mode, and the API prevents
revocation of the last active all-scope administrator. Maintain a backup administrator and rotate
it before expiry so token administration remains available.

## Security boundary

Phase 4 provides hashed bearer tokens, scopes, expiry, revocation, owner checks, safe artifact
storage, upload limits, and audit events. It does **not** provide SSO, organizational identity,
advanced RBAC, a secrets vault, tenant isolation, brute-force protection for a public service, or
a hardened internet-facing control plane.

- Terminate TLS before traffic leaves a trusted host or network.
- Never expose the Agent or token-management routes directly to the untrusted public internet.
- Give CI runners network access only to the Agent they need.
- Store a backup all-scope administrator outside CI and rotate it before expiry.
- Use short expiries and separate owners/tokens for separate automation contexts.
- Redact token-shaped values from runner logs and configured serial patterns.
- Revoke a token immediately after suspected disclosure.

This limited boundary is an intentional Phase 4 cut line, not a claim of production security.

## Audit events

Creation, use, and revocation produce `API_TOKEN_CREATED`, `API_TOKEN_USED`, and
`API_TOKEN_REVOKED` events. Session and artifact actions produce their own structured events. Use
the event API to correlate changes with request IDs without recording plaintext secrets.
