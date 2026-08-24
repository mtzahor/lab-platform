# Production security checklist

Run this checklist before initial exposure, after infrastructure/authentication changes, and during
every upgrade. The reference production target is one private Linux host behind a narrowly trusted
TLS reverse proxy, with PostgreSQL and artifact storage not exposed to the internet.

## Machine-assisted preflight

```console
lab-control-plane config validate --config /etc/lab-platform/control-plane.yaml
lab-control-plane production-check --config /etc/lab-platform/control-plane.yaml
lab-control-plane db check --config /etc/lab-platform/control-plane.yaml
lab-control-plane doctor --config /etc/lab-platform/control-plane.yaml
```

`config validate` performs strict typed/profile validation. `production-check` requires the
production profile and checks HTTPS/proxy/TLS assumptions, PostgreSQL/pool/TLS posture, secret
strength, development settings, resource limits, API category limits, OIDC secret availability,
artifact path, disk space, connectivity, and schema where applicable. `doctor` reports the live
deployment diagnostics. `--output json` is suitable for a deployment gate; `--offline` skips the
database probe only for an intentionally offline validation.

The production `serve` command runs the same dependency-aware preflight automatically and exits
nonzero before opening the listener if it fails. Running the commands above separately gives the
operator earlier, clearer feedback but cannot be used to bypass the startup checks.

These commands cannot inspect firewall policy, external proxy configuration, bucket public access,
backup recoverability, host patch level, or human account hygiene. A green command is evidence for
part of this checklist, not a security certification.

## Required checklist

### Profile and public edge

- [ ] `profile: production`; development mode, auto-login, test identities, legacy token
  compatibility, debug exceptions, and insecure Agent transport are disabled.
- [ ] The public URL is the single expected HTTPS origin for browser, CLI, CI, and Agents.
- [ ] TLS uses current organisation-approved protocols/ciphers and automated renewal; expiry is
  monitored.
- [ ] Only the exact reverse-proxy CIDR is trusted for forwarded headers. The direct control-plane
  port is private and cannot be reached around the proxy.
- [ ] The proxy preserves WebSocket upgrades, SSE streaming, request IDs, upload limits, and sane
  timeouts. It removes untrusted forwarded headers before adding its own.
- [ ] Firewall/security-group rules publish only intended HTTP/HTTPS ingress and required
  administration paths; PostgreSQL, object storage, metrics, and host management remain private.
- [ ] Browser security headers and same-origin cookie/CSRF behavior are verified at the external
  URL. Arbitrary cross-origin cookie hosting is not enabled.

### Secrets and identities

- [ ] `LAB_SECRET_KEY_FILE` points to a generated high-entropy secret (at least 32 characters with
  diversity); no default/example value is active.
- [ ] Database, OIDC, Agent, API/service-account, TLS, and object-store credentials come from
  mounted secret files or a workload/secret manager, never ordinary YAML, image layers, URLs, or
  committed `.env` files.
- [ ] Secret files/directories and backup keys have restrictive ownership/modes; diagnostics,
  tickets, shell history, Compose output, and logs are checked for leakage.
- [ ] The bootstrap owner is secured, unused default/demo accounts are absent, and each human has an
  individual account. Service accounts are least-privilege, expiring where practical, and scoped
  to known source networks.
- [ ] OIDC, when enabled, uses HTTPS issuer metadata, a protected client secret, PKCE, exact redirect
  URIs, pre-provisioned users, and reviewed claim mapping. Recovery local access is controlled and
  tested.
- [ ] Organisation/team/role assignments and resource-specific policies are reviewed. Public
  multi-tenant hosting has passed the additional isolation gate in
  [managed service](MANAGED_SERVICE.md).

### Agents and physical infrastructure

- [ ] Every Agent uses a unique one-time enrollment and unique rotatable credential over WSS.
- [ ] Enrollment tokens are short-lived and revoked/expire after use; Agent credentials live in the
  host secret store and never in YAML or support output.
