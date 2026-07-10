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
```

Tests must retain more than 90% combined branch coverage. Add unit tests for business
rules and integration tests for changes that cross package or process boundaries.

## Design rules

- Keep `packages/core` free from I/O and framework imports.
- Inject collaborators through constructors; do not add service globals.
- Prefer immutable Pydantic contracts in `packages/models`.
- Publish lifecycle changes through `EventBus`.
- Make resource lifecycles asynchronous and idempotent.
- Keep Phase 0 local and read-only; check [ROADMAP.md](ROADMAP.md) before expanding scope.

Use semantic versioning. Python package metadata encodes `0.1.0-alpha` as the
PEP 440-compatible version `0.1.0a0`.
