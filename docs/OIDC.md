# OpenID Connect

Phase 6 implements an OpenID Connect authorization-code login with PKCE for pre-provisioned Lab
Platform users. It is a bearer-session flow: the callback returns the same access/refresh session
payload used by local authentication. It does not create a browser cookie or redirect to a product
frontend after login.

## Configuration

```yaml
identity:
  oidc:
    enabled: true
    issuer_url: https://identity.example.com
    client_id: lab-platform
    client_secret_env: LAB_PLATFORM_OIDC_CLIENT_SECRET
    scopes:
      - openid
      - profile
      - email
    username_claim: preferred_username
    transaction_ttl_seconds: 600
    clock_skew_seconds: 60
```

Export the client secret through the named environment variable before starting the control plane:

```console
export LAB_PLATFORM_OIDC_CLIENT_SECRET='<provider-issued-secret>'
```

The configuration stores only the environment-variable name. When OIDC is enabled,
`issuer_url`, `client_id`, and `client_secret_env` are required, `identity.enabled` must also be
true, and the scopes must include `openid`. Issuer and discovered provider endpoints require HTTPS
except for loopback development. Scopes must be non-empty and unique. Transaction lifetime is
bounded to 60-1,800 seconds and accepted clock skew to 0-300 seconds.

The checked-in development and PostgreSQL examples keep OIDC disabled.

## Login flow

Start a login with:

```text
GET /api/v1/auth/oidc/login?organisation_slug=default
```

`organisation_slug` is optional and defaults to `identity.default_organisation_slug`. The control
plane discovers the provider, verifies that the discovered issuer exactly matches configuration,
creates single-use state and nonce values, and redirects with an S256 PKCE challenge. The callback
URI is derived from `control_plane.public_url`:

```text
GET /api/v1/auth/oidc/callback
```

On callback, the control plane consumes state before exchanging the authorization code, sends the
PKCE verifier to the token endpoint, retrieves the provider JWKS, and validates the returned ID
token. Validation is deliberately narrow: RS256 only, an unambiguous RSA signing key, exact issuer,
audience and authorized-party rules, signature, expiry, optional not-before/issued-at bounds,
nonce, and a non-empty subject. Provider response bodies and token material are not included in
public errors or logs.

Pending login state is short-lived, single-use, and process-local. A multi-process deployment must
therefore keep a login and its callback on the same process; a shared transaction store is not yet
implemented.

## User mapping

OIDC does not perform just-in-time provisioning. Before login, an administrator must create an
OIDC user and organisation membership through the identity-only administration API or CLI:

```console
labctl user create \
  --username alice \
  --display-name "Alice" \
  --email alice@example.com \
  --authentication-source oidc \
  --organisation-role member
```

The REST equivalent is `POST /api/v1/users` with `authentication_source: "OIDC"` and no
`password`. `LOCAL` remains the default source and requires a password; an OIDC create request must
omit it. The CLI neither prompts for a password nor permits `--password-env` for an OIDC user, and
`user reset-password` rejects OIDC users.

The configured `username_claim` (`preferred_username` by default) is case-normalized and matched to
the pre-provisioned user's `username`. The user and organisation must both be active.

The validated OIDC `sub` claim is required, but Phase 6 does not persist or map by the
issuer/subject pair. Administrators must therefore choose a provider claim whose value is stable
and unique within each Lab Platform organisation. Automatic user provisioning,
external-group-to-role mapping, account linking, and multiple-provider routing are not part of this
phase.

Successful mapping issues a normal Lab Platform session. Unknown users, local-auth users, disabled
users, and suspended organisations are rejected without creating accounts. Successful login and
provider/mapping failures are written to the selected organisation's audit history once that
organisation can be resolved.

## Operational limitations

- The callback returns JSON bearer credentials; cookie login, CSRF policy, and a post-login browser
  redirect are not implemented.
- Pending state is in process memory rather than a shared database.
- Provider support is intentionally limited to discovery, client-secret authentication, and RS256
  ID tokens; provider-specific extensions are not negotiated.
- Username-claim mapping is pre-provisioned and organisation-specific; there is no JIT lifecycle or
  external-group synchronization.

Unit tests use local signed fixtures and a fake provider. Integration tests exercise redirect,
PKCE, state replay rejection, token validation, mapping, disabled/unmapped users, and provider
failures without internet access or a live identity provider.