- [ ] Agent application/protocol versions are inside the configured compatibility window;
  unsupported Agents cannot enroll/connect or accept work.
- [ ] Agent hosts are patched, time-synchronized, and network-restricted. Only required serial/USB
  devices and groups are mapped; containers are never made `privileged` as a shortcut.
- [ ] Physical safety interlocks, local locks, and explicit simulated/physical selection are tested.
  A control-plane identity decision does not replace hardware safety controls.

### Database and artifact storage

- [ ] Production uses PostgreSQL with a least-privilege application role, connection pooling,
  private networking, TLS (`verify-full` for a remote provider where possible), and no internet
  exposure.
- [ ] `db status` is current. Migrations are explicit, tested from the supported previous release,
  and paired with the documented rollback class.
- [ ] The local artifact directory is a dedicated persistent mount with correct ownership,
  capacity/inode alerts, and no direct web/file-share exposure; or the S3 bucket is private,
  encrypted, versioned, prefix-scoped, and reached with workload credentials.
- [ ] Maximum request, firmware, artifact, and log-artifact sizes are intentionally configured.
  Temporary/spool space can accommodate bounded concurrent uploads.
- [ ] Artifact and audit retention are configured and deletion events are reviewed. Provider
  lifecycle rules cannot race the application retention worker.

### Abuse and exhaustion controls

- [ ] Limits exist for concurrent workflows, active CI sessions, SSE streams, artifact/log size,
  and reservation duration.
- [ ] Category rate limits exist for login, artifact upload, workflow creation, Agent enrollment,
  and expensive searches. External edge limits cover multi-process/fleet-wide abuse; in-process
  limits are not a distributed quota service.
- [ ] Rejections are predictable (`413`/rate-limit/application errors), include safe request IDs,
  and do not expose credentials or cross-organisation resource existence.
- [ ] Database connections, process/file descriptors, proxy concurrency, queue depth, and disk/
  object capacity have alerts before exhaustion.

### Logging, monitoring, and response

- [ ] Production logs are structured JSON at an intentional level, correlated by request/Agent/
  operation/workflow IDs, collected off-host, access-controlled, and retention-limited.
- [ ] Redaction tests and spot checks show no authorization headers, cookies, passwords, secret
  keys, DSNs, enrollment/API tokens, or OIDC codes.
- [ ] `/health/live` is used only for process restart and `/health/ready` for traffic admission.
  Metrics/alerts cover database/storage readiness, Agent connections, workflows, reservations,
  queue depth, artifact use, HTTP latency/errors, and background failures.
- [ ] Security reporting uses the private process in [`SECURITY.md`](../SECURITY.md). Incident
  contacts, credential rotation, evidence preservation, customer communication, and isolation
  procedures are rehearsed.

### Backups, upgrades, and supply chain

- [ ] A complete PostgreSQL+artifact backup is created on schedule, encrypted, copied outside the
  host/failure domain, deeply verified, and restored successfully into an isolated fresh deployment.
- [ ] The application secret, database credentials, TLS material, OIDC settings, and infrastructure
  configuration have a separate protected recovery path; application archives intentionally omit
  them.
- [ ] Only immutable signed image digests/release assets from the intended repository are deployed.
  Checksums, Sigstore identity, SBOM, vulnerability results, and release notes are reviewed.
- [ ] `upgrade check` passes with a verified backup and Agent fleet report. The change record names
  the schema and rollback class; the operator follows [upgrading](UPGRADING.md).
- [ ] The [disaster-recovery](DISASTER_RECOVERY.md) runbook has named owners, tested RPO/RTO, and a
  recent SimLab exercise.

## Explicit Phase 8 limits

Phase 8 does not provide an active-active control plane, Kubernetes operator, arbitrary remote
Agent package installer, mTLS fleet PKI, external secret-vault integration, billing system, or a
security certification. Kubernetes/HA and public multi-tenant service exposure require additional
design and evidence. Keep the reference deployment on a trusted, monitored network and document
every exception to this checklist with an owner and expiry.
