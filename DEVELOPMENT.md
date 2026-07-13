# Development

Install and verify the repository with:

```console
uv sync --extra dev
uv run ruff format --check .
uv run ruff check .
uv run mypy
uv run pytest
uv build
```

The test suite includes repository and domain tests, a shared-backend-contract-style SimLab test,
FastAPI integration tests, persistence restart coverage, and a real Uvicorn/`labctl` workflow on an
ephemeral port. Tests require no internet or physical hardware.

## Configuration

`config/agent.yaml` and `config/simlab.yaml` are deeply merged and strictly validated. Relative
database/artifact paths resolve from the project root when using the standard `config` directory,
and from a supplied custom config directory in isolated tests.

Generate or verify the checked-in OpenAPI schema:

```console
uv run python scripts/export_openapi.py docs/openapi.json
uv run python scripts/export_openapi.py --check docs/openapi.json
```

## Adding a backend

Implement every method in `LabBackend`, pass the backend contract tests, translate implementation
errors into stable platform errors, and add only the selection case to `create_lab_backend()`.
Application services, API routes, and CLI commands must not change for a new backend.
