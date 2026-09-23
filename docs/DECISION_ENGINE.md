# Experimental post-v1 Decision Engine

Jev provides a likely cause, symbolic recovery recommendation, severity, Choice
confidence, complete probability distributions, and a Noul retry-safety probability
for a completed workflow. Deterministic assertions remain the source of pass/fail.

**Jev recommendations do not override deterministic test limits or hardware safety rules.**

This optional subsystem does not control hardware, execute commands, change limits,
retry tests, power-cycle a DUT, or provide a chatbot. It has no backend/Agent execution
port. Even an accepted operator recommendation is only an audit record.

## Architecture and evidence

Execution → measurements/assertions → stored distributed workflow result → bounded
canonical state → `DecisionEngine` → Jev adapter (one batch) → deterministic policy
→ append-only decision record → optional engineer display.

The control-plane run ID is a distributed operation ID. The serializer consumes its
recorded `workflow_run` and `steps`; it also accepts local `WorkflowRun` objects.
No fresh probe, instrument read, or remote command occurs during diagnosis.

`lab-diagnostic-v1` includes the question criteria, action list, severity rubric and
canonical state format. Questions have an additional content hash. Review/bump the
schema version when those semantics change; update golden fixtures deliberately.

Evidence is sorted, formatted JSON, capped at 32 KiB by default, with at most 64
steps. Larger runs become unavailable. It includes statuses/actions, stable error
codes, limited error-signal keywords, recorded numeric/boolean observations,
instrument health statuses, and structured measurements (`name`, `value`,
`lower_limit`, `upper_limit`, `expected`, `unit`, `passed`). Unknown fields are omitted.
Measurement/instrument collections are capped at 20 entries; truncation is explicit
and forces human review. No raw logs, firmware
paths, addresses, command payloads, artifact contents, owner identities or arbitrary
metadata are transmitted. Secret-like labels and personal contact/network data are
omitted. Historical free-form assertion patterns are not transmitted.

The current platform has no general measurement table or retry history counter.
Missing state remains unknown; a fresh hardware snapshot is never inferred. Add new
observations explicitly to the serializer and golden cases, with schema review.

## Installation and configuration

Install the optional extra with `uv sync --extra jev` or
`python -m pip install 'lab-platform[jev]'`. The official Python SDK is pinned at
0.7.1 and loaded only when an enabled diagnosis calls it. No SDK/key is needed when
disabled. The adapter pins the official `https://api.typesafe.ai` destination,
batches all questions with `system_one`, disables SDK retries, and enforces an
async deadline. SDK objects never leave the adapter.

Set environment variables on the **control-plane process** (see root `.env.example`):

| Variable | Default | Meaning |
| --- | --- | --- |
| `JEV_ENABLED` | `false` | Master opt-in; restart after changing configuration |
| `JEV_API_KEY` | empty | Secret supplied by deployment environment; never commit |
| `JEV_MODEL` | `jev-latest` | Pin a concrete provider model for reproducible evaluation |
| `JEV_TIMEOUT_MS` | `5000` | Whole diagnosis deadline; range 100–30000 ms |
| `JEV_DIAGNOSIS_MIN_CONFIDENCE` | `0.90` | Minimum diagnosis Choice confidence |
| `JEV_ACTION_MIN_CONFIDENCE` | `0.95` | Minimum action Choice confidence |
| `JEV_RETRY_SAFE_MIN_PROBABILITY` | `0.99` | Review retries below this Noul probability |
| `JEV_MODE` | `recommend` | `shadow` or `recommend`; no automatic mode |
| `JEV_MAX_STATE_BYTES` | `32768` | Canonical evidence limit; 1024–65536 bytes |

Non-secret defaults can also be configured under YAML `decision_engine`; JEV
environment values override them at runtime composition. Environment configuration
errors disable diagnosis and log a static configuration warning. Missing/invalid
keys fail the diagnostic attempt; ordinary startup, test execution, cleanup and
Agent maintenance continue. Existing YAML validation rules still apply.

The repository does not automatically load `.env`; inject it using your process
manager or deployment tooling. Do not enable SDK debug/body logging in a shared lab.

All confidence thresholds are **provisional**, not validated performance claims.
Calibrate on representative recorded runs and engineer outcomes. Confidence is not
proof that a classification or action is correct.

## Policy and actions

Allowed diagnoses: `dut_failure`, `communication_failure`, `configuration_failure`,
`instrument_failure`, `infrastructure_failure`, `expected_test_failure`, `unknown`.

Allowed actions: `retry_step`, `power_cycle_dut`, `reset_interface`, `restart_test`,
`mark_failed`, `request_human_review`, `no_action`.

Unknown actions are rejected. Unknown causes, incomplete evidence, low Choice
confidence, high/critical severity, disruptive recovery, or insufficient retry-safety
probability require human review. Other decisions are recommendations only.
`auto_retry` is a reserved domain result, never emitted by this release.

