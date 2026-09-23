# Post-v1 decision engine implementation note

Architecture reviewed before implementation on 2026-09-22.

* Python 3.11+, Pydantic v2, FastAPI; explicit setuptools packages and uv lock.
* Local `WorkflowRun` / `WorkflowStepResult` live in `models/workflows.py`.
  The web run identifier instead addresses `DistributedOperation`; the completed
  `result.workflow_run` and `result.steps` carry the Agent's recorded evidence.
* `core/results.py` derives assertions/JUnit deterministically. Measurements, when
  present, are step output; there is no independent general measurement table.
* Local statuses are lower-case workflow enums; distributed statuses are upper-case.
* `core/workflows.py` executes sequential steps using `LabBackend`. There is no
  general automatic step retry counter. Transport retries are delivery semantics,
  not permission to repeat a physical operation.
* Agents own physical locks, safety, and backend/plugin execution. Diagnosis has no
  dependency on these execution services and never requests new hardware evidence.
* The control plane composes services in `runtime.py`, uses protected operation
  reads in `operational_access.py`, and exposes `/api/v1/workflow-runs` in `api.py`.
* SQLite repositories also run through the PostgreSQL compatibility adapter;
  schema changes belong in the numbered central migrations. Production migrations
  are explicit. Existing structured logging supports event payloads and redaction.
* Configuration combines Pydantic settings, YAML, environment and CLI overrides.
* React/TypeScript `WorkflowRunPage.tsx` renders deterministic steps/assertions;
  OpenAPI JSON and generated TypeScript contracts are checked in.

Implementation sequence / file plan:

1. `models/decisions.py`; `core/decision_engine/{interface,schema,serializer,policy,service}.py`:
   provider-neutral contracts, bounded evidence, immutable question version and policy.
2. `control_plane/{jev_provider,decision_api}.py`, `config.py`, `runtime.py`, `api.py`:
   lazy optional SDK, one batch, timeout, authorized read/diagnose/feedback routes,
   separate bounded shadow worker. No changes to test execution.
3. `persistence/decisions.py`, `migrations.py`: append-only attempts and feedback,
   tenant-scoped lookup, exact evidence/hash, full validated probabilities and errors.
4. `core/decision_engine/evaluation.py`, CLI integration, 24 synthetic fixtures,
   golden states, offline unit/integration tests, minimal run UI and UI tests.
5. `docs/DECISION_ENGINE.md`, example environment, dependency lock and API contracts.

Only disabled, shadow, and recommendation operation is implemented. Auto-retry is
a future calibrated experiment, not an executable policy in this release.

The initial review encountered OneDrive hydration delays. The source checkout is
now readable; if a local toolchain still reports a placeholder, use a separate
temporary environment and keep that limitation separate from platform behavior.
