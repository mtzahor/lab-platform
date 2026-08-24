# Security policy

## Reporting a vulnerability

Do not open a public issue for a suspected vulnerability that could expose credentials, tenant
data, Agents, benches, artifacts, or deployment infrastructure. Use GitHub’s private security
advisory flow for this repository and include:

- the affected version and deployment mode;
- a minimal reproduction or clear attack path;
- the security impact and required preconditions; and
- whether credentials, tenant boundaries, or physical hardware safety are involved.

Do not include live tokens, passwords, private keys, customer data, or proprietary firmware. Use
synthetic identifiers and redact logs. Maintainers will acknowledge a complete report, assess the
supported release channels, and coordinate remediation and disclosure.

## Supported versions

During the pre-1.0 period, security fixes target the current stable release and, when a preview is
the only available Phase 8 line, the latest preview. Nightly builds are unsupported and can change
without notice. Exact support windows are recorded in each GitHub release and in
`docs/VERSION_COMPATIBILITY.md`.

## Deployment boundary

Security depends on production configuration. Internet-facing deployments must use TLS, disable
development identities and legacy compatibility, isolate PostgreSQL and object storage, protect
Agent credentials, and test backup restoration. Run `lab-control-plane production-check` before
exposure and follow `docs/PRODUCTION_SECURITY.md`.

## Repository security gates

The supply-chain workflow always audits the complete Python and frontend lockfiles and scans the
source tree and runtime images with Trivy. GitHub dependency review additionally requires the
repository Dependency graph. After enabling that feature under **Settings → Advanced Security**,
set the repository Actions variable `DEPENDENCY_REVIEW_ENABLED=true`; the workflow then treats
high-severity dependency changes as a required failure. Until both settings are present, the job
emits a warning instead of invoking an API that GitHub reports as unsupported.
