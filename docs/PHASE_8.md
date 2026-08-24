# Phase 8 — open-core product and deployment

Phase 8 turns the Phase 7 application into a product an external team can install, operate,
upgrade, back up, and recover. It deliberately adds deployment and lifecycle reliability rather
than new hardware families or a billing system.

The target product version is a `0.9.0` beta/preview line. It is not `1.0`, and Kubernetes, active-
active control planes, a billing engine, and arbitrary remote Agent package installation remain
out of scope.

## Product boundary

The Apache-2.0 community edition includes the Agent, control plane, dashboard, CLI, SimLab,
reservations, queues, workflows, CI integrations, distributed operation, local authentication,
teams/roles, the basic audit log, API, and plugin SDK. These capabilities build and operate without
a commercial package.

Commercial value is operational convenience: hosted control planes, managed databases/backups/
upgrades, extended retention, advanced organisation/OIDC policies, multi-site administration,
support, and SLAs. `CommunityFeatureProvider` is the sole code seam for future separately
distributed extensions; community code must not import commercial modules. See
[the open-core model](OPEN_CORE_MODEL.md) and [licensing](LICENSING.md).

## Supported deployment modes

| Mode | Intended use | Entry point | Support boundary |
| --- | --- | --- | --- |
| Development | contributors and local evaluation | `lab-platform dev up` or source checkout | loopback, development identities, SQLite allowed |
| Demo | disposable product walkthrough | `docker compose -f deploy/demo/compose.yaml up --build` | local host only; explicitly not production |
| Self-hosted production | one VM/server/NAS and remote Agents | `lab-platform init` then production Compose | PostgreSQL, TLS proxy, persistent artifacts/backups |
| Managed control plane | design partners operate only Agents | same enrollment and Agent binary | hosted operations own control plane/DB/storage/backups |

## Release and supply-chain contract

Official release automation builds the Python wheel/source archive, static dashboard archive, and
multi-architecture `lab-platform-control-plane` and `lab-platform-agent` images. A release carries
OCI provenance, SPDX SBOMs, `SHA256SUMS`, a keyless Sigstore bundle, image signatures, and GitHub
attestations. Preview releases never update `stable`; nightly builds never become GitHub/PyPI
releases. See [release channels](RELEASE_CHANNELS.md).

The security workflow audits the Python and npm locks, checks changed dependencies, scans source
and containers for high/critical vulnerabilities, secrets, and deployment misconfiguration, and
generates security-gate SBOMs. Dependabot covers Python, npm, Docker, and GitHub Actions.

## Operational cut line

A Phase 8 release is accepted only when a hardware-free deployment test proves this sequence:

```text
initialize → deploy → bootstrap owner → enroll SimLab Agent → run workflow
    → create/verify backup → upgrade previous minor → restore fresh deployment
    → verify history → reconnect Agent → run another workflow
```

The release gate must also prove:

- both images build for `linux/amd64` and `linux/arm64`, carry OCI metadata, run as UID/GID 10001,
  and answer their health checks;
- the production profile rejects insecure transport, weak/missing secrets, unsafe proxy trust,
  SQLite, development identities, and missing resource limits;
- schema status/check/migrate commands distinguish a fresh, current, old, and future schema;
- backup verification catches corruption and restore refuses incompatible or non-empty targets;
- local and S3-compatible artifact storage pass one behavioral contract;
- retention is idempotent, excludes active resources, and audits physical deletion;
- liveness stays process-only while readiness covers required database/schema/storage/gateway state;
- control-plane/Agent application and protocol compatibility are reported and enforced;
- community distributions contain no commercial imports or dependencies; and
- production logs are structured, correlated, and redacted.

## Evidence map

| Requirement family | Primary evidence |
| --- | --- |
| Packages/dashboard | `.github/workflows/release.yml`, `scripts/verify_web_wheel.py` |
| Images, SBOM, signatures | `docker/*/Dockerfile`, release and security workflows |
| Community boundary | `scripts/release/check_oss_boundary.py`, feature-provider unit tests |
| Production configuration/diagnostics | Phase 8 config and control-plane CLI unit tests |
| Storage, backup, retention | Phase 8 operations unit/integration tests |
| Compatibility | release metadata, protocol, CLI version, and upgrade tests |
| Real product path | published-artifact Compose acceptance test with PostgreSQL and SimLab |

This page is the phase contract, not permission to claim an untested item. Each release notes its
actual rollback class, supported versions, known limitations, and the exact acceptance run.
