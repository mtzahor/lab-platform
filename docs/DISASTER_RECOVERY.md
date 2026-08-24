# Disaster recovery

Disaster recovery restores a trustworthy service and proves that recovered state can accept new
work. It is different from high availability: Phase 8 supports one control-plane instance and does
not provide active-active failover.

Every deployment must choose and record:

- recovery point objective (RPO): the maximum acceptable durable-data loss;
- recovery time objective (RTO): the maximum acceptable service outage;
- owners and escalation contacts;
- independent locations for database, artifact, configuration, and secret recovery material; and
- the exact application/image, schema, and backup compatibility needed to restore.

Test the runbook with SimLab. A checksum-only backup test is not a recovery exercise.

## Common first actions

1. Preserve evidence: timestamps, request/operation IDs, logs, metrics, Agent status, running image
   digest, schema status, and infrastructure events. Do not copy secrets into the incident record.
2. Stop traffic or keep readiness false when the instance cannot safely serve.
3. Stop new workflows, CI sessions, reservations, uploads, migrations, and retention deletion.
4. Protect surviving database/object/Agent-local state from well-intentioned cleanup.
5. Decide whether this is repair-in-place, replacement from durable stores, or restore from backup.
6. Communicate the affected organisations, data window, and next checkpoint.

## Scenario A — control-plane process crashes

**Impact.** Browser, CLI, CI, enrollment, and gateway requests stop. Agents lose their WebSocket;
benches may appear offline. An operation already accepted by an Agent can continue at its local
safe boundary, so central state is unknown until reconciliation.

**Automatic behavior.** Liveness disappears, readiness removes the instance from traffic, and the
service manager may restart it. Agents retain credentials and durable local journals/buffered
events, retry with backoff, and reconcile boot/sequence/operation state after reconnect. The Agent
remains responsible for hardware locks and safety.

**Recovery.** Inspect the exit and resource pressure before restart loops erase useful context.
Verify database and artifact availability, restart the same immutable image/configuration, wait for
readiness, run `doctor`, then watch every Agent reconcile. Inspect unknown or active operations
before admitting new work on their benches.

**Possible loss.** With intact PostgreSQL and artifact storage, committed central state should
survive. Ephemeral HTTP/SSE connections and unflushed process logs are lost. Agent events can be
lost only if an outage exceeds its bounded local buffer/retention or Agent-local state is also
lost.

## Scenario B — database unavailable

**Impact.** The control plane cannot safely authenticate, schedule, persist, or reconcile central
work. Readiness must fail; liveness can remain healthy because the process itself is alive.

**Automatic behavior.** New state-changing work is rejected or removed from traffic. Existing
Agent-side work may reach a safe local boundary and events remain buffered subject to Agent limits.
The application must not initialize or migrate a database implicitly during this incident.

**Recovery.** Restore network/DNS/TLS/credentials or fail over using the database provider's
documented procedure. Run read-only `db status`/`db check`; if the database was restored, confirm
its schema and transaction-consistent recovery point before starting traffic. Restart/reconcile the
control plane, then validate identities, reservations, workflows, and audit continuity.

**Possible loss.** A connectivity outage with an intact primary loses no committed database data.
A database restore loses commits newer than its recovery point; related artifact bytes may then be
orphaned and must be reconciled conservatively, not deleted during the incident.

## Scenario C — artifact store unavailable

**Impact.** Artifact upload, download, transfer, and workflows requiring firmware/results fail.
Database metadata and non-artifact control functions may still exist, but readiness should fail
when the configured artifact backend is required.

**Automatic behavior.** Storage operations return predictable errors and must not commit successful
metadata for missing bytes. In-progress uploads are cleaned or retried safely. Agent caches may
retain bounded temporary copies but are not the authoritative control-plane backup.

**Recovery.** Check mount capacity/permissions or object endpoint, DNS, TLS, credentials, bucket,
prefix, and provider status. Restore service without changing the backend identity, run `doctor`,
and verify old download, new upload, checksum, and deletion/retention behavior. Reconcile metadata
and objects from a known-consistent inventory before removing orphans.