Severity uses Score's most probable ordinal level; ties select the higher level.
The fractional Score and rubric are retained, not treated as a physical quantity.
Noul has a probability, not a separate Choice confidence. Distributions, selected
labels, finite ranges, sums and rubric consistency are validated before policy.

## API, UI and audit

The following routes follow existing authentication, CSRF, tenant and operation
access boundaries:

* `POST /api/v1/workflow-runs/{operation_id}/diagnose`: completed runs only;
  loads evidence, makes one batch, applies policy, persists, returns the recommendation.
* `GET /api/v1/workflow-runs/{operation_id}/diagnoses`: latest 20 attempts;
  includes the exact evidence and its SHA-256 for engineer inspection.
* `POST /api/v1/workflow-runs/{operation_id}/diagnoses/{decision_id}/feedback`:
  append an operator outcome (`accepted`, `rejected`, `different_action_taken`),
  optional `action_taken`, `actual_root_cause` and bounded `notes`.

Reads require operation read access. Diagnosis and feedback additionally require
workflow-run permission (legacy tokens require both relevant scopes). Feedback is
scoped to the same tenant, run and recommendation. It never executes the action.
Shadow records remain hidden, including after switching to recommendation mode.

The existing workflow page shows a distinct Decision Engine section only in enabled
recommendation mode. “Why am I seeing this?” displays recorded evidence, not
AI-generated reasoning. The section records operator outcomes and notes.

The central database migration installs idempotent diagnostic decision, feedback,
and shadow-claim tables without changing the platform's existing schema cutline,
using the existing SQLite/PostgreSQL adapter. Run the existing control-plane database
migration command before production rollout. These tables retain evidence after
normal run retention; they do not have a run foreign key because the serializer also
supports local run IDs. Restrict database access as with operational/audit records.
No automatic diagnostic-data retention or deletion is introduced in this experiment.

Every attempt stores timestamp, tenant/run IDs, schema/questions hashes, exact state
and state hash when serialization succeeds, requested and returned models, provider,
SDK version, full distributions, confidences, Noul probability, Score/rubric,
policy result/reason/thresholds, latency, mode and sanitized error code. Free-form
provider response bodies and exception messages are not stored. Structured logs
record attempt IDs/status/errors without evidence or credentials. Audit persistence
failure suppresses the recommendation and logs `audit_unavailable`.

## Failures and rollout

Timeouts, authentication/rate-limit errors, network/service failures, missing SDK,
malformed/missing answers, unexpected labels, invalid probabilities, serialization
failures and oversized evidence return an unavailable diagnosis with no action.
Admission allows one in-flight request per control-plane process; concurrent requests
are audited as busy. There is no network wait in test execution or lease maintenance.

Stage A: explicitly enable `JEV_MODE=shadow`. A separate task selects at most five
completed failed workflows every 30 seconds. A durable per-run/schema claim prevents
replay on restart and bounds each run to one shadow attempt. Failed attempts are not
automatically retried. A crash after claiming may leave a claimed run without a
completed record; inspect claims during evaluation. The worker shuts down before the
database closes, and failures do not stop the main control-plane monitor.

Stage B: set `JEV_MODE=recommend`. Engineers request diagnosis and record outcomes;
no automatic diagnosis or recovery is performed in this mode.

Stage C is deliberately unimplemented. Any future software-only retry experiment
requires independent review, calibrated confidence, a retry-safe threshold, a
maximum retry count, deterministic safety checks and a separate feature flag.
General autonomous hardware recovery remains out of scope.

Disable at any time with `JEV_ENABLED=false` and restart. The UI is hidden, no worker
starts, and no provider import/request occurs. Historical audit records remain stored.

## Evaluation

The 24 synthetic cases cover success, boot/network/serial failures, disconnected or
malfunctioning instruments, configuration problems, measurement limits, flakiness,
recoverable timeouts, ambiguous failures, disruptive recovery and low confidence.
Expected classifications, acceptable actions, severity ranges and required human
review are in `tests/fixtures/decision_engine/cases.json`. Golden states and question
schema catch unintentional changes.

Offline integration replay (no credential or network required):

```sh
lab-platform evaluate-decision-engine \
  --fixtures tests/fixtures/decision_engine/cases.json \
  --responses tests/fixtures/decision_engine/synthetic_responses.json
```

For a deliberate live evaluation after data review, configure Jev and replace
`--responses ...` with `--live`. `--output report.json` saves a report. The command
reports classification/action/severity/review agreement, per-question confidences,
action-confidence histogram, unavailable cases and regressions; any regression
returns a nonzero exit status. Synthetic replay responses are test doubles, not Jev
predictions and not evidence of model accuracy. Preserve model/schema versions when
comparing real evaluation reports and annotate actual engineer outcomes.

Official references: [Python SDK](https://docs.typesafe.ai/sdk/python),
[usage and retries](https://docs.typesafe.ai/sdk/python/usage),
[typed response semantics](https://docs.typesafe.ai/sdk/python/api/types/responses).
