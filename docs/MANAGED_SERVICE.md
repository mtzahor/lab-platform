# Managed service boundary

Phase 8 makes a hosted control plane technically possible without creating a different Agent or a
billing system. A managed offering is the same product protocol operated as a service, with
additional operational controls and support.

## Initial topology

```text
Customer browsers / CLI / CI ──HTTPS──┐
                                      ├── Managed control plane
Customer Agent hosts ─────────WSS─────┘          │
                                                 ├── PostgreSQL
                                                 └── private object storage
```

One production control-plane deployment may serve multiple organisations initially. Active-active
control planes, a Kubernetes operator, per-customer deployments, billing, a customer portal, and
usage-based metering are outside Phase 8.

## Responsibility model

| Area | Customer | Managed-service operator |
| --- | --- | --- |
| hardware and physical safety | owns benches, wiring, drivers, and local access | documents supported Agent contract |
| Agent host | hardens OS, stores credential, maps devices, applies approved Agent updates | provides signed Agent release and compatibility window |
| connectivity | permits outbound HTTPS/WSS and diagnoses local network | operates public TLS endpoint, routing, and gateway availability |
| control plane and dashboard | uses organisation-scoped product interfaces | deploys, patches, monitors, and scales service |
| database/artifact storage | supplies retention/residency requirements | operates private durable stores and tested recovery |
| identity and access | manages users, teams, roles, and IdP policy | protects auth service and documents advanced options |
| workflows and firmware | owns content and authorization | protects tenant data and platform execution boundaries |
| incidents | reports local symptoms and preserves relevant Agent evidence | detects, communicates, mitigates, and supplies service evidence |

Contractual support, data-processing, residency, recovery, maintenance-window, and SLA terms sit
outside the open-source software license and must be explicit for a real service.

## Enrollment experience

The hosted path is intentionally the same as self-hosting:

1. create or assign the customer organisation;
2. create its first owner through the supported bootstrap/administration path;
3. issue a short-lived, one-time Agent enrollment token in that organisation;
4. install the ordinary signed Agent on the customer's hardware host;
5. connect it to the supplied HTTPS control-plane URL;
6. save the returned Agent credential in the host secret store; and
7. verify that its benches appear only to authorised organisation principals.

The enrollment token and resulting credential are different secrets. They must never be sent in a
URL, ordinary YAML, ticket, or chat transcript. Re-enrollment is not a routine reconnect or upgrade
step.

## Tenant-isolation gate

Organisation scope is the intended ownership boundary for identities, Agents, benches,
reservations, workflows, operations, CI sessions, artifacts, and audit records. A public managed
service must not launch merely because a single-tenant deployment works.

Before hosting unrelated customers, the operator must close or explicitly mitigate every known
transitional/global path documented in [the security model](SECURITY_MODEL.md), then run
bidirectional tenant-isolation tests over every list, detail, mutation, stream, transfer,
background job, identifier, audit event, and recovery path. Legacy-token compatibility must be
disabled. Internal/system jobs must carry trustworthy organisation scope or use a narrowly audited
system identity.

The Phase 8 reference architecture is managed-service-ready, not by itself a certification that a
public multi-tenant boundary has passed that gate.

## Service operations

A managed deployment should provide:

- immutable signed releases, preview/canary/stable rings, and recorded digests;
- explicit schema preflight/migrations and a documented rollback class;
- automatic database and object-store backups plus recurring isolated restore tests;
- per-organisation and platform resource/rate limits with predictable errors;
- TLS, private database/object storage, credential rotation, and least-privilege workload identity;
- readiness/liveness, metrics, structured redacted logs, audit events, and on-call alerts;
- capacity forecasts for database, streams, queue depth, artifacts, and Agent connections;
- retention and deletion behavior aligned with customer policy; and
- incident, maintenance, export, offboarding, and disaster-recovery runbooks.

Hosted backups and extended retention are paid operational services; the community edition still
contains operator-driven backup/restore and configurable basic retention.

## Design-partner readiness

A controlled design partner should need only:

```text
control-plane URL
temporary owner bootstrap/sign-in path
Agent installation and enrollment instructions
support contact and maintenance expectations
```

Start with SimLab, then one non-critical physical Agent. Prove enrollment, workflow, artifact,
disconnect/reconnect, backup, restore, and upgrade behavior before widening the fleet. Record data
residency, retention, support hours, RPO/RTO, known limitations, and exit/export procedure.

Do not build billing or promise an SLA before the operational and tenant-isolation evidence exists.
See [open-core model](OPEN_CORE_MODEL.md), [production security](PRODUCTION_SECURITY.md), and
[disaster recovery](DISASTER_RECOVERY.md).
