# Open-core model

Lab Platform's commercial proposition is operational convenience: pay for somebody else to run,
upgrade, back up, monitor, and support the platform. The community product must remain useful on
its own and must never import a commercial package to start or perform core lab work.

## Community edition

The Apache-2.0 community distribution includes:

- the Agent, control plane, web dashboard, CLI, and REST API;
- SimLab and physical-backend/plugin SDK support;
- distributed Agent enrollment and inventory;
- reservations, scheduling, queues, workflows, and artifacts;
- vendor-neutral CI integrations;
- local authentication, organisations, users, service accounts, teams, fixed roles, and core RBAC;
- basic audit records and configurable community retention;
- local and S3-compatible artifact storage; and
- self-hosting, diagnostics, explicit migrations, and operator-driven backup/upgrade tooling.

Basic Agent support, firmware flashing, reservations, workflows, CI, SimLab, fundamental RBAC, and
the plugin SDK are adoption features, not commercial gates.

## Commercial and managed value

Separately distributed services or extensions may add:

- hosted control planes and managed PostgreSQL/object storage;
- automated backup verification and managed upgrades;
- extended artifact and audit retention services;
- advanced OIDC/organisation policy controls;
- multi-site administration and larger-scale observability;
- managed Agent update channels; and
- operational support, deployment assistance, and SLA-backed service.

Those capabilities reduce operational burden or add organisation-scale convenience. They must not
turn an ordinary self-hosted community workflow into a deliberately frustrating product.

## Feature-provider boundary

Community code uses one typed seam:

```python
class FeatureProvider(Protocol):
    @property
    def edition(self) -> str: ...

    def enabled(self, feature: Feature) -> bool: ...
```

`CommunityFeatureProvider` is the default and reports every commercial extension point disabled.
The current explicit feature names cover advanced audit/OIDC policy, automated backups, extended
retention, managed Agent channels/upgrades, multi-site administration, organisation policy, and
scale observability.

Rules for new code:

1. Do not scatter `if enterprise` or package-import probes through application code.
2. Add an explicit `Feature` only for a genuinely commercial convenience, never for core lab
   behavior.
3. Inject a provider at the composition boundary; core domain objects must not locate commercial
   code dynamically.
4. A disabled feature must produce a clear capability response, not a mysterious failure or a
   degraded community code path.
5. Shared security, compatibility, and data-integrity fixes belong in the community core.

## Repository and package boundary

The initial implementation keeps the community application in this repository. A future
commercial package should use a separately distributed namespace/repository or another visibly
separated module set and depend on stable community interfaces in the permitted direction:

```text
commercial extension ──depends on──> community interfaces
community distribution ──must not depend on──> commercial modules
```

The source and wheel release gate rejects imports from known enterprise/commercial namespaces,
commercial directories in the community wheel, and a community package dependency on an
enterprise distribution. This check complements normal tests; it does not replace architectural
review of subtler coupling.

Community persistence migrations and configuration must also operate without commercial tables,
secrets, licenses, or external services. An OSS build should pass its complete non-hardware gate
with `CommunityFeatureProvider` and no commercial source checkout.

## Data and API rules

- Community data remains readable and recoverable with community tools.
- Disabling or removing a commercial extension must not orphan core organisations, Agents,
  workflows, or artifacts.
- Extension-owned schema uses an explicit ownership/migration boundary.
- Public community APIs do not silently change behavior based on a hidden license state.
- Managed hosting uses the same Agent and protocol as self-hosting; customers do not install an
  artificially restricted Agent.

## Product review checklist

Before accepting a proposed paid feature, ask:

- Is this primarily saved operational work or advanced organisational convenience?
- Can a small team still install, secure, run, back up, upgrade, and recover the community edition?
- Does the community path remain documented and tested?
- Is the feature behind the provider boundary with no reverse dependency?
- Are license, data portability, support, and failure behavior explicit?

If the answer would remove a core lab capability from community users, the boundary is wrong.
See [licensing](LICENSING.md) and [managed service](MANAGED_SERVICE.md).
