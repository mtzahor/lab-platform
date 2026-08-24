# Backup and restore

A Lab Platform backup is complete only when persistent database state and the artifact bytes
referenced by that state can be recovered together. Keep backups encrypted, access-controlled,
off the application host, and subject to a tested retention policy.

## What the built-in archive contains

`lab-control-plane backup create` writes a versioned `.tar.zst` archive containing:

- a PostgreSQL custom-format dump, or a consistent SQLite copy for development;
- organisation, identity, role, Agent, bench, reservation, queue, workflow, CI, artifact metadata,
  and audit records stored in that database;
- local artifact files or every object beneath the configured S3 prefix unless
  `--exclude-artifacts` is explicitly selected; and
- non-secret configuration references such as profile, public URL, and storage-backend name.

The archive does not contain application secrets, Agent-local state, caches, temporary files,
external identity-provider configuration, TLS private keys, or infrastructure credentials. Back
those up through the secret manager and infrastructure system that owns them.

The manifest records format version, application version, schema, creation time, database backend,
payload sizes, and SHA-256 digests. Verification rejects unsafe archive paths, duplicate or
undeclared entries, size/checksum mismatches, corrupt database dumps, a newer schema, and an
incompatible application version.

The command selects the artifact adapter from `artifacts.storage_backend`. S3 backup requires a
safe non-empty prefix and a boto3-compatible client/credential chain. It paginates that exact
prefix, streams every object into the archive, and checks observed size, ETag stability where
available, and stored SHA-256 metadata. Objects outside the prefix are never included or deleted.

## Consistent backup procedure

PostgreSQL creates a transactional database dump, but database metadata and local/S3 artifact bytes
are two systems. For a recoverable application-level point:

1. stop admitting workflows, uploads, and CI sessions;
2. allow in-flight artifact transfers and workflows to finish;
3. record `db status` and confirm artifact storage health;
4. create the archive;
5. perform deep verification immediately; and
6. copy the verified archive to independent, encrypted storage.

If the deployment cannot guarantee quiescence, stop the control-plane service while leaving
PostgreSQL and artifact storage available to the backup command. The local adapter detects a file
that changes while it is copied; the S3 adapter rechecks object size/identity after streaming and
fails instead of silently accepting a detected change.

Creation materializes artifact content and an uncompressed tar in local temporary space before
writing the final archive. For S3, this means scratch capacity must cover the selected prefix plus
archive overhead; the object store does not make the backup command diskless.

`pg_dump` and `pg_restore` must be available in the control-plane execution environment for a
PostgreSQL deployment. Use a database account permitted to read all application objects and to
restore into the prepared target.

## Create

```console
lab-control-plane backup create \
  --config /etc/lab-platform/control-plane.yaml \
  --destination /srv/lab-platform/backups
```

A directory destination produces a timestamped name such as
`lab-platform-backup-2026-08-24T120000Z.tar.zst`. An explicit destination may end in `.tar.zst` or
uncompressed `.tar`. The command refuses to replace an existing archive.

Use `--exclude-artifacts` only for a deliberate database-only export. It is not sufficient for
disaster recovery when artifact metadata references bytes outside the archive. Restoring such an
archive performs no artifact cleanup or upload even with `--overwrite`; the independently managed
object set must already match that database recovery point.

## Verify

```console
lab-control-plane backup verify \
  /srv/lab-platform/backups/lab-platform-backup-2026-08-24T120000Z.tar.zst \
  --config /etc/lab-platform/control-plane.yaml
```

Verification is deep by default from the CLI: every declared payload is hashed and the database
dump is inspected. `--output json` provides machine-readable evidence. Run verification after
creation, after transfer to off-host storage, and periodically for long-lived copies.

Checksum verification is necessary but not a restore test. On a schedule, restore into an isolated
fresh deployment and verify identities, Agents, benches, reservations, workflows, artifacts, audit
history, and a new SimLab workflow.

## Restore into a fresh deployment

1. Provision a control-plane version compatible with the manifest. The current reader accepts the
   same major version, no future application version, and at most the previous minor line.
2. Provision an empty database of the same backend recorded in the archive.
3. Provision an empty selected artifact target: a local directory with correct ownership/free
   space, or a private S3 bucket and safe non-empty prefix.
4. Restore secrets and non-secret configuration independently, without starting the service.
5. Verify the archive, then restore it.

```console
lab-control-plane backup restore \
  /srv/lab-platform/backups/lab-platform-backup-2026-08-24T120000Z.tar.zst \
  --config /etc/lab-platform/control-plane.yaml
```

The interactive command requires the exact text `RESTORE`. For reviewed automation, `--yes`
supplies that exact confirmation; it does not permit replacement of non-empty targets. Add
`--overwrite` only when the runbook explicitly authorizes replacement and the pre-restore state is
preserved separately.

The database backend must match the manifest: PostgreSQL restores to PostgreSQL and SQLite to
SQLite. The selected artifact target must be empty unless `--overwrite` is explicit. For S3,
restore validates every archive source before mutation; overwrite then deletes only keys under the
configured prefix and verifies each uploaded object's size and SHA-256 metadata. A failed upload
removes partial new uploads, but cannot recreate overwritten old objects without bucket versioning
or another recovery copy, because replacement is not transactional across the whole prefix. After
restore, run:

```console
lab-control-plane db status --config /etc/lab-platform/control-plane.yaml
lab-control-plane db check --config /etc/lab-platform/control-plane.yaml
lab-control-plane doctor --config /etc/lab-platform/control-plane.yaml
```

Start the control plane, wait for `/health/ready`, sign in, inspect historical records, reconnect a
SimLab Agent, and run a new workflow. Do not reconnect the physical fleet until those checks pass.

## Storage, access, and retention

- Keep at least one copy outside the server and its storage failure domain.
- Encrypt in transit and at rest; restrict restore access more tightly than ordinary read access.
- Never put database URLs, secret keys, OIDC secrets, or live tokens in archive names or logs.
- Record application/schema versions, archive digest, storage location, creation/verification time,
  and the operator or automation identity.
- Define recovery-point and recovery-time objectives, then schedule backups often enough to meet
  them.
- Delete old backups only after newer copies have passed verification and a restore exercise.

Provider snapshots, S3 versioning, and replication are useful additional layers, but a disk or
bucket snapshot taken without database/artifact coordination does not replace the application
backup contract.
