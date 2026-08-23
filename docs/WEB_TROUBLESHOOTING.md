# Web dashboard troubleshooting

Start with the browser network panel and the control-plane structured log. Record the route,
response status, stable API error code, and `X-Request-ID`; do not record cookies, passwords,
credentials, firmware contents, or full serial logs.

## Dashboard does not load

If startup reports that `web_dist/index.html` is missing, build the frontend before enabling web
serving:

```console
cd apps/web
npm ci
npm run build
```

For an installed release, verify the wheel before installation:

```console
uv run python scripts/verify_web_wheel.py dist/*.whl
```

A blank page with 404ing hashed assets usually means `index.html` and `assets/` came from different
builds or a reverse proxy rewrote `/assets`. Deploy the directory as one unit. Missing file-like
paths must return 404; only extensionless client routes should fall back to `index.html`.

## Sign-in loops or immediately expires

- Confirm browser and API use the same HTTPS origin and that `web.public_url` is correct.
- Inspect `Set-Cookie` attributes without copying cookie values.
- Ensure a proxy does not cache `/api/v1/auth/*` or remove `Cookie`/`Set-Cookie` headers.
- Check server clock and configured session lifetimes.
- For OIDC, confirm the provider redirect URI exactly matches the control-plane callback and the
  user is pre-provisioned.
- A `401` means authentication/session failure; a `403` is an access decision and should not cause
  another login.

## Mutations fail with CSRF errors

Cookie-authenticated unsafe methods require `X-CSRF-Token` to equal the readable `lab_csrf`
cookie. Confirm both are present, the request uses `credentials: include`, and the frontend/API are
same-origin. Refresh rotates both values. Do not disable CSRF or make the session cookie readable.

An explicit bearer client does not need the browser CSRF header. If a browser debugging tool adds
an invalid `Authorization` header, bearer precedence can make an otherwise valid cookie request
fail authentication.

## Live indicator says Polling or Stale

Open `/api/v1/events` in the network panel. It should remain pending with
`text/event-stream`, `Cache-Control: no-store`, and periodic data or heartbeat comments.

- Disable proxy buffering/compression that batches event frames.
- Increase upstream read timeout beyond the heartbeat interval.
- Confirm the route is authenticated and not handled by the SPA fallback.
- Verify the server feature flag and browser support for `EventSource`.

Polling is a safe degraded mode. It should refresh visible resources without changing their
reported state. See [live updates](LIVE_UPDATES.md).

## A resource is absent or an action disappeared

The caller may lack collection or named-resource access. `/api/v1/auth/me` provides high-level
access, while bench/workflow responses can supply resource-specific action maps. Use the effective
permissions administration view or ask an authorized administrator. A policy-hidden object may
return the same `404` as an absent object by design.

If state—not permission—blocks an action, inspect bench online/health/stale status, current
reservation/lease, active operation, Agent drain state, capabilities, and workflow requirements.

## Workflow or operation appears stuck

Do not interpret `UNKNOWN` as running. Inspect Agent last-seen time, command/operation status,
reservation validity, and the reconciliation action when allowed. Refreshing the browser is safe;
the work is persisted by the control plane and Agent, not by the tab.

Use the cursor serial endpoint for recent text and artifact download for finalized output. Very
large logs are intentionally bounded in the UI.

## Build or CI fails

```console
uv run python scripts/export_openapi.py --service control-plane --check \
  docs/control-plane-openapi.json
cd apps/web
npm run api:check
npm run format:check
npm run lint
npm run typecheck
npm run test
npx playwright install chromium
npm run e2e:all
npm run build
npm audit --audit-level=high
```

`npm run e2e` is the fixture-backed browser suite. `npm run e2e:integration` is the real-process
suite and needs free loopback ports `18127` through `18129`; it creates a disposable database and
starts one control plane plus two SimLab Agents. Inspect the Playwright web-server output when a
readiness check or child process fails. It never depends on `.lab-control-plane/` or physical
hardware.

If the OpenAPI schema changed, regenerate the checked JSON first, then run `npm run api:generate`
and review the TypeScript diff. Never edit `src/api/schema.ts` by hand.
