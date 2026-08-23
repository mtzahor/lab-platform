# Web authentication

The Phase 7 browser uses the Phase 6 identity service without storing bearer or refresh tokens in
JavaScript-accessible storage. Native CLI, CI, and service-account clients continue to use explicit
bearer credentials.

## Discovery and sign-in

`GET /api/v1/auth/config` is public and returns only browser-safe configuration: whether local and
OIDC login are enabled, the OIDC start path, default organisation slug, API path, feature flags,
branding, upload limit, and live-update settings. It contains no provider secret or token.

Local login posts username, password, organisation, and `browser: true` to
`POST /api/v1/auth/login`. A client may instead send `X-Lab-Auth-Mode: cookie`. On success the body
contains public identity/session timing fields but no reusable access token.

OIDC starts at `GET /api/v1/auth/oidc/login?browser=true&return_to=/safe/path`. The control plane
uses authorization code with S256 PKCE. A successful callback sets the same browser cookies and
redirects only to a validated root-relative SPA path. Provider errors do not place credentials in
the URL. Users must still be pre-provisioned according to [OIDC](OIDC.md).

## Cookie and CSRF model

| Cookie/header | Visibility | Purpose |
| --- | --- | --- |
| `lab_session` | `HttpOnly`; browser only | rotating identity session credential |
| `lab_csrf` | readable by the same-origin frontend | double-submit CSRF value |
| `X-CSRF-Token` | request header | must match `lab_csrf` on unsafe cookie-authenticated requests |

Cookies use `Path=/`, `SameSite=Lax`, and `Secure` when the configured public URL is HTTPS. The
session cookie is `HttpOnly`; the CSRF cookie intentionally is not. Responses carrying session
material use `Cache-Control: no-store`.

`POST`, `PUT`, `PATCH`, and `DELETE` requests authenticated by the cookie must include the matching
CSRF header. `GET`, `HEAD`, and `OPTIONS` stay read-only. An explicit `Authorization: Bearer ...`
header takes precedence over ambient cookies and follows the native API behavior; merely having a
cookie does not add a CSRF requirement to that bearer request.

## Current identity and permission bootstrap

`GET /api/v1/auth/me` returns the principal, organisation, session/credential identifiers,
organisation membership role, effective role names, and high-level permissions. The UI uses these
values to tailor navigation. Named-resource responses may include more specific action maps; the
server still recomputes each decision.

On a protected `401`, the browser performs one centralized, single-flight refresh and retries the
request once. A successful refresh rotates the CSRF value, so mutation retries rebuild the header
from the new cookie. If refresh fails or the retry is still `401`, the UI cancels/removes the
cached `/auth/me` identity immediately, moves to sign-in, and preserves only a safe internal return
path. Logout clears the same cache as soon as server revocation succeeds. A `403` is not treated as
session expiry. A hidden named resource can return `404` by policy.

## Rotation and logout

`POST /api/v1/auth/refresh` requires CSRF for a cookie session and rotates both the session and
CSRF values. `POST /api/v1/auth/logout` revokes the server session and clears both cookies. Closing
a tab does not revoke a session; use logout or the session administration surface.

Administrators disabling a user prevents new authentication. Active-session revocation behavior
remains controlled by the identity service rather than inferred by the dashboard.

## Browser safety rules

- Never put a session, password, service credential, or CSRF token in a URL or client log.
- Never copy a one-time service credential into local/session storage.
- Do not render provider or API errors as HTML.
- Do not configure a service worker or shared cache to store authenticated API responses.
- Keep browser and API same-origin. Distinct-origin credentialed CORS is not enabled.
- Use HTTPS outside loopback and protect the control plane behind the documented private-network
  boundary.

See [web deployment](WEB_DEPLOYMENT.md), [permissions](WEB_PERMISSIONS.md), and the
[security model](SECURITY_MODEL.md).
