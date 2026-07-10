# Lab Platform

Lab Platform is a local-first foundation for coordinating hardware lab benches. Phase 0
provides a typed Python core, a plugin system, a simulated five-bench lab, a read-only
Agent API, and the `labctl` command-line client. It has no cloud, authentication,
database, dashboard, or container-orchestration requirement.

The current release is **0.1.0-alpha**.

## Quick start

Requirements: Python 3.11 or newer and [uv](https://docs.astral.sh/uv/).

```console
git clone <repository-url> lab-platform
cd lab-platform
uv sync
source .venv/bin/activate
lab-agent
```

In a second terminal:

```console
labctl health
labctl benches
labctl plugins
curl http://127.0.0.1:8080/health
```

The activation step puts `lab-agent` and `labctl` on the terminal path. Without
activating, prefix commands with `uv run`. To smoke-test startup without leaving a
server running, use `uv run lab-agent --once`.

## Configuration

Configuration is loaded from `config/agent.yaml` and `config/simlab.yaml`. Both files
are merged and validated strictly; unknown keys and invalid values fail startup.

```console
uv run labctl config validate
uv run lab-agent --config-dir config
```

The Agent listens on `127.0.0.1:8080` by default. `lab-agent --host` and `--port` can
override the listener, while `labctl --url` can target a different local listener.

## Development

```console
uv sync --extra dev
uv run ruff format --check .
uv run ruff check .
uv run mypy
uv run pytest
```

See [ARCHITECTURE.md](ARCHITECTURE.md), [CONTRIBUTING.md](CONTRIBUTING.md),
[PLUGIN_API.md](PLUGIN_API.md), and [ROADMAP.md](ROADMAP.md).

## Phase 0 API

- `GET /health`
- `GET /version`
- `GET /plugins`
- `GET /benches`

All endpoints are read-only and use JSON.
