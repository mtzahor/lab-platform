# Release and support policy

Lab Platform uses semantic product versions, independent Plugin API/Agent protocol/schema
versions, and a deliberately modest maintenance commitment. This policy applies after `1.0.0`;
the current `0.9.0-beta` line is preview software.

## Release categories

- **Regular releases** deliver useful compatible fixes and features when ready. No monthly train
  is promised.
- **Release candidates** (`1.0.0-rc.N`) are production-like validation builds. They are not LTS
  and may be replaced by another candidate after a release blocker.
- **LTS releases** receive security and critical correctness/hardware-safety fixes for a stated
  support window. LTS designation is explicit in release notes and metadata; a normal release
  never becomes LTS by implication.
- **Preview/nightly releases** are for evaluation and do not receive long-term fixes.

At most one LTS minor line is active at a time. The initial sustainable target is 12 months from
its LTS designation. An LTS line is created only when maintainer capacity and user demand justify
it; `1.0.0` is not automatically promised as LTS.

## What an active LTS line receives

- security fixes for supported community components
- critical data-integrity, backup/restore, authentication, authorization, and tenant-isolation
  fixes
- critical hardware-safety and resource-lock fixes
- severe availability regressions with a bounded backport
- compatibility documentation updates where the supported environment changes

New hardware families, broad features, UI redesigns, ordinary performance improvements, and
breaking dependency upgrades go to the current regular line. A risky backport may be declined in
favor of an upgrade when that is safer; the advisory explains the decision.

## Support boundary

Support covers the documented community build, deployment shape, database, artifact stores, host
platforms, and exact hardware matrix rows for the release. It does not make every plugin/device
combination supported, replace vendor support, promise managed-service SLAs, or certify safe use in
medical, vehicle, mains-power, or other safety-critical control systems.

Community plugins follow their named maintainer's policy. A Plugin API-compatible package can load
without becoming an officially supported device.

## End of support

Release notes announce an LTS end date when the line is designated. At end of support, fixes move
to the current line and users should upgrade. If a severe issue requires support to end early, the
project publishes the reason, mitigation, replacement target, and revised date. Source remains
available under its license; end of support means no maintenance commitment.

## Upgrade and downgrade

Read every intervening release note, take and verify an off-host backup, validate configuration,
run database migrations explicitly, upgrade the control plane before or within the documented
Agent compatibility window, and execute the acceptance workflow. Agent fleet status reports
current/recommended/unsupported versions; automatic remote package replacement is not promised.

Downgrade is restore-based unless release notes explicitly provide a reversible migration. Keep
the pre-upgrade database/artifact/config backup until the new release passes acceptance. Never run
an older binary against a schema it does not declare compatible.

## Security reporting

Report vulnerabilities privately using [`SECURITY.md`](../SECURITY.md). Public issues must not
include credentials, exploitable target commands, private device identities, or undisclosed
vulnerability details. Security advisories name affected/safe versions, mitigation, and any
deprecation-policy exception.

## Cadence and compatibility

Small patch releases may ship whenever a reviewed fix is ready. Minor releases group compatible
features when useful. Major releases are reserved for deliberate public-interface breaks with a
migration path. The detailed REST, CLI, Plugin API, workflow/config, and deprecation rules are in
[the stability policy](STABILITY_POLICY.md); release channels and upgrade mechanics remain in
[release channels](RELEASE_CHANNELS.md) and [upgrading](UPGRADING.md).
