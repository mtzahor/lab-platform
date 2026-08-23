# Web deployment

The dashboard has one production build and two supported process layouts. Integrated serving is
the default and easiest self-hosted layout. A separate static server is supported behind the same
browser origin. Both use the same generated API client and cookie authentication.

## Build the release bundle

Requirements are Python 3.11 or newer, `uv`, and Node.js 22 or a compatible current LTS release.

```console
uv sync --extra dev
uv run python scripts/export_openapi.py --service control-plane \
  docs/control-plane-openapi.json
cd apps/web
npm ci
npm run api:generate
npm run check
cd ../..
uv build
uv run python scripts/verify_web_wheel.py dist/*.whl
```

`npm run build` writes directly to
`apps/control_plane/src/lab_platform/control_plane/web_dist`. The wheel packages `index.html` and
the fingerprinted files under `web_dist/assets`. Application startup fails immediately when web
serving is enabled but that entry point is absent. This prevents a deployment that appears healthy
while every browser route returns a 404.

CI also rebuilds the checked bundle and fails when it differs, the OpenAPI-generated TypeScript is
stale, or a referenced asset is absent from the wheel.

## Integrated deployment

Enable the packaged router in the control-plane configuration:

```yaml
web:
  enabled: true
  public_url: https://lab.example.internal
  api_base_url: /api/v1
  live_updates:
    sse_enabled: true
    polling_fallback_seconds: 5
  uploads:
    maximum_firmware_size_mb: 100
  features:
    identity_admin: true
    audit_viewer: true
    ci_sessions: true
  branding:
    product_name: Lab Platform
```

Build/install the wheel, migrate the central database, then start the service with the same TLS and
PostgreSQL requirements as the distributed control plane:

```console
lab-control-plane migrate --config /etc/lab-platform/control-plane.yaml
lab-control-plane --config /etc/lab-platform/control-plane.yaml
```

The control plane serves:

- `/` and client-side routes from the single-page bundle;
- `/assets/*` with immutable caching when the name is fingerprinted;
- `index.html` with `no-cache` so a rollout can select new assets;
- `/api/v1/*`, `/docs`, `/redoc`, `/openapi.json`, and WebSocket paths as server routes, never SPA
  fallbacks;
- no `.map` or dot-prefixed paths.

Keep TLS termination and proxy behavior aligned with `control_plane.public_url`. If TLS terminates
in a proxy, use the existing loopback-only termination mode and do not expose the backend bind.
Ensure proxies disable buffering for `/api/v1/events` and allow long-lived responses.

## Separate static-server deployment

Use this layout when a dedicated web server or CDN process should own static files, but keep one
browser origin:

```text
Browser https://lab.example.internal
  /, /assets/*  -> static server
  /api/v1/*     -> reverse proxy -> control plane
```

Build with the root-relative API path (the default):

```console
cd apps/web
VITE_API_BASE_URL= npm run build
```

Copy the contents of `web_dist` to the static document root. Configure unknown, extensionless
paths to return `index.html`, while missing `/assets/*` paths return 404. Apply the same cache and
security headers documented above, and do not publish `*.map` files. Route `/api/v1/events`
without response buffering.

The frontend also accepts an absolute `VITE_API_BASE_URL`, but arbitrary distinct-origin cookie
deployment is intentionally unsupported in this alpha. The control plane does not publish a
permissive CORS policy, and its browser cookies use a same-site design. Do not weaken cookies or
use wildcard credentialed CORS. A future distinct-origin design needs an explicit origin allowlist,
trusted-proxy rules, secure `SameSite` policy, and corresponding tests.

## Rollback

Static assets are content-fingerprinted, so an older wheel or static directory can be restored as a
unit. Do not mix `index.html` from one build with assets from another. Database schema migration is
independent of web enablement; disabling `web.enabled` removes only the browser route and does not
disable REST, CLI, CI, or Agent traffic.

## Production checklist

- Use PostgreSQL and run migrations before startup.
- Use HTTPS/WSS, protected DSN/TLS files, and a private network.
- Keep `web.public_url`, OIDC redirect URI, proxy host, and the browser origin consistent.
- Confirm `Secure`, `HttpOnly`, `SameSite=Lax`, CSRF, CSP, clickjacking, and cache headers.
- Set server artifact and request limits at least as strict as the UI upload limit.
- Disable external telemetry unless an operator has explicitly designed and approved it.
- Exercise local login or OIDC, SSE reconnection, polling fallback, a workflow, artifact download,
  and logout after every deployment change.

See [web authentication](WEB_AUTHENTICATION.md) and [troubleshooting](WEB_TROUBLESHOOTING.md).
