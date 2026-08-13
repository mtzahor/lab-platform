# Local authentication

Local authentication is implemented across domain service, persistence, control-plane HTTP routes,
the first-owner bootstrap command, and the CLI. Organisation-scoped user creation,
enable/disable, lookup, listing, and password reset are also available through REST and `labctl`.

## Password rules and storage

- Minimum configured length is 12 characters.
- There are no forced composition rules or periodic expiry.
- Passwords are salted and hashed with a versioned stdlib scrypt encoding:
  `scrypt$v1$N$r$p$salt$derived-key`.
- Plaintext passwords are not stored and are hidden from model representations.
- Malformed stored hashes fail closed.
- Login failure messages are intentionally generic.

The defaults use scrypt `N=16384`, `r=8`, `p=1`, a 16-byte random salt, and a 32-byte derived key.
These are current implementation details, not a stable interchange format. Parameter upgrades
will require an explicit rehash strategy.

## Login rate limiting

The domain service counts failed attempts by organisation slug, normalized username, and source IP
within the configured window. Defaults are ten failures in fifteen minutes. Successful login
records an attempt and clears matching failures. The schema includes indexed login-attempt storage.

The HTTP login route applies this limit using the direct request peer address. Deployments behind
a proxy must preserve a trustworthy client-address boundary; forwarded-header trust is not yet a
configurable application policy.

## Sessions

Local login issues an opaque bearer token:

```text
lps_<session-uuid-without-dashes>_<random-secret>
```

Only the session ID and SHA-256 hash of the random secret are stored. Defaults are a 15-minute
access expiry, a 12-hour session horizon, and a seven-day configured maximum. Refresh rotates the
secret and invalidates the previous token. Logout and administrator revocation set `revoked_at`.

The HTTP refresh route rotates the token. For an interactive login the CLI stores one versioned
native-secret bundle containing access/refresh tokens, session ID, access expiry, and maximum
session expiry. The current server uses one rotating opaque token for access and refresh, while the
bundle format also supports a future distinct refresh token.

When a request using a stored login receives one `401` with `SESSION_EXPIRED` or `TOKEN_EXPIRED`,
the client posts the stored refresh token to `/api/v1/auth/refresh`, persists the rotated bundle,
and retries the original request exactly once. Concurrent attempts are lock-serialized. Existing
raw access-token entries are read as the current rotating token and migrate on first successful
refresh. `LAB_PLATFORM_TOKEN` remains higher precedence, is never modified, and does not
auto-refresh.

If rotated credentials cannot be written safely, the CLI deletes stale native data and
best-effort logs out the newly rotated server session. Refresh tokens are never printed or passed
in argv.

## CLI commands

The following commands and request shapes are implemented in `labctl`:

```console
labctl auth login \
  --server https://lab.example.internal \
  --username alice \
  --organisation embedded-lab

labctl auth status
labctl auth whoami
labctl auth logout
```

If `--username` is omitted, `login` prompts for it. The password is always obtained with a hidden
prompt; `--password` does not exist. `--organisation` is sent as `organisation_slug` when present.
The control plane exposes:

```http
POST /api/v1/auth/login
POST /api/v1/auth/logout
POST /api/v1/auth/refresh
GET  /api/v1/auth/me
GET  /api/v1/auth/sessions
DELETE /api/v1/auth/sessions/{session_id}
```

Omitting `organisation_slug` from login selects the configured default organisation. Login and
refresh return `access_token`, bearer type, expiry, public principal/organisation/session data, and
never the stored secret hash. Logout and session revocation return `204`. Only a user session may
list or revoke its sessions; a service credential receives `403` for those operations.

### Credential precedence

1. The environment variable named by `--token-env` (`LAB_PLATFORM_TOKEN` by default).
2. A server-scoped native OS credential.
3. No bearer credential.

The environment value wins even after a successful interactive login. `auth login` warns about
that situation. Logging out an environment token cannot unset the parent shell, so the CLI warns
the operator to remove it.

### Native storage

- macOS uses Keychain through `/usr/bin/security`.
- Linux uses Secret Service through `secret-tool` when installed and configured.
- The token is passed to the storage command over standard input, never in argv.
- The complete versioned bundle is one secret value keyed by normalized server URL, so separate
  control planes do not share sessions.
- There is no plaintext file fallback.

If neither backend is available, interactive authentication fails with guidance to configure the
native store or use `LAB_PLATFORM_TOKEN`. Unauthenticated Phase 5 bootstrap and public health and
version requests remain possible on such headless systems.

## Bootstrap administrator

Create the first organisation owner before interactive login:

```console
lab-control-plane bootstrap-admin \
  --config config/control-plane.yaml \
  --username michael \
  --display-name "Michael Tzahor"
```

The default flow prompts for the password and confirmation without echo. For automation, pass
`--password-env NAME` to read a named environment variable; no plaintext password argument exists.
`--email`, `--organisation-slug`, and `--organisation-name` are optional.

The command atomically creates the user, password, and `OWNER` membership and refuses if an owner
already exists. `--recovery` explicitly repairs an existing owner: for the named user it can
reactivate the account, replace its password and owner membership, and revoke its existing
sessions. Recovery makes the repaired identity a password-backed `LOCAL` user, including when the
existing record used OIDC. Run recovery only with direct database/operator authority.

## User administration

An authorised administrator can create and manage local users without exposing passwords in argv:

```console
labctl user create \
  --username alice \
  --display-name "Alice Operator" \
  --organisation-role member

labctl user disable alice
labctl user enable alice
labctl user reset-password alice
```

Create/reset prompts twice without echo unless `--password-env NAME` selects a secure environment
value. Disabling a user and resetting a password revoke that user's active sessions. The final
organisation owner cannot be disabled.

The same create command pre-provisions an OIDC user when explicitly selected:

```console
labctl user create \
  --username alice-oidc \
  --display-name "Alice OIDC" \
  --authentication-source oidc \
  --organisation-role member
```

OIDC creation neither prompts for a password nor accepts `--password-env`, and password reset is
rejected for an OIDC user. See [OpenID Connect](OIDC.md) for the provider-claim mapping.

The matching identity-only routes are:

```http
GET   /api/v1/users
POST  /api/v1/users
GET   /api/v1/users/{user_id}
PATCH /api/v1/users/{user_id}
POST  /api/v1/users/{user_id}/disable
POST  /api/v1/users/{user_id}/enable
POST  /api/v1/users/{user_id}/reset-password
```

## Remaining work

- Add CLI session list/revoke if retained in the final UX.
- Add browser cookies and a CSRF policy if browser login is added; API security headers are already
  applied to successful and error responses.
- Add configurable trusted-proxy/client-address resolution.
- Extend the existing unit/integration expiry, revocation, disabled-user, and rate-limit coverage
  into a broader deployment-level authentication abuse suite.
- Broaden application throttling beyond local password login to OIDC initiation/callback, bearer
  authentication failures, and public API abuse before treating the service as a public endpoint.
