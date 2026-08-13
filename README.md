# Lab Platform

Lab Platform safely shares, reserves, and controls simulated and physical hardware benches. A
central control plane now gives local shells and CI systems one inventory and API across multiple
independent lab Agents while each Agent remains the final authority for hardware locks, execution,
and safety.

The current release is **0.7.0-alpha**. It retains the complete Phase 5 distributed software cut
line—authenticated Agent enrollment, persistent WebSockets, unified inventory, confirmed leases,
duplicate-safe commands, reconciliation, remote workflows/artifacts, distributed CI, and scale
coverage—and adds the Phase 6 identity foundation: organisations, users, service accounts, teams,
fixed RBAC, sessions/credentials, identity and access-policy administration, audit persistence,
configuration, OIDC, resource-scoped enforcement, decision snapshots, and CLI auth/admin commands.
GitHub Actions, GitLab CI, Jenkins, and local runs still use the same vendor-neutral CLI and REST
API.

See [Phase 5](docs/PHASE_5.md) for the authority model, protocol guarantees, complete distributed
demo, recovery behavior, and early-release limitations.

Phase 6 is deliberately not presented as a hardened public-SaaS boundary in this alpha. Its models,
schema-v10 repositories, authentication and authorisation services, first-owner bootstrap, auth
routes, dual Phase 6/legacy bearer handling, identity/access-policy REST and CLI, principal
ownership, remote actor context, security headers, bounded audit retention, safe loopback
auto-login, and refreshable native CLI sessions are present. OIDC authorization-code/PKCE login
maps a configurable username claim to pre-provisioned users. Principal-facing command,
reservation, workflow, CI, Agent lifecycle/enrollment, runtime lifecycle, and artifact services
re-authorise trusted resources before side effects. Artifact access inherits its durable parent,
and named-resource denials can use indistinguishable `404` responses. The main protected identity
and operational actions append attributable success or denial events. Major distributed records
carry organisation scope, and the Agent, bench, workflow, operation, reservation, CI, and artifact
collections enforce their documented per-resource or ownership filters. Remaining work is focused
on explicit legacy/internal paths, lower-priority storage and globally named Agent/bench keys,
public-edge throttling/proxy/browser hardening, and legacy-token migration. Automatic/background
work with no originating Phase 6 decision and legacy work deliberately has no invented initiating
actor or snapshot; a maintenance retry may reuse evidence already persisted for a
principal-requested CI cancellation. The durable control-intent records are not a transactional
replay outbox. See
[Phase 6 status](docs/PHASE_6.md), the
[Phase 6 team-access demo](docs/PHASE_6_TEAM_DEMO.md), [OIDC](docs/OIDC.md), and the
[security model](docs/SECURITY_MODEL.md); do not treat the new tables or settings as a completed
multi-tenant boundary.

## Distributed quick start

