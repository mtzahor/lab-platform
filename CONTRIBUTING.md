# Contributing

## Set up

Install Python 3.11+ and uv, then run:

```console
uv sync --extra dev
uv run labctl config validate
uv run lab-agent --once
```

## Quality gate

Before opening a pull request, run the same checks as CI:

```console
uv run ruff format --check .
uv run ruff check .
uv run mypy
uv run pytest
uv run python scripts/export_openapi.py --check docs/openapi.json
uv build
```

Tests must retain more than 90% combined branch coverage. Add unit tests for business
rules and integration tests for changes that cross package or process boundaries.

## Design rules

- Keep `packages/core` free from I/O and framework imports.
- Inject collaborators through constructors; do not add service globals.
- Prefer immutable Pydantic contracts in `packages/models`.
- Publish lifecycle changes through `EventBus`.
- Make resource lifecycles asynchronous and idempotent.
- Keep transport rules out of services and SimLab-specific rules inside the adapter.
- Preserve the per-bench reservation and operation atomicity guarantees.

Use semantic versioning. Python package metadata encodes `0.2.0-alpha` as the
PEP 440-compatible version `0.2.0a0`.