**Possible loss.** A transient outage loses no durable objects. Store corruption/account loss loses
objects newer than the independent object recovery point. Database rows alone cannot recreate
firmware, logs, reports, or diagnostic bundles.

## Scenario D — server disk is lost

**Impact.** The Compose host, local PostgreSQL volume, local artifacts, Caddy state, configuration,
and mounted secrets may all be gone. Remote Agents and an external database/S3 store can survive
because they are separate systems.

**Automatic behavior.** Nothing on the failed host is assumed recoverable. Agents retry the known
URL and preserve local safety/state within their configured bounds.

**Recovery.** Provision a clean supported host. Verify signed image digests, restore protected
configuration/secrets/TLS or DNS proxy state, provision an empty matching database and artifact
target, and restore the latest verified complete backup. Run schema/configuration diagnostics before
starting. Restore the same public URL when possible so Agents reconnect without re-enrollment; then
reconcile a SimLab canary and the physical fleet.

**Possible loss.** All data after the backup/object-store RPO can be lost. If PostgreSQL or S3 was
external and intact, loss may be limited to host configuration and local proxy state. Never assume
a named Docker volume is an off-host backup.

## Scenario E — bad application upgrade

**Impact.** Startup, migration, authentication, API, storage, or Agent compatibility can fail; a
partially accepted deployment can also corrupt operator confidence even when the process is live.

**Automatic behavior.** Configuration/schema compatibility and readiness should block unsafe
traffic. An incompatible Agent is rejected with a minimum/protocol reason. No automatic downgrade
or arbitrary remote Agent installation occurs.

**Recovery.** Stop the new control plane and retention workers. Preserve post-failure evidence and
the database. Follow the release's rollback class. The current Phase 8 schema policy is **restore
backup required**: restore the verified pre-upgrade database+artifact archive into an empty target,
deploy the previous image digest/configuration, verify readiness and a SimLab workflow, then admit
Agents gradually.

**Possible loss.** Restoring the pre-upgrade backup loses work accepted after that recovery point.
An application-only rollback against an unsupported newer schema can cause additional damage and
is forbidden.

## Scenario F — Agent fleet reconnects after an outage

**Impact.** Many simultaneous reconnects create gateway, database, reconciliation, queue, and log
load. Central operations may be unknown, reservations may be in grace, and benches must not accept
conflicting work.

**Automatic behavior.** Agents reconnect with persistent credentials, boot IDs, sequences,
journals, and buffered events. The control plane applies bounded reconciliation and duplicate-safe
commands; offline/unknown/reconciling are distinct from available. Reservation grace prevents an
immediate unsafe reassignment.

**Recovery.** Restore control-plane readiness first. Rate/canary the fleet if infrastructure allows,
watch connection and reconciliation metrics, and investigate Agents whose boot/sequence or version
is rejected. Let authoritative reconciliation resolve operations and leases. Do not delete
Agent-local databases, rotate every credential, release unknown reservations, or replay commands
manually as a shortcut.

**Possible loss.** Durable central and Agent state should converge. Events older than a full Agent
buffer or artifacts removed from an Agent cache before transfer can be missing after a long outage;
record those gaps explicitly.

## Recovery acceptance

A recovery is not complete until:

- `/health/live` and `/health/ready` behave correctly and `doctor` passes;
- schema, application version, release channel, and image digest match the runbook;
- an owner can authenticate and organisation isolation checks pass;
- representative historical workflows, artifacts, and audit events are readable;
- a SimLab Agent reconnects without re-enrollment and completes a new workflow;
- physical Agents reconcile without conflicting reservations or unexplained unknown work; and
- a new complete backup is created, verified, and copied off-host.

Record actual RPO/RTO, lost-data window, manual decisions, and follow-up controls after every drill
or incident. See [backup and restore](BACKUP_RESTORE.md), [upgrading](UPGRADING.md), and
[production security](PRODUCTION_SECURITY.md).
