# Phase 9 reliability validation

Phase 9 separates fast automated checks from release evidence. Unit, integration, and SimLab
tests prove deterministic contracts; they do not prove a 24-hour soak, physical hardware, a
production-scale restore, or release-candidate performance. The reference profile is machine
readable in [`tests/reliability/phase9-reference.yaml`](../tests/reliability/phase9-reference.yaml),
and accepted gate state is recorded in
[`release/phase9-readiness.yaml`](../release/phase9-readiness.yaml).

## Reference scale and targets

The initial single-control-plane reference profile is intentionally bounded:

| Dimension | Target |
| --- | ---: |
| connected Agents | 10 |
| benches and independent resources | 1,000 |
| queued CI sessions / concurrent reservation pressure | 500 |
| active workflows | 100 |
| concurrent live streams | 20 |
| retained historical events | 10,000 |

Engineering acceptance targets, not commercial SLAs:

- common API p95 at or below 500 ms on the recorded reference deployment
- ordinary operation progress p95 at or below 2 seconds
- Agent status propagation p95 at or below 30 seconds
- deterministic reservation/resource conflict result
- bench inventory of 1,000 entries remains usable without a UI stall
- no unacceptable memory, lock, queue, reservation, event, or database growth in 24 hours

The evidence record states CPU, memory, disk, database, network, OS, architecture, Python/runtime,
deployment commit/image, and workload generator location. A larger supplemental run does not
replace the reproducible baseline.

## Fast software gate

Run the complete regression plus focused scale/recovery suites:

```console
.venv/bin/pytest
.venv/bin/pytest tests/end_to_end/test_phase5_distributed_scale.py
.venv/bin/pytest tests/recovery/test_phase5_reconciliation_drain.py
.venv/bin/pytest tests/unit/test_phase9_resources.py
.venv/bin/pytest tests/unit/test_phase9_operational_maturity.py
```

These runs can make automated evidence available for review. They cannot set criteria 34–40 to
`passed` without the corresponding reference or interruption run.

## HTTP load/soak probe

The bounded probe records availability and latency without writing credentials to arguments or
output. Run it against a disposable reference deployment:

```console
python scripts/reliability/phase9_http_probe.py \
  --base-url http://127.0.0.1:8443 \
  --path /api/v1/health \
  --requests 10000 \
  --concurrency 20 \
  --maximum-p95-ms 500 \
  --profile reference \
  --output evidence/phase9-http-load.json
```

For an authenticated inventory endpoint, put the token in an environment variable and use
`--token-env LAB_PLATFORM_TOKEN`. URLs with user info, queries, or fragments are rejected so
credentials cannot be copied into the record.

A duration probe is useful during a soak:

```console
python scripts/reliability/phase9_http_probe.py \
  --base-url http://127.0.0.1:8443 \
  --duration-seconds 86400 \
  --requests-per-second 20 \
  --concurrency 20 \
  --profile reference \
  --output evidence/phase9-http-soak.json
```

The probe caps stored samples and explicitly labels its limited scope. A passing HTTP record alone
does not close the load or soak gate; pair it with the stateful workloads and telemetry below.

## Stateful load procedure

1. Start from a clean, production-like PostgreSQL/artifact deployment with production logging and
   metrics enabled.
2. Seed 10 SimLab Agents, 1,000 uniquely addressed benches/resources, and 10,000 historical events.
3. Warm caches, then record a five-minute idle baseline.
4. Generate 500 competing reservation/CI requests with both non-conflicting and deliberately
   conflicting resource sets.
5. Hold 100 workflows active and 20 live streams while inventory, operation, and analytics reads
   continue.
6. Assert one winner per exclusive resource, no partial multi-resource acquisition, monotonic
   fencing tokens, bounded queues, unique terminal events, and complete cleanup.
7. Record request/progress/status percentiles, error-code distribution, CPU, RSS, database
   connections/size, artifact bytes, queue depth, and event count.
8. Run one successful SimLab workflow after load stops.

Failed authorization and intentional resource conflicts are expected workload outcomes, not
availability errors; record them separately. Any 5xx response, duplicate unsafe dispatch, leaked
lock, or missing terminal state fails the run.