Requirements: Python 3.11 or newer and [uv](https://docs.astral.sh/uv/).

```console
uv sync --all-extras
uv run lab-control-plane --config config/control-plane.yaml
```

The checked-in configuration is a loopback-only development deployment. For a non-loopback
deployment, start from
[`config/control-plane.postgresql.yaml`](config/control-plane.postgresql.yaml), inject its real DSN
and TLS files through deployment-secret/configuration management, and run
`lab-control-plane migrate --config <path>` before starting the service.

The existing loopback demo below retains its Phase 5 legacy scoped client token and one-time Agent
enrollment token. For a new Phase 6 identity login, first run `lab-control-plane bootstrap-admin`
as documented in [local authentication](docs/LOCAL_AUTH.md), then use `labctl auth login`:

```console
source .venv/bin/activate
export LAB_PLATFORM_SERVER=http://127.0.0.1:8443
lab-control-plane bootstrap-admin \
  --config config/control-plane.yaml \
  --username local-admin \
  --display-name "Local Admin"
labctl auth login --username local-admin --organisation default
labctl token create \
  --name local-admin --owner local-admin \
  --scope agents:read --scope agents:admin --scope benches:read \
  --scope reservations:write --scope workflows:run --scope operations:read \
  --scope artifacts:read --scope artifacts:write --scope ci:sessions
export LAB_PLATFORM_TOKEN='<printed client token>'

labctl agent enrollment-token create --name home-lab --expires-in 30m
export LAB_AGENT_ENROLLMENT_TOKEN='<printed enrollment token>'
lab-agent connect \
  --config config/agent.yaml \
  --control-plane http://127.0.0.1:8443 \
  --credential-env LAB_AGENT_HOME_CREDENTIAL
```

Enrollment prints the Agent ID, Agent-specific gateway URL, and a credential shown once. It never
writes the secret to disk. Save the non-secret YAML fragment in a per-Agent configuration, choose
unique local database/artifact/data paths, set `control_plane.allow_insecure_loopback: true` for
this demo, export the credential, and start the Agent. Repeat with another name to add more Agents.
The full copy-ready walkthrough is in [the Phase 5 demo](docs/PHASE_5.md#loopback-simlab-demonstration).

Once connected, `labctl` stays pointed at the control plane and contains no Agent URL:

```console
labctl agent list
labctl bench list --online
labctl reservation create home-lab/bench-01 --owner demo-user --duration 30m
```

## Single-Agent CI compatibility demo

The Phase 4 local API remains available. Run its complete isolated SimLab demonstration with:

```console
git clone <repository-url> lab-platform
cd lab-platform
uv sync --all-extras
./scripts/local-ci-demo.sh
```

The script starts an isolated Agent, creates a temporary scoped API token, builds a deterministic
firmware input, runs [`esp32-ci-test`](examples/workflows/esp32-ci-test.yaml), exports JUnit,
downloads artifacts, finalizes the session, and verifies reservation release. It needs neither
internet access nor physical hardware.

For an existing Agent, store its issued API bearer token in an environment variable and run:

```console
export LAB_PLATFORM_SERVER=http://127.0.0.1:8080
export LAB_PLATFORM_TOKEN='the-one-time-token'

labctl ci run \
  --workflow esp32-ci-test \
  --artifact firmware=build/firmware.bin \
  --input expected_version=0.6.0 \
  --require capability=firmware \
  --require capability=serial \
  --require capability=reset \
  --require capability=probe \
  --label board=esp32 \
  --allow-simulated \
  --no-allow-physical \
  --wait-timeout 10m \
  --reservation-duration 30m \
  --junit-output hardware-results.xml \
  --artifacts-directory hardware-artifacts
```

See [Phase 4](docs/PHASE_4.md), [API tokens](docs/API_TOKENS.md), and
[CI sessions](docs/CI_SESSIONS.md) for the local compatibility surface.

## Run a standalone Agent interactively

```console
uv sync --all-extras
source .venv/bin/activate
lab-agent --config-dir config
```

In a second terminal:

```console
labctl health
labctl bench list
labctl reservation create bench-01 --owner demo-user --duration 30m
labctl bench power-cycle bench-01 --owner demo-user
labctl operation watch <operation-id>
labctl reservation release <reservation-id> --owner demo-user
```

The standalone Agent listens on `http://127.0.0.1:8080` by default. Select the client API endpoint
(a control plane in distributed mode or a local Agent in compatibility mode) with `--server`,
`LAB_PLATFORM_SERVER`, or `~/.config/lab-platform/cli.yaml`. Machine requests use
`LAB_PLATFORM_TOKEN` or `--token-env NAME`; token values are never accepted as ordinary CLI
arguments.

## Same workflow on a physical ESP32

Connect an ESP32 DevKit V1 over a USB data cable and configure
[`examples/esp32-local.yaml`](examples/esp32-local.yaml). Build the reference firmware as described
in [ESP32 setup](docs/ESP32_SETUP.md), set the bench label `board: esp32`, register the Phase 4
workflow, and start the real backend:

```console
lab-agent --config examples/esp32-local.yaml
```

Use an explicit physical-only selection. The pytest hardware-gate environment variables do not
authorize ordinary CLI requests; `--bench`, `--no-allow-simulated`, and `--allow-physical` are the
CLI safety boundary:

```console
labctl ci run \
  --bench esp32-devkit-01 \
  --workflow esp32-ci-test \
  --artifact firmware=build/firmware.bin \
  --input expected_version=0.6.0 \
  --no-allow-simulated \
  --allow-physical \
  --junit-output hardware-results.xml \
  --artifacts-directory hardware-artifacts
```

Normal tests use SimLab. The same declarative workflow and public API are used for both backends;
CI providers never call simulator or hardware drivers directly.

## State and recovery

The standalone default stores platform-owned state under `.lab-platform/`:

- `lab.db` contains catalog, reservation, queue, lock, workflow, operation, event, token, artifact,
  result, cleanup, and CI-session metadata.
- `artifacts/` contains SHA-256-verified firmware, serial captures, reports, and CI outputs under
  generated paths.

The production distributed control plane stores coordination metadata in PostgreSQL and keeps
artifact bytes in its configured artifact directory. The loopback demo instead uses
`.lab-control-plane/` for a SQLite database and artifact store. Each distributed Agent needs
separate SQLite-backed local state for its durable command journal, reservation leases, buffered
events, artifact cache, and existing operation data. SimLab remains the source of truth for current
simulated device state. Delete local state only when you intentionally want to clear histories and
identities.

## Security boundary

The enforced operational boundary combines Phase 5 transport/infrastructure controls with the new
Phase 6 identity bridge: TLS/WSS configuration, unique hashed Agent
credentials, hashed legacy and identity-bound client credentials, one-time enrollment, credential
rotation/revocation, identity-admin permission checks, principal-bound reservation ownership,
resource-scoped checks on named operational routes, remote actor context and durable decision
snapshots on identity-backed work and principal-initiated Agent controls, snapshot-safe idempotent
replay, a conservative HTTP security-header baseline, replay-resistant protocol IDs/sequences,
short-lived artifact capabilities, upload limits, safe generated paths, and declarative workflows
without arbitrary shell execution. Plaintext Agent and client credentials belong in protected
environment/native secret stores, not YAML.

This alpha release is a single-control-plane reference deployment, not a hardened public SaaS.
PostgreSQL is the production central store; SQLite remains a developer-demo option. Phase 6 API
route families pass focused two-tenant isolation tests in both directions, the main operational
collections apply per-item RBAC, and principal-facing application services recheck command,
reservation, workflow, CI, drain/enrollment/runtime-lifecycle, and artifact decisions. Artifact
reads, writes, transfers, and platform-managed deletion inherit trusted parent resources; a
`WORKFLOW_STEP` artifact cannot yet be resolved safely for a Phase 6 principal, and remote artifact
deletion is not exposed. Explicit internal/background and legacy compatibility callers,
lower-priority storage, and some deployment-global human-readable Agent/bench identifiers remain
transitional. Legacy-token compatibility has no organisation principal and is excluded from tenant
isolation guarantees. OIDC is implemented for pre-provisioned users, but pending state is
process-local, mapping is not bound durably to issuer/subject, and JIT provisioning, external group
mapping, and a cookie-based browser session are absent. mTLS, HA, a secrets vault, broad public-API
rate limiting, and additional proxy/browser hardening remain incomplete or later deployment work.
Keep the PostgreSQL DSN in deployment-secret storage, require database TLS, and run
`lab-control-plane migrate --config <path>` before starting the service. Outside the loopback demo,
either configure an HTTPS public URL with both direct TLS certificate/key paths, or bind the process
to loopback behind a same-host TLS proxy and explicitly enable
`development.allow_tls_termination_proxy`. Use a private network and **do not expose the loopback
development configuration to the untrusted public internet.**

## Development

```console
uv sync --all-extras
uv run ruff format --check .
uv run ruff check .
uv run mypy
uv run pytest -m "not hardware"
uv run python scripts/export_openapi.py --service agent --check docs/openapi.json
uv run python scripts/export_openapi.py --service control-plane --check \
  docs/control-plane-openapi.json
uv build
```

Documentation starts at [docs/index.md](docs/index.md). Core references are
[ARCHITECTURE.md](ARCHITECTURE.md), [API.md](API.md), [CLI.md](CLI.md), and
[ROADMAP.md](ROADMAP.md). Provider guides cover [GitHub Actions](docs/GITHUB_ACTIONS.md),
[GitLab CI](docs/GITLAB_CI.md), and [Jenkins](docs/JENKINS.md).
