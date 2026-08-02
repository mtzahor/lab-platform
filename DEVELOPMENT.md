# Development

Install and verify the repository with:

```console
uv sync --extra dev
uv run ruff format --check .
uv run ruff check .
uv run mypy
uv run pytest -m "not hardware"
uv build
```

The test suite includes repository/domain tests, shared backend contracts, protocol contracts,
FastAPI/WebSocket integration, persistence restart and reconciliation coverage, a 10-Agent
SimLab scale scenario, and a real Uvicorn/`labctl` workflow on an ephemeral port. Normal tests
require no internet or physical hardware.

## Configuration

`config/agent.yaml` and `config/simlab.yaml` are deeply merged and strictly validated. Relative
database/artifact paths resolve from the project root when using the standard `config` directory,
and from a supplied custom config directory in isolated tests. The independent control plane uses
the strict, single-file `config/control-plane.yaml`; its checked-in HTTP setting is accepted only
because both bind and public URL are loopback and development transport is explicitly enabled.
For a non-loopback deployment, HTTPS requires both direct TLS certificate/key paths. An explicitly
enabled TLS-termination proxy is accepted only while the process itself remains bound to loopback.
The production control-plane store uses a `postgresql://` DSN; the checked-in loopback demo retains
SQLite. Run `lab-control-plane migrate --config <path>` before starting a deployed control plane.
The production-oriented settings in `config/control-plane.postgresql.yaml` deliberately omit a
password: inject the complete DSN from a deployment secret, or use protected libpq credentials
such as `PGPASSFILE`. Never commit a database password.

PostgreSQL integration tests are optional for local development. Provision an empty disposable
database, keep its password in `PGPASSFILE` (or another protected libpq source), and run the live
smoke test explicitly:

```console
export LAB_PLATFORM_TEST_POSTGRESQL_URL='postgresql://lab@127.0.0.1/lab_platform_test?sslmode=require'
uv run pytest tests/unit/test_phase5_postgresql.py -k optional_live_smoke
```

The ordinary non-hardware suite remains self-contained and uses temporary SQLite databases.

Generate or verify both checked-in OpenAPI schemas:

```console
uv run python scripts/export_openapi.py --service agent docs/openapi.json
uv run python scripts/export_openapi.py --service agent --check docs/openapi.json
uv run python scripts/export_openapi.py --service control-plane docs/control-plane-openapi.json
uv run python scripts/export_openapi.py --service control-plane --check \
  docs/control-plane-openapi.json
```

## Adding a backend

Implement every method in `LabBackend`, pass the backend contract tests, translate implementation
errors into stable platform errors, and add only the selection case to `create_lab_backend()`.
Application services, API routes, and CLI commands must not change for a new backend.