## 24-hour soak procedure

The minimum release evidence duration is 24 continuous hours; 72 hours and seven days are
supplemental profiles. Cycle deterministic workloads for reservations, queued workflows, Agent
reconnects, artifacts/cleanup, failures, and live-stream disconnects. Sample at least every minute:

- process/container RSS and CPU
- open files, tasks/threads, sockets, and database connections
- pending/running/terminal operations
- locks and active/expired reservations
- queue depth and oldest wait age
- database and artifact-store size
- event duplicate count and consumer lag
- plugin/resource health transitions

Pass conditions are no unbounded memory growth, no lock or active-reservation leak, no duplicate
terminal events, no stuck operation/unbounded queue, cleanup within its documented window, and a
successful post-soak workflow. Define the acceptable RSS trend before running; do not choose it
after observing results.

## Reconnect and plugin recovery

During active non-destructive SimLab work, repeatedly disconnect/reconnect Agents with delayed and
duplicate protocol messages. Assert inventory converges, status propagation stays within the
target, commands are deduplicated, acknowledgements replay safely, and ownership remains local to
the Agent.

Inject one plugin initialization failure and one runtime failure while another plugin remains
healthy. The failed plugin reports its stage/code, only its devices degrade, shutdown remains
bounded, and a controlled reinitialize/replug recovers without restarting unrelated hardware.
Never inject a destructive retry into physical equipment without its own reviewed procedure.

## Service and storage interruption

Run each scenario in an isolated deployment with preserved logs/metrics/correlation IDs:

| Injection | Required recovery assertion |
| --- | --- |
| Agent restart | lease/fencing reconciliation completes; no command is repeated unsafely. |
| control-plane restart | Agents reconnect and replay idempotently; queued/active state reconciles. |
| PostgreSQL interruption | requests fail closed; no phantom success; state converges after DB recovery. |
| artifact-store interruption | upload records remain retryable/failed explicitly; no corrupt artifact is published. |
| WebSocket disconnect, delay, duplicate | correlation and message IDs deduplicate state/dispatch. |
| hardware disappearance | resource becomes unavailable; other plugins/resources continue. |
| failed artifact upload | workflow result identifies missing/failed artifact and cleanup remains safe. |

After each injection, verify resource locks, reservations, operations, histories, artifact hashes,
audit events, and one new successful workflow. Record the exact interruption interval and whether
an operator action was required.

## Backup, destroy, restore, reconnect

The Phase 8 acceptance harness provides the production-like foundation:

```console
python scripts/deployment_acceptance.py --help
```

For Phase 9 evidence, seed the reference-scale counts, create and verify an off-host backup,
destroy only the isolated acceptance volumes selected by the harness, restore into a clean
deployment, reconnect Agents, and compare entity/artifact counts and hashes. Record:

- measured backup/restore/reconnect duration
- recovery point (RPO) and recovery time (RTO)
- database/artifact bytes and entity counts
- version/schema before and after
- missing or intentionally excluded state
- operator steps and limitations

Do not perform a destructive recovery exercise against user or production volumes. The acceptance
harness validates exact generated volume names and refuses reuse; keep that safety boundary.

## Evidence record and review

Use [`tests/reliability/evidence-template.yaml`](../tests/reliability/evidence-template.yaml) as the
release record. Replace `NOT_RUN` only from preserved output; do not edit expected results into
observed fields. Hash or link raw logs, metrics, traces, JUnit, backup manifests, and artifacts.
Redact credentials and private device identities without removing correlation IDs.

Two reviews are required before a release gate becomes `passed`: the runner confirms the observed
result and a release reviewer confirms environment, duration, assertions, and artifact retention.
Then update only the corresponding criterion in `release/phase9-readiness.yaml`.

Validate record structure without pretending the release is ready:

```console
python scripts/release/check_phase9_readiness.py --allow-incomplete
```

The release command intentionally exits non-zero while any criterion is not `passed`:

```console
python scripts/release/check_phase9_readiness.py
```

Current Phase 9 reliability and physical evidence is not yet recorded, so `1.0.0` remains blocked.
