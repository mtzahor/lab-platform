# Lab Platform

Lab Platform is a local-first system for safely reserving and controlling remote hardware
benches. Phase 1 provides a versioned REST API, an HTTP-only CLI, persistent reservations and
operation history, and a deterministic SimLab backend. No physical hardware or internet access is
required for the demo.

The current release is **0.2.0-alpha**.

## Quick start

Requirements: Python 3.11 or newer and [uv](https://docs.astral.sh/uv/).

```console
git clone <repository-url> lab-platform
cd lab-platform
uv sync --extra dev
source .venv/bin/activate
lab-agent --config-dir config
```

In a second terminal, run the complete workflow:

```console
labctl health
labctl bench list
labctl bench reserve bench-01 --owner demo-user
labctl bench power-cycle bench-01 --owner demo-user
labctl operation watch <operation-id>
labctl bench flash bench-01 examples/firmware/demo.bin --owner demo-user --version 1.1.0
labctl operation watch <operation-id>
labctl bench show bench-01
labctl event list --bench-id bench-01
labctl bench release bench-01 --owner demo-user
```

Each mutating bench action returns an operation ID. Substitute that ID in the following `watch`
command. Add `--output json` to read commands for machine-readable output.

The Agent listens on `http://127.0.0.1:8080` by default. Select another Agent with `--server`, the
`LAB_PLATFORM_SERVER` environment variable, or `~/.config/lab-platform/cli.yaml`.

## Local state

The default configuration stores platform-owned state under `.lab-platform/`:

- `lab.db` contains reservation, operation, firmware metadata, and event history.
- `artifacts/<sha256>/` contains uploaded firmware.

SimLab remains the source of truth for current simulated power and firmware state. Delete
`.lab-platform/` only when you intentionally want to clear local Phase 1 history.

## Development

```console
uv sync --extra dev
uv run ruff format --check .
uv run ruff check .
uv run mypy
uv run pytest
uv build
```

See [PHASE_1.md](PHASE_1.md), [API.md](API.md), [CLI.md](CLI.md),
[SIMLAB_INTEGRATION.md](SIMLAB_INTEGRATION.md), [ARCHITECTURE.md](ARCHITECTURE.md), and
[DEVELOPMENT.md](DEVELOPMENT.md).
