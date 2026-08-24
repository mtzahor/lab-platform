# Lab Platform

Lab Platform safely shares, reserves, and controls simulated and physical hardware benches. A
central control plane now gives local shells and CI systems one inventory and API across multiple
independent lab Agents while each Agent remains the final authority for hardware locks, execution,
and safety.

The current development line is **0.9.0-beta** on the `preview` channel. It retains the distributed
Agent, physical-safety, workflow/CI, identity/RBAC, and React dashboard foundations from Phases 5–7
and adds the Phase 8 product boundary: inspectable production/demo Compose deployments, packaged
`init`/`dev` commands, non-root multi-architecture images, strict environment profiles and mounted
secrets, proxy/TLS validation, explicit database checks/migrations, local and S3-compatible artifact
storage, retention, backup/restore verification, liveness/readiness/diagnostics, release and Agent
compatibility reporting, supply-chain automation, and an Apache-2.0 open-core boundary.

Start with [Phase 8](docs/PHASE_8.md), [self-hosting](docs/SELF_HOSTING.md), and the
[production security checklist](docs/PRODUCTION_SECURITY.md). The distributed authority model and
protocol guarantees remain documented in [Phase 5](docs/PHASE_5.md).

The supported Phase 8 production target is one self-hosted control plane, not active-active HA or
a Kubernetes operator. The architecture can host customer Agents using the same protocol, but a
public multi-tenant managed service still requires the explicit isolation gate and current-gap
review in [the managed-service boundary](docs/MANAGED_SERVICE.md) and
[security model](docs/SECURITY_MODEL.md). Legacy compatibility and transitional/global identifier
paths must not be mistaken for a certified SaaS boundary.

## Self-hosted production quick start

Requirements are a current Docker Engine/Compose v2, an `amd64` or `arm64` Linux host, persistent
storage, and a DNS/TLS plan. Install the matching CLI, then generate an inspectable deployment:

```console
lab-platform init /opt/lab-platform
cd /opt/lab-platform
```

Edit the non-secret values in `.env` (`LAB_VERSION`, `LAB_PUBLIC_HOST`, `LAB_PUBLIC_URL`, and
`LAB_ACME_EMAIL`). The initializer creates mounted database/application secrets with owner-only
permissions and refuses to overwrite an existing deployment.

```console
docker compose run --rm control-plane config validate \
  --config /etc/lab-platform/control-plane.yaml
docker compose run --rm control-plane production-check \
  --config /etc/lab-platform/control-plane.yaml
docker compose run --rm control-plane db migrate \
  --config /etc/lab-platform/control-plane.yaml
docker compose up -d
curl --fail https://lab.example.com/health/ready
```

Continue with [production deployment](docs/PRODUCTION_DEPLOYMENT.md), create the first owner through
`lab-control-plane bootstrap-admin`, enroll a SimLab Agent, run a smoke workflow, and create and
verify the first off-host backup. Do not expose the disposable demo as production.

## Distributed quick start

Requirements: Python 3.11 or newer and [uv](https://docs.astral.sh/uv/).

```console
uv sync --all-extras
uv run lab-control-plane --config config/control-plane.yaml
```

The 0.9.0-beta wheel includes the production dashboard bundle, and the checked-in control-plane
config enables it at `http://127.0.0.1:8443/`. For frontend development or deployment layouts, see
[web deployment](docs/WEB_DEPLOYMENT.md).

The checked-in configuration is a loopback-only development deployment. For a non-loopback
deployment, generate the supported template with `lab-platform init`, keep secrets in its mounted
secret files or an external secret manager, and run `lab-control-plane db migrate --config <path>`
explicitly before starting the service.

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
artifact bytes in configured local or S3-compatible storage. The loopback demo instead uses
`.lab-control-plane/` for a SQLite database and local artifact store. Each distributed Agent needs
separate SQLite-backed local state for its durable command journal, reservation leases, buffered
events, artifact cache, and existing operation data. SimLab remains the source of truth for current
simulated device state. Delete local state only when you intentionally want to clear histories and
identities.

## Security boundary

The operational boundary combines TLS/WSS, narrowly trusted proxy headers, unique hashed Agent and
identity credentials, one-time enrollment, resource-scoped RBAC, durable actor/decision evidence,
duplicate-safe protocol handling, short-lived artifact capabilities, checked streaming uploads,
bounded resources/rate categories, structured redacted logs, explicit migrations, and recoverable
storage. Agents remain the final authority for local locks and physical safety. Plaintext
credentials belong in mounted/native/workload secret stores, never ordinary YAML, URLs, logs, or
workflow definitions.

Production uses PostgreSQL, a strong application secret, HTTPS, disabled development identities,
configured limits/retention, and private local or S3-compatible artifact storage. Run
`config validate`, `production-check`, `db check`, and `doctor`; create and restore-test a complete
backup before exposure. Follow [production security](docs/PRODUCTION_SECURITY.md),
[backup/restore](docs/BACKUP_RESTORE.md), and [disaster recovery](docs/DISASTER_RECOVERY.md).

The beta remains a single-control-plane baseline, not active-active HA or an automatic public SaaS
certification. Legacy-token compatibility lacks an organisation principal; lower-priority/internal
paths and some human-readable Agent/bench identifiers remain transitional; OIDC uses
pre-provisioned users and has no JIT/external-group provisioning; arbitrary distinct-origin browser
cookies and arbitrary remote Agent package installation are unsupported. Keep the reference
deployment on a trusted private network and complete the managed-service tenant-isolation gate
before serving unrelated organisations.

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
cd apps/web && npm ci && npm run check && cd ../..
uv build
uv run python scripts/verify_web_wheel.py dist/*.whl
```

Documentation starts at [docs/index.md](docs/index.md). Core references are
[ARCHITECTURE.md](ARCHITECTURE.md), [API.md](API.md), [CLI.md](CLI.md), and
[ROADMAP.md](ROADMAP.md). Provider guides cover [GitHub Actions](docs/GITHUB_ACTIONS.md),
[GitLab CI](docs/GITLAB_CI.md), and [Jenkins](docs/JENKINS.md).
