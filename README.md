# Lab Platform

Lab Platform is a local-first system for safely sharing, reserving, and controlling simulated and
physical hardware benches. Phase 4 adds hardware CI: a build pipeline can authenticate, select and
reserve a compatible bench, upload firmware, run a declarative workflow, publish JSON/JUnit
results, download logs, and release the bench reliably.

The current release is **0.5.0-alpha**. GitHub Actions is the first polished integration; GitLab CI
and Jenkins use the same vendor-neutral CLI and API.

## Hardware CI quick start

Requirements: Python 3.11 or newer and [uv](https://docs.astral.sh/uv/).

Run the complete local demonstration with SimLab:

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

For an existing Agent, store the one-time token in an environment variable and run:

```console
export LAB_PLATFORM_SERVER=http://127.0.0.1:8080
export LAB_PLATFORM_TOKEN='the-one-time-token'

labctl ci run \
  --workflow esp32-ci-test \
  --artifact firmware=build/firmware.bin \
  --input expected_version=0.5.0 \
  --require capability=firmware \
  --require capability=serial \
  --require capability=reset \
  --require capability=probe \
  --label board=esp32 \
  --allow-simulated \
  --allow-physical \
  --wait-timeout 10m \
  --reservation-duration 30m \
  --junit-output hardware-results.xml \
  --artifacts-directory hardware-artifacts
```

See [Phase 4](docs/PHASE_4.md), [API tokens](docs/API_TOKENS.md), and
[CI sessions](docs/CI_SESSIONS.md).

## Run the Agent interactively

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

The Agent listens on `http://127.0.0.1:8080` by default. Select another Agent with `--server`,
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

Use a gated, explicit hardware run:

```console
LAB_PLATFORM_ENABLE_HARDWARE_TESTS=1 \
labctl ci run \
  --bench esp32-devkit-01 \
  --workflow esp32-ci-test \
  --artifact firmware=build/firmware.bin \
  --input expected_version=0.5.0 \
  --no-allow-simulated \
  --allow-physical \
  --junit-output hardware-results.xml \
  --artifacts-directory hardware-artifacts
```

Normal tests use SimLab. The same declarative workflow and public API are used for both backends;
CI providers never call simulator or hardware drivers directly.

## Local state

The default configuration stores platform-owned state under `.lab-platform/`:

- `lab.db` contains catalog, reservation, queue, lock, workflow, operation, event, token, artifact,
  result, cleanup, and CI-session metadata.
- `artifacts/` contains SHA-256-verified firmware, serial captures, reports, and CI outputs under
  generated paths.

SimLab remains the source of truth for current simulated device state. Delete `.lab-platform/`
only when you intentionally want to clear local history.

## Security boundary

Phase 4 provides hashed scoped tokens, expiry/revocation, upload limits, safe artifact paths, and
declarative workflows without arbitrary shell execution. It does not provide SSO, organization
identity, advanced RBAC, a secrets vault, or a hardened public control plane. Keep the Agent on a
trusted private network and terminate TLS at a trusted reverse proxy. **Do not expose it directly
to the untrusted public internet.**

## Development

```console
uv sync --all-extras
uv run ruff format --check .
uv run ruff check .
uv run mypy
uv run pytest
uv build
```

Documentation starts at [docs/index.md](docs/index.md). Core references are
[ARCHITECTURE.md](ARCHITECTURE.md), [API.md](API.md), [CLI.md](CLI.md), and
[ROADMAP.md](ROADMAP.md). Provider guides cover [GitHub Actions](docs/GITHUB_ACTIONS.md),
[GitLab CI](docs/GITLAB_CI.md), and [Jenkins](docs/JENKINS.md).
