# Artifact storage

The control plane stores artifact metadata in PostgreSQL and artifact bytes behind the
`ArtifactStorage` interface. The community edition supports local disk and S3-compatible object
storage; Agents keep their separate local transfer cache.

Storage objects are addressed by platform-generated relative keys. Uploads enforce configured size
limits and expected size/SHA-256 values, downloads stream through the control plane, and storage
references never contain credentials.

## Choose one backend

### Local disk

Local storage is the production Compose default:

```yaml
artifacts:
  storage_backend: local
  directory: /var/lib/lab-platform/artifacts
  maximum_upload_size_mb: 500
  transfer_token_ttl_seconds: 300
  finalization_timeout_seconds: 300
```

`LAB_ARTIFACT_STORAGE_BACKEND=local` and `LAB_ARTIFACT_DIR` override those values. Use a dedicated
persistent filesystem, not the container layer. The control-plane UID must be able to create,
read, rename, and delete beneath the root. Do not expose the directory through a web server or file
share; API authorization must remain in front of every artifact.

Local writes use a confined staging directory and an atomic final rename. Storage keys reject
absolute paths, traversal, backslashes, and NUL bytes. Monitor capacity and inodes, and include the
volume in every complete backup.

### S3-compatible storage

```yaml
artifacts:
  storage_backend: s3
  maximum_upload_size_mb: 500
  s3:
    bucket: lab-platform-artifacts
    prefix: production
    region_name: eu-west-1
    # Set only for MinIO or another compatible service.
    endpoint_url: https://objects.example.com
```

Equivalent non-secret overrides are:

```dotenv
LAB_ARTIFACT_STORAGE_BACKEND=s3
LAB_S3_BUCKET=lab-platform-artifacts
LAB_S3_PREFIX=production
LAB_S3_REGION=eu-west-1
LAB_S3_ENDPOINT_URL=https://objects.example.com
```

The bucket is required and the prefix must be a safe, non-empty object-key prefix. Omit
`endpoint_url` for AWS S3. Use HTTPS for remote compatible stores; a private endpoint does not make
plaintext transport safe.

The runtime uses a boto3-compatible client. Ensure the deployed package/image contains that
runtime dependency. Credentials come from the SDK's environment, mounted credential file,
workload identity, or instance/task role chain; do not add access keys to ordinary YAML, `.env`
files committed to source, object keys, or database records.

Grant the control-plane identity only the object read, write, metadata, and delete permissions it
needs within the selected bucket/prefix. The built-in S3 backup also requires prefix-scoped object
listing. Keep public access blocked. Enable provider encryption, versioning, access logs, lifecycle
safeguards, and cross-failure-domain replication according to the deployment's recovery objectives.

## Operational contract

Both backends implement:

- `put`: validate the key, stream content, enforce limits, and persist SHA-256 metadata;
- `get`: return a byte stream without loading an entire object into application memory;
- `stat` and `exists`: report backend-neutral size/checksum availability;
- `delete`: be safe to repeat for retention recovery; and
- reference conversion: map a durable local or `s3://` reference back to a platform key.

S3 uploads may spool a bounded incoming object to temporary local storage before SDK upload. Size
the container's temporary filesystem for the configured maximum artifact and concurrent upload
count.

Readiness should fail when the selected required backend cannot be reached, while liveness remains
process-only. Alert on readiness/storage failures, capacity, request latency, upload failures, and
retention backlog. Run `lab-control-plane doctor` after changing credentials, endpoints, mounts,
or permissions.

## Backup and recovery

The built-in complete archive supports both selected backends. Local backup checks every file; S3
backup paginates the configured non-empty prefix, streams every object, and verifies size/object
identity and SHA-256 metadata where present. Restore refuses a non-empty target unless overwrite is
explicit, and S3 overwrite is confined to the exact prefix. Creation still needs local scratch
space for the materialized artifact set and archive. See [backup and restore](BACKUP_RESTORE.md).

Never assume object-store durability is a backup. Accidental deletion, retention-policy error,
credential compromise, and provider/account loss require an independent recovery copy.

## Changing backends

Changing `storage_backend` does not copy objects or rewrite existing durable references. A direct
configuration flip can make historical artifacts unavailable. Treat migration as a planned data
migration:

1. quiesce uploads and workflows;
2. take and verify a complete source backup;
3. copy every object with its key and SHA-256 metadata;
4. update references through a release-supported migration tool;
5. switch configuration and verify representative old and new downloads; and
6. retain the source until the rollback window closes.

There is no general online local-to-S3 migration command in the Phase 8 CLI. Do not improvise a
database rewrite in production.
