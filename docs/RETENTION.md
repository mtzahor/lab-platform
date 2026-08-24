# Retention policy

Retention bounds storage growth without making deletion invisible. It is not a backup policy:
backups and object-store recovery copies require separate lifecycles.

## Configuration

The artifact policy distinguishes the operational value of each class:

```yaml
retention:
  artifacts:
    enabled: true
    default_days: 30
    failed_workflow_days: 90
    firmware_days: 180
    serial_log_days: 30
    junit_report_days: 90
    workflow_log_days: 30
    diagnostic_bundle_days: 14
    worker_interval_seconds: 3600
    batch_size: 100

audit:
  enabled: true
  retention_days: 365
```

The environment exposes focused overrides for the default, failed-workflow, and firmware periods:

```dotenv
LAB_RETENTION_DEFAULT_DAYS=30
LAB_RETENTION_FAILED_WORKFLOW_DAYS=90
LAB_RETENTION_FIRMWARE_DAYS=180
```

Use YAML for the remaining reviewable policy. Durations must be positive; a class set to `null`
has no time-based expiry unless another applicable rule supplies one. Production configuration
validation must reject invalid ranges rather than silently substituting a default.

Artifact types map to `firmware`, `serial_log`, `junit_report`, `workflow_log`,
`diagnostic_bundle`, or `other`. Unknown types use `default_days`. A failed-workflow artifact keeps
the longer of its class period and `failed_workflow_days`; this avoids deleting failure evidence
earlier than an ordinary artifact of the same class.

## Deletion lifecycle

Schema 12 adds durable lifecycle and retry columns to every platform artifact. Existing rows
migrate to `active`; the worker moves eligible rows through `active` → `pending_deletion` →
`tombstoned`. A due-state index bounds scans, while ordinary artifact reads and mutations expose
only active rows.

The runtime uses `RetentionWorker` and the durable repository path:

1. find expired active rows or abandoned pending claims, excluding artifacts whose owning workflow,
   workflow step, or operation is still active;
2. atomically claim a bounded batch as `pending_deletion`, recording a unique claim token, claim
   time, incremented attempt count, and cleared prior error;
3. delete the physical local/S3 object;
4. in one database transaction, verify the claim token, mark the row `tombstoned`, record deletion
   time, clear claim/error fields, and insert a successful `RETENTION_DELETION` audit row; and
5. on failure, retain the pending row and transactionally record the bounded error, retry time, and
   failed `RETENTION_DELETION` audit evidence so a later worker can reclaim it after the claim lease.

Deleting an already-absent object is treated as an idempotent operation. If a worker stops after
physical deletion but before the tombstone commit, a later run repeats the delete and completes the
metadata transition. A claim-token check prevents an old worker from committing over a newer
attempt. Tombstoned metadata remains durable evidence instead of disappearing with the object.

The active-owner query protects nonterminal CI sessions, pending/running/cancel-requested workflow
runs and steps, and nonterminal, unknown, or reconciling operations from expiry. Do not use an S3
bucket lifecycle rule to race the application policy; if infrastructure lifecycle is required,
make it longer than application retention and test the interaction.

## Audit and privacy

Application audit retention uses `audit.retention_days` and runs in bounded maintenance batches.
Each durable deletion audit row contains organisation and artifact identity, artifact/owner type,
owner ID, size, attempt count, outcome, and a bounded failure reason where applicable. It must never
contain artifact contents, credentials, database URLs, or secret-bearing filenames.

Longer audit or artifact history increases privacy, discovery, and storage obligations. Shorter
history can remove debugging or compliance evidence. Set periods with the teams responsible for
security, legal requirements, lab operations, and backup retention; the defaults are product
defaults, not compliance advice.

## Operations

After changing policy:

1. validate configuration and run `production-check`;
2. estimate eligible object count and bytes before the first shortened-policy run;
3. take and verify a complete backup;
4. lower `batch_size` if object-store or database deletion load is a concern;
5. monitor background failures, claimed/tombstoned counts, deleted bytes, and audit events; and
6. sample old and retained artifacts after the worker runs.

```console
lab-control-plane config validate --config /etc/lab-platform/control-plane.yaml
lab-control-plane production-check --config /etc/lab-platform/control-plane.yaml
lab-control-plane doctor --config /etc/lab-platform/control-plane.yaml
```

Retention is destructive and normally irreversible in the live deployment. Restore an
incorrectly deleted object only from a recovery copy whose database metadata and object set are
consistent. See [backup and restore](BACKUP_RESTORE.md).
