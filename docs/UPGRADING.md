# Upgrading Lab Platform

An upgrade is an operational change, not just an image pull. The supported self-hosted sequence is
backup, preflight, pull, explicit migration, restart, and verification. Never combine an
unrecorded database migration with an untested deployment change.

## Compatibility contract

Each release publishes these facts in its release notes:

- the minimum source application release;
- the minimum and target database schema versions;
- the supported Agent and Agent-protocol window;
- its release channel; and
- one rollback class: **fully reversible**, **application rollback only**, or **restore backup
  required**.

The Phase 8 policy supports an upgrade from at least the previous minor application line. The
`0.9.0-beta` source tree targets schema 12, accepts schema 11 as its minimum migration source, and
classifies rollback after migration as **restore backup required**. Release notes are authoritative
if a later patch changes any of those values.

The API remains `v1`, the plugin API remains `1.0`, and the Agent protocol remains `1.0` in this
line. See [version compatibility](VERSION_COMPATIBILITY.md) before changing an Agent fleet.

## Before the change window

1. Read the target release notes, security advisories, compatibility window, and rollback class.
2. Record the running image digests, application version, schema status, configuration revision,
   Agent version report, and storage backend.
3. Verify free database, artifact, backup, and temporary space.
4. Run configuration and live diagnostics against the current deployment.
5. Quiesce new workflows and CI sessions, then allow active work to reach a safe boundary.
6. Create a complete backup, copy it off-host, and perform deep verification.

For a host installation:

```console
lab-control-plane config validate --config /etc/lab-platform/control-plane.yaml
lab-control-plane production-check --config /etc/lab-platform/control-plane.yaml
lab-control-plane doctor --config /etc/lab-platform/control-plane.yaml
lab-control-plane db status --config /etc/lab-platform/control-plane.yaml
lab-control-plane backup create --config /etc/lab-platform/control-plane.yaml \
  --destination /srv/lab-platform/backups
lab-control-plane backup verify \
  /srv/lab-platform/backups/lab-platform-backup-YYYY-MM-DDTHHMMSSZ.tar.zst \
  --config /etc/lab-platform/control-plane.yaml
lab-control-plane upgrade check \
  /srv/lab-platform/backups/lab-platform-backup-YYYY-MM-DDTHHMMSSZ.tar.zst \
  --target-version 0.9.0-beta \
  --config /etc/lab-platform/control-plane.yaml
```

In Compose, prefix each command with
`docker compose run --rm control-plane` and use the mounted configuration path. A production
`upgrade check` is intentionally blocked when no verified backup is supplied.

## Apply the upgrade

Use immutable version tags or image digests. Do not deploy the mutable `preview` or `nightly` tag
as the change record.

```console
docker compose pull
docker compose run --rm control-plane db check \
  --config /etc/lab-platform/control-plane.yaml
docker compose run --rm control-plane db migrate \
  --config /etc/lab-platform/control-plane.yaml
docker compose up -d
```

`db check` is read-only. `db migrate` is the explicit forward-migration action; production startup
must not be used as an implicit migration tool. A future or inconsistent schema is a hard blocker.
Do not use an override for an unsupported source schema unless a release-specific recovery
procedure explicitly requires it.

## Verify before reopening work

```console
curl --fail https://lab.example.com/health/live
curl --fail https://lab.example.com/health/ready
docker compose exec control-plane lab-control-plane doctor \
  --config /etc/lab-platform/control-plane.yaml
labctl version --all
```

Then sign in, list Agents and benches, run a short SimLab workflow, download its artifact, and
review logs and metrics for migration, storage, background-worker, or compatibility errors. Keep
new work paused until readiness is stable and the smoke workflow succeeds.

## Agent order

Do not install packages remotely from the control plane. `upgrade check` and `labctl version --all`
report fleet status; operators update Agents with their normal package or image management.

- Upgrade an Agent first when the target control plane marks its current version **required** or
  **unsupported**.
- Agents marked **recommended** or **available** may remain connected within the declared window,
  but should be scheduled promptly.
- Upgrade a small SimLab/canary group, verify reconnect and one workflow, then roll through the
  remaining fleet.
- Preserve Agent-local state and credentials. Re-enrollment is not a normal upgrade step.

## Rollback

Stop immediately if migration, readiness, login, Agent reconnect, or the smoke workflow fails.
Follow the target release's declared rollback class:

- **Fully reversible:** run only the documented reverse procedure.
- **Application rollback only:** restore the previous image only while the schema remains in its
  documented compatibility range.
- **Restore backup required:** stop the new control plane, provision an empty target matching the
  backup backend, restore the verified pre-upgrade backup, deploy the previous image digest, and
  verify readiness and history.

The current Phase 8 database policy is **restore backup required**. Never point an older binary at
a schema it does not explicitly support. See [backup and restore](BACKUP_RESTORE.md) and
[disaster recovery](DISASTER_RECOVERY.md).

## Upgrade evidence

Retain the change record, release notes, image digests and signatures, pre/post schema reports,
backup verification output, Agent compatibility report, readiness results, and smoke-workflow ID.
That evidence is what makes the next rollback or incident response repeatable.
